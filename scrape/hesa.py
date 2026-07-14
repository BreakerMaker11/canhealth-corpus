"""
HESA written-briefs harvester — Stage 2 (parliament session 44-1 and 45-1).

Harvests per study (Work tab only):
  hesa_brief   : Written Briefs (GetBriefs AJAX endpoint -> PDF)
  hesa_report  : Committee Report (DocumentViewer page -> PDF link)
  gov_response : Government Response (DocumentViewer page -> PDF link)

Usage:
  uv run python -m scrape.hesa [--session 44-1] [--limit N] [--dry-run]

  --session 44-1   Parliament-session to harvest (default: 44-1)
  --limit N        Stop after downloading N PDFs (for development/debug)
  --dry-run        Print what would be fetched; no network writes, no file writes
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from bs4 import BeautifulSoup, NavigableString
from datetime import date
from pathlib import Path

import requests
from dateutil import parser as du_parser

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
RAW = ROOT / "data" / "raw"
MANIFEST_PATH = ROOT / "manifest.jsonl"
FAILED_PATH = ROOT / "failed.csv"
DISCARDED_PATH = ROOT / "discarded_studies.csv"
WHITELIST_PATH = ROOT / "studies_whitelist.csv"

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
BASE = "https://www.ourcommons.ca"
HEADERS = {
    "User-Agent": "canhealth-corpus/0.1 (personal research; contact: syermakou@gmail.com)"
}
DELAY = 1.0  # seconds between requests (politeness rule)

# ---------------------------------------------------------------------------
# Procedural study patterns — studies whose titles start with or contain these
# are logged to discarded_studies.csv and skipped.
# Bills are included by default; individual entries can be whitelisted later.
# ---------------------------------------------------------------------------
PROCEDURAL_PREFIXES = (
    "Committee Business",
    "Election of ",
    "Meeting Requested Pursuant to Standing Order",
    "Meeting Requested by ",
    "Briefing on ",
    "Briefing by ",
    "Subject Matter of ",
    "Main Estimates ",
    "Supplementary Estimates",
    "Departmental Plan ",
)
PROCEDURAL_CONTAINS = (
    "Bill C-",
    "Bill S-",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str, max_len: int = 28) -> str:
    """Lowercase, alphanumeric + underscores, no leading/trailing underscores."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"_+", "_", text)
    return text[:max_len].rstrip("_")


def session_slug(session: str) -> str:
    """'44-1' -> '441'"""
    return session.replace("-", "")


def load_whitelist() -> set[str]:
    """Return set of whitelisted study_activity_ids from studies_whitelist.csv."""
    if not WHITELIST_PATH.exists():
        return set()
    with WHITELIST_PATH.open(encoding="utf-8") as f:
        return {row["study_activity_id"].strip() for row in csv.DictReader(f) if row.get("study_activity_id")}


def is_procedural(title: str) -> tuple[bool, str]:
    """Return (True, reason) if the study is procedural/administrative."""
    for prefix in PROCEDURAL_PREFIXES:
        if title.startswith(prefix):
            return True, f"starts_with:{prefix.strip()}"
    for fragment in PROCEDURAL_CONTAINS:
        if fragment in title:
            return True, f"contains:{fragment.strip()}"
    return False, ""


def parse_published(raw_text: str) -> tuple[str | None, str | None]:
    """
    Parse "Published on: Monday, May 9, 2022 at 8:05 a.m. (EDT)".
    Returns (iso_date, None) on success, (None, raw_text) on failure.
    Never approximates — if parsing fails, date is left null.
    """
    try:
        cleaned = raw_text.replace("Published on:", "").strip()
        cleaned = re.sub(r"\s*\(.*?\)\s*$", "", cleaned).strip()
        cleaned = cleaned.replace("a.m.", "AM").replace("p.m.", "PM")
        dt = du_parser.parse(cleaned, fuzzy=True)
        return dt.date().isoformat(), None
    except Exception:
        return None, raw_text if raw_text else None


def direct_text(tag) -> str:
    """
    Text directly inside `tag`, excluding text from child elements.
    Uses NavigableString to avoid capturing nested tag text.
    """
    return "".join(
        str(c) for c in tag.contents if isinstance(c, NavigableString)
    ).strip()


def abs_url(href: str) -> str:
    """Resolve protocol-relative or root-relative URLs to absolute https."""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return BASE + href
    return href


# ---------------------------------------------------------------------------
# Manifest / state helpers
# ---------------------------------------------------------------------------

def load_manifest() -> tuple[set[str], set[str]]:
    """Return (existing_doc_ids, existing_source_urls) from manifest.jsonl."""
    ids: set[str] = set()
    urls: set[str] = set()
    if MANIFEST_PATH.exists():
        for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                obj = json.loads(line)
                ids.add(obj["doc_id"])
                urls.add(obj["source_url"])
    return ids, urls


def next_counter(prefix: str, existing_ids: set[str], in_flight: set[str]) -> int:
    """Return the next unused 4-digit counter for `prefix_{NNNN}`."""
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d{{4}})$")
    used: set[int] = set()
    for doc_id in existing_ids | in_flight:
        m = pattern.match(doc_id)
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return n


def append_manifest(entry: dict, dry_run: bool) -> None:
    if dry_run:
        print(f"  [DRY-RUN] manifest << {json.dumps(entry)}")
        return
    with MANIFEST_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_failed(doc_id: str, url: str, error: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [DRY-RUN] failed.csv << {doc_id} | {url} | {error}")
        return
    with FAILED_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([doc_id, url, error, date.today().isoformat()])


def log_discarded(study_id: str, title: str, session: str, reason: str) -> None:
    """Append to discarded_studies.csv; silently skip if already present."""
    existing: set[str] = set()
    if DISCARDED_PATH.exists():
        for row in csv.DictReader(DISCARDED_PATH.open(encoding="utf-8")):
            existing.add(row["study_activity_id"])
    if study_id in existing:
        return
    with DISCARDED_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([study_id, title, session, reason])


# ---------------------------------------------------------------------------
# HTTP fetch (polite, with backoff)
# ---------------------------------------------------------------------------

def fetch(url: str, sess: requests.Session, dry_run: bool) -> str | None:
    """
    GET url, enforce 1 req/s delay, back off 30 s on 429/403 then stop.
    Returns response text, or None in dry_run.
    """
    if dry_run:
        print(f"  [DRY-RUN] GET {url}")
        return None
    r = sess.get(url, headers=HEADERS, timeout=30)
    time.sleep(DELAY)
    if r.status_code in (429, 403):
        print(f"  BACKOFF {r.status_code}: {url} — waiting 30 s then stopping.")
        time.sleep(30)
        raise SystemExit(f"Server refused ({r.status_code}); stopped to avoid abuse.")
    r.raise_for_status()
    return r.text


def fetch_binary(url: str, dest: Path, sess: requests.Session, dry_run: bool) -> bool:
    """Download binary (PDF) to dest. Returns True on success."""
    if dry_run:
        print(f"  [DRY-RUN] DOWNLOAD {url} -> {dest.name}")
        return True
    r = sess.get(url, headers=HEADERS, timeout=60)
    time.sleep(DELAY)
    if r.status_code in (429, 403):
        print(f"  BACKOFF {r.status_code}: {url} — waiting 30 s then stopping.")
        time.sleep(30)
        raise SystemExit(f"Server refused ({r.status_code}); stopped.")
    r.raise_for_status()
    dest.write_bytes(r.content)
    return True


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_study_list(html: str) -> list[dict]:
    """
    Parse the Work-tab page to extract unique (studyActivityId, title) pairs.
    Returns list of dicts with keys: study_id, title.
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    studies: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "StudyActivity?studyActivityId=" not in href:
            continue
        sid = href.split("studyActivityId=")[1].split("&")[0]
        title = a.get_text(strip=True)
        if sid in seen or not title:
            continue
        seen.add(sid)
        studies.append({"study_id": sid, "title": title})
    return studies


def parse_study_meta(html: str) -> dict:
    """
    Parse a StudyActivity page.
    Returns: {title, report_viewer_url, govresponse_viewer_url}
    Both viewer URLs may be None.
    """
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    report_url: str | None = None
    govresponse_url: str | None = None

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Match report-N/ links (not meeting-N links)
        if re.search(r"/DocumentViewer/en/\d+-\d+/HESA/report-\d+/$", href):
            if report_url is None:
                report_url = abs_url(href)
        elif re.search(r"/DocumentViewer/en/\d+-\d+/HESA/report-\d+/response-", href):
            if govresponse_url is None:
                govresponse_url = abs_url(href)

    return {
        "title": title,
        "report_viewer_url": report_url,
        "govresponse_viewer_url": govresponse_url,
    }


def resolve_pdf_from_viewer(html: str) -> str | None:
    """Find the PDF download link inside a DocumentViewer page."""
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).lower()
        if href.lower().endswith(".pdf") or text == "pdf":
            return abs_url(href)
    return None


def parse_briefs(html: str) -> list[dict]:
    """
    Parse the GetBriefs AJAX fragment.
    Returns list of dicts: {organization, pdf_url, is_joint,
                             published_date?, published_raw?}
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []

    for item in soup.select(".brief-item"):
        pdf_link = item.select_one("a.btn-brief")
        if not pdf_link:
            continue

        pdf_url = abs_url(pdf_link["href"])

        # Published date
        pub_el = item.select_one(".published")
        pub_raw = pub_el.get_text(strip=True) if pub_el else ""
        pub_date, pub_raw_fallback = parse_published(pub_raw) if pub_raw else (None, None)

        # Joint vs single-org brief
        joint_lis = item.select("li.joint-name")
        if joint_lis:
            orgs = []
            for li in joint_lis:
                name = direct_text(li).strip()
                if name:
                    orgs.append(name)
            organization = " / ".join(orgs)
            is_joint = True
        else:
            name_el = item.select_one(".name")
            organization = direct_text(name_el).strip() if name_el else ""
            is_joint = False

        entry: dict = {
            "organization": organization,
            "pdf_url": pdf_url,
            "is_joint": is_joint,
        }
        if pub_date:
            entry["published_date"] = pub_date
        elif pub_raw_fallback:
            entry["published_raw"] = pub_raw_fallback

        results.append(entry)

    return results


# ---------------------------------------------------------------------------
# Download + manifest helpers
# ---------------------------------------------------------------------------

def download_and_record(
    *,
    doc_id: str,
    source_url: str,
    source_type: str,
    organization: str | None,
    study_title: str,
    session: str,
    file_type: str,
    extra_fields: dict,
    existing_ids: set[str],
    existing_urls: set[str],
    in_flight_ids: set[str],
    in_flight_urls: set[str],
    sess: requests.Session,
    dry_run: bool,
) -> bool:
    """
    Download one PDF and append its manifest line. Returns True if downloaded.
    Skips silently if doc_id or source_url already seen (manifest or in-flight).
    """
    if doc_id in existing_ids or doc_id in in_flight_ids:
        print(f"  SKIP {doc_id} (doc_id already in manifest)")
        return False
    if source_url in existing_urls or source_url in in_flight_urls:
        print(f"  SKIP {doc_id} (source_url already in manifest)")
        return False

    dest = RAW / f"{doc_id}.{file_type}"

    # Download
    if not dry_run and dest.exists():
        print(f"  FILE EXISTS {dest.name} — recording manifest only")
    else:
        try:
            ok = fetch_binary(source_url, dest, sess, dry_run)
            if not ok:
                return False
        except Exception as exc:
            print(f"  FAIL {doc_id}: {exc}")
            log_failed(doc_id, source_url, str(exc), dry_run)
            return False

    # Manifest entry
    manifest_entry: dict = {
        "doc_id": doc_id,
        "source_url": source_url,
        "fetch_date": date.today().isoformat(),
        "source_type": source_type,
        "organization": organization,
        "study_or_consultation_title": study_title,
        "parliament_session": session,
        "language": None,
        "file_type": file_type,
        "needs_ocr": None,
    }
    manifest_entry.update(extra_fields)
    append_manifest(manifest_entry, dry_run)

    in_flight_ids.add(doc_id)
    in_flight_urls.add(source_url)
    return True


def save_html_and_record(
    *,
    doc_id: str,
    viewer_url: str,
    html_content: str,
    pdf_url: str | None,
    source_type: str,
    organization: str | None,
    study_title: str,
    session: str,
    existing_ids: set[str],
    existing_urls: set[str],
    in_flight_ids: set[str],
    in_flight_urls: set[str],
    dry_run: bool,
) -> bool:
    """
    Save an already-fetched DocumentViewer HTML page to data/raw/ and record
    its manifest entry.  source_url = viewer_url (HTML), pdf_url = linked PDF.
    Returns True if the document was saved (new); False if skipped.
    """
    if doc_id in existing_ids or doc_id in in_flight_ids:
        print(f"  SKIP {doc_id} (doc_id already in manifest)")
        return False
    if viewer_url in existing_urls or viewer_url in in_flight_urls:
        print(f"  SKIP {doc_id} (source_url already in manifest)")
        return False

    dest = RAW / f"{doc_id}.html"
    if dry_run:
        print(f"  [DRY-RUN] WRITE {dest.name}  ({len(html_content):,} chars)")
    elif dest.exists():
        print(f"  FILE EXISTS {dest.name} — recording manifest only")
    else:
        dest.write_text(html_content, encoding="utf-8")

    manifest_entry: dict = {
        "doc_id": doc_id,
        "source_url": viewer_url,
        "fetch_date": date.today().isoformat(),
        "source_type": source_type,
        "organization": organization,
        "study_or_consultation_title": study_title,
        "parliament_session": session,
        "language": None,
        "file_type": "html",
        "needs_ocr": None,
    }
    if pdf_url:
        manifest_entry["pdf_url"] = pdf_url
    append_manifest(manifest_entry, dry_run)

    in_flight_ids.add(doc_id)
    in_flight_urls.add(viewer_url)
    return True


# ---------------------------------------------------------------------------
# Main harvest logic
# ---------------------------------------------------------------------------

def harvest(session: str, limit: int | None, dry_run: bool) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    parl, sess_num = session.split("-")
    ss = session_slug(session)  # "441"

    http_sess = requests.Session()
    existing_ids, existing_urls = load_manifest()
    in_flight_ids: set[str] = set()
    in_flight_urls: set[str] = set()
    downloaded = 0
    whitelist = load_whitelist()
    if whitelist:
        print(f"  Whitelist loaded: {len(whitelist)} study_activity_id(s)")

    def over_limit() -> bool:
        return limit is not None and downloaded >= limit

    # ------------------------------------------------------------------
    # Step 1: Fetch the Work tab to get all studies
    # ------------------------------------------------------------------
    work_url = f"{BASE}/Committees/en/HESA/Work?parl={parl}&session={sess_num}"
    print(f"\n=== Fetching study list: {work_url}")
    work_html = fetch(work_url, http_sess, dry_run)

    if dry_run:
        print("  (dry-run: using fixture for study list)")
        fixture = ROOT / "fixtures" / "hesa441_work_tab.html"
        work_html = fixture.read_text(encoding="utf-8") if fixture.exists() else ""

    all_studies = parse_study_list(work_html or "")
    print(f"  Found {len(all_studies)} unique studies in {session}")

    # Filter procedural; whitelist checked first; log discards
    substantive: list[dict] = []
    for study in all_studies:
        sid = study["study_id"]
        if sid in whitelist:
            print(f"  WHITELIST [{sid}] {study['title'][:70]}")
            substantive.append(study)
            continue
        proc, reason = is_procedural(study["title"])
        if proc:
            print(f"  DISCARD [{sid}] {study['title'][:70]}  ({reason})")
            log_discarded(sid, study["title"], session, reason)
        else:
            substantive.append(study)
    print(f"  Substantive studies: {len(substantive)}")

    # ------------------------------------------------------------------
    # Step 2: Per-study harvest
    # ------------------------------------------------------------------
    for study in substantive:
        if over_limit():
            break

        sid = study["study_id"]
        study_url = f"{BASE}/committees/en/HESA/StudyActivity?studyActivityId={sid}"
        print(f"\n--- Study [{sid}]: {study['title'][:70]}")

        # Fetch study page for report/gov_response links
        time.sleep(DELAY)
        study_html = fetch(study_url, http_sess, dry_run)
        if dry_run:
            # In dry-run fall back to workforce fixture for the first study
            fixture = ROOT / "fixtures" / "study_workforce.html"
            study_html = fixture.read_text(encoding="utf-8") if fixture.exists() else ""

        meta = parse_study_meta(study_html or "")
        study_title = meta["title"] or study["title"]
        study_slug = slugify(study_title)
        doc_prefix = f"hesa{ss}_{study_slug}"

        # ---- hesa_brief: fetch briefs listing ----
        briefs_url = (
            f"{BASE}/committees/en/HESA/StudyActivity/GetBriefs?studyActivityId={sid}"
        )
        briefs_html = fetch(briefs_url, http_sess, dry_run)
        if dry_run:
            fixture = ROOT / "fixtures" / "study_workforce_briefs.html"
            briefs_html = fixture.read_text(encoding="utf-8") if fixture.exists() else ""

        briefs = parse_briefs(briefs_html or "")
        print(f"  {len(briefs)} brief(s) found")

        for brief in briefs:
            if over_limit():
                break

            counter = next_counter(doc_prefix, existing_ids, in_flight_ids)
            doc_id = f"{doc_prefix}_{counter:04d}"

            extra: dict = {}
            if brief.get("is_joint"):
                extra["joint"] = True
            if "published_date" in brief:
                extra["published_date"] = brief["published_date"]
            elif "published_raw" in brief:
                extra["published_raw"] = brief["published_raw"]

            print(f"  BRIEF {doc_id}  org={brief['organization'][:50]!r}")
            ok = download_and_record(
                doc_id=doc_id,
                source_url=brief["pdf_url"],
                source_type="hesa_brief",
                organization=brief["organization"] or None,
                study_title=study_title,
                session=session,
                file_type="pdf",
                extra_fields=extra,
                existing_ids=existing_ids,
                existing_urls=existing_urls,
                in_flight_ids=in_flight_ids,
                in_flight_urls=in_flight_urls,
                sess=http_sess,
                dry_run=dry_run,
            )
            if ok:
                downloaded += 1

        # ---- hesa_report ----
        if not over_limit() and meta["report_viewer_url"]:
            viewer_url = meta["report_viewer_url"]
            viewer_html = fetch(viewer_url, http_sess, dry_run)
            if dry_run:
                fixture = ROOT / "fixtures" / "hesa441_report10.html"
                viewer_html = fixture.read_text(encoding="utf-8") if fixture.exists() else ""

            if viewer_html:
                pdf_url = resolve_pdf_from_viewer(viewer_html)
                if not pdf_url:
                    print(f"  WARN: no PDF link found in report viewer: {viewer_url}")
                counter = next_counter(doc_prefix, existing_ids, in_flight_ids)
                doc_id = f"{doc_prefix}_{counter:04d}"
                print(f"  REPORT {doc_id}")
                ok = save_html_and_record(
                    doc_id=doc_id,
                    viewer_url=viewer_url,
                    html_content=viewer_html,
                    pdf_url=pdf_url,
                    source_type="hesa_report",
                    organization="Standing Committee on Health",
                    study_title=study_title,
                    session=session,
                    existing_ids=existing_ids,
                    existing_urls=existing_urls,
                    in_flight_ids=in_flight_ids,
                    in_flight_urls=in_flight_urls,
                    dry_run=dry_run,
                )
                if ok:
                    downloaded += 1

        # ---- gov_response ----
        if not over_limit() and meta["govresponse_viewer_url"]:
            viewer_url = meta["govresponse_viewer_url"]
            viewer_html = fetch(viewer_url, http_sess, dry_run)
            if dry_run:
                viewer_html = ""  # no gov_response fixture; skip in dry-run

            if viewer_html:
                pdf_url = resolve_pdf_from_viewer(viewer_html)
                if not pdf_url:
                    print(f"  WARN: no PDF link found in gov_response viewer: {viewer_url}")
                counter = next_counter(doc_prefix, existing_ids, in_flight_ids)
                doc_id = f"{doc_prefix}_{counter:04d}"
                print(f"  GOV_RESPONSE {doc_id}")
                ok = save_html_and_record(
                    doc_id=doc_id,
                    viewer_url=viewer_url,
                    html_content=viewer_html,
                    pdf_url=pdf_url,
                    source_type="gov_response",
                    organization="Government of Canada",
                    study_title=study_title,
                    session=session,
                    existing_ids=existing_ids,
                    existing_urls=existing_urls,
                    in_flight_ids=in_flight_ids,
                    in_flight_urls=in_flight_urls,
                    dry_run=dry_run,
                )
                if ok:
                    downloaded += 1

    print(f"\n=== Done. {downloaded} file(s) downloaded.")
    if limit is not None and downloaded >= limit:
        print(f"    (stopped at --limit {limit})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest HESA briefs/reports from ourcommons.ca")
    ap.add_argument("--session", default="44-1", help="Parliament session, e.g. 44-1 (default)")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Stop after downloading N PDFs (omit for full run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be fetched without writing anything")
    args = ap.parse_args()

    if args.dry_run:
        print("=== DRY-RUN mode — no files written ===")

    harvest(session=args.session, limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
