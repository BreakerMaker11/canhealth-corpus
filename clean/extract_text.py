"""Stage 3: Extract text from data/raw/ into data/interim/.

PDFs  — PyMuPDF blocks mode, position-sorted; [[page N]] markers between pages.
        If a multi-page PDF yields < 200 chars total, set needs_ocr=true, skip.
HTML  — BeautifulSoup; headings as markdown markers, list items as distinct lines.
        (reports/gov_responses: DocumentViewer HTML, no page markers, no OCR check.)

Both  — langdetect on first 1,000 chars → record language in manifest.

Usage:
  uv run python -m clean.extract_text [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
from pathlib import Path

import tempfile
import time

import fitz  # PyMuPDF
import requests as _requests
from bs4 import BeautifulSoup, NavigableString, Tag
from langdetect import detect, LangDetectException

ROOT = Path(__file__).parent.parent
RAW = ROOT / "data" / "raw"
INTERIM = ROOT / "data" / "interim"
MANIFEST_PATH = ROOT / "manifest.jsonl"

OCR_CHAR_THRESHOLD = 200  # fewer chars on a multi-page PDF → needs_ocr
HTML_SPARSE_WORDS = 500   # DocumentViewer HTML only has cover page; fall back to pdf_url
UA = "canhealth-corpus/0.1 (personal research; contact: syermakou@gmail.com)"

HEADING_PREFIX = {
    "h1": "# ", "h2": "## ", "h3": "### ",
    "h4": "#### ", "h5": "##### ", "h6": "###### ",
}


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def extract_pdf(path: Path) -> tuple[str | None, bool]:
    """
    Extract text from a PDF using PyMuPDF blocks mode.

    Returns (text, needs_ocr).
    text is None when needs_ocr is True (multi-page, < 200 chars).
    [[page N]] markers are written between pages (not before page 1).
    """
    doc = fitz.open(str(path))
    page_texts: list[str] = []

    for page in doc:
        blocks = page.get_text("blocks")
        # filter to text blocks (type 0); sort top-to-bottom, left-to-right
        text_blocks = [b for b in blocks if b[6] == 0]
        text_blocks.sort(key=lambda b: (b[1], b[0]))
        page_text = "\n".join(b[4].strip() for b in text_blocks if b[4].strip())
        page_texts.append(page_text)

    total_text = "\n".join(page_texts)

    if doc.page_count > 1 and len(total_text) < OCR_CHAR_THRESHOLD:
        doc.close()
        return None, True

    doc.close()

    # Join pages with [[page N]] markers between them
    parts: list[str] = []
    for i, text in enumerate(page_texts):
        if i > 0:
            parts.append(f"[[page {i + 1}]]")
        if text:
            parts.append(text)

    return "\n".join(parts), False


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------

def _walk_content(tag: Tag, lines: list[str]) -> None:
    """Recursively walk a BeautifulSoup tag tree, appending structured lines."""
    for child in tag.children:
        if isinstance(child, NavigableString):
            continue
        if not isinstance(child, Tag):
            continue

        name = child.name.lower() if child.name else ""

        if name in HEADING_PREFIX:
            text = child.get_text(" ", strip=True)
            if text:
                lines.append(f"{HEADING_PREFIX[name]}{text}")

        elif name == "p":
            text = child.get_text(" ", strip=True)
            if text:
                lines.append(text)

        elif name in ("ul", "ol"):
            for li in child.find_all("li", recursive=False):
                text = li.get_text(" ", strip=True)
                if text:
                    lines.append(text)

        elif name == "li":
            # li encountered outside a ul/ol (shouldn't happen often)
            text = child.get_text(" ", strip=True)
            if text:
                lines.append(text)

        elif name == "table":
            for row in child.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
                line = "\t".join(c for c in cells if c)
                if line:
                    lines.append(line)

        elif name in ("div", "section", "article", "main", "blockquote", "aside"):
            _walk_content(child, lines)

        # skip nav, header, footer, script, style, figure, etc.


def extract_html(path: Path) -> str:
    """
    Extract structured text from a DocumentViewer HTML file.
    Headings → markdown markers; list items → one per line; paragraphs → plain.
    No [[page N]] markers (these docs are cited by section, not page).
    """
    html = path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")

    # Remove nav, header, footer, scripts, styles
    for tag in soup.find_all(["nav", "header", "footer", "script", "style", "noscript"]):
        tag.decompose()

    # Prefer #publicationContent, then <main>, then <body>
    content = (
        soup.find(id="publicationContent")
        or soup.find("main")
        or soup.body
    )
    if content is None:
        return ""

    lines: list[str] = []
    _walk_content(content, lines)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PDF URL fallback (for paginated DocumentViewer HTML reports)
# ---------------------------------------------------------------------------

def download_and_extract_pdf_url(url: str) -> tuple[str | None, bool]:
    """Download a PDF from url, extract text, delete the temp file.

    Reports/gov_responses are saved as single-page HTML (cover only) because
    DocumentViewer paginates content. The full text lives in the linked PDF.
    Returns (text, needs_ocr) same as extract_pdf().
    """
    time.sleep(1)  # politeness
    resp = _requests.get(url, headers={"User-Agent": UA}, timeout=60)
    resp.raise_for_status()
    tmp_path = Path(tempfile.mktemp(suffix=".pdf"))
    try:
        tmp_path.write_bytes(resp.content)
        return extract_pdf(tmp_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

def detect_language(text: str) -> str | None:
    sample = text[:1000].strip()
    if not sample:
        return None
    try:
        return detect(sample)
    except LangDetectException:
        return None


# ---------------------------------------------------------------------------
# Manifest update
# ---------------------------------------------------------------------------

def load_manifest() -> dict[str, dict]:
    """Load manifest.jsonl into an ordered dict keyed by doc_id."""
    entries: dict[str, dict] = {}
    if MANIFEST_PATH.exists():
        for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                obj = json.loads(line)
                entries[obj["doc_id"]] = obj
    return entries


def save_manifest(entries: dict[str, dict]) -> None:
    """Rewrite manifest.jsonl preserving insertion order."""
    with MANIFEST_PATH.open("w", encoding="utf-8") as f:
        for entry in entries.values():
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Summary / report
# ---------------------------------------------------------------------------

def print_report(
    entries: dict[str, dict],
    extracted: list[str],
    skipped_ocr: list[str],
    skipped_exists: int,
) -> None:
    print("\n" + "=" * 60)
    print("EXTRACTION SUMMARY")
    print("=" * 60)
    print(f"  Extracted this run : {len(extracted)}")
    print(f"  Skipped (exists)   : {skipped_exists}")
    print(f"  needs_ocr (skipped): {len(skipped_ocr)}")
    print(f"  Total in manifest  : {len(entries)}")

    # Language breakdown
    lang_counts: dict[str, int] = {}
    for doc_id in entries:
        lang = entries[doc_id].get("language") or "unknown"
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
    print("\nLanguage breakdown:")
    for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1]):
        print(f"  {lang:<10} {count}")

    # needs_ocr list
    print(f"\nneeds_ocr docs ({len(skipped_ocr)}):")
    for doc_id in skipped_ocr:
        e = entries.get(doc_id, {})
        print(f"  {doc_id}  [{e.get('source_type','')}]  {e.get('organization','')[:50]}")

    # Word count distribution (from interim files)
    word_counts: list[int] = []
    for doc_id in entries:
        if entries[doc_id].get("needs_ocr"):
            continue
        txt_path = INTERIM / f"{doc_id}.txt"
        if txt_path.exists():
            wc = len(txt_path.read_text(encoding="utf-8").split())
            word_counts.append(wc)

    if word_counts:
        word_counts.sort()
        n = len(word_counts)
        print(f"\nWord-count distribution ({n} docs with extracted text):")
        print(f"  min    : {word_counts[0]:,}")
        print(f"  p10    : {word_counts[int(n * 0.10)]:,}")
        print(f"  median : {word_counts[n // 2]:,}")
        print(f"  p90    : {word_counts[int(n * 0.90)]:,}")
        print(f"  max    : {word_counts[-1]:,}")
        print(f"  mean   : {statistics.mean(word_counts):,.0f}")

    # Three random samples
    random.seed(42)
    _print_samples(entries)


def _print_samples(entries: dict[str, dict]) -> None:
    print("\n--- Random samples ---")

    briefs = [d for d in entries.values()
              if d["source_type"] == "hesa_brief" and not d.get("needs_ocr")
              and (INTERIM / f"{d['doc_id']}.txt").exists()]
    reports = [d for d in entries.values()
               if d["source_type"] in ("hesa_report", "gov_response")
               and (INTERIM / f"{d['doc_id']}.txt").exists()]
    non_en = [d for d in entries.values()
              if d.get("language") and d["language"] not in ("en", "unknown", None)
              and not d.get("needs_ocr")
              and (INTERIM / f"{d['doc_id']}.txt").exists()]

    samples = []
    if briefs:
        samples.append(("Brief sample", random.choice(briefs)))
    if reports:
        samples.append(("Report/GovResponse sample", random.choice(reports)))
    if non_en:
        samples.append(("Non-English sample", random.choice(non_en)))
    else:
        print("\n  (no non-English docs found)")

    for label, entry in samples:
        doc_id = entry["doc_id"]
        txt_path = INTERIM / f"{doc_id}.txt"
        text = txt_path.read_text(encoding="utf-8")
        words = text.split()
        snippet = " ".join(words[:120])
        print(f"\n[{label}]")
        print(f"  doc_id   : {doc_id}")
        print(f"  org      : {entry.get('organization','?')[:70]}")
        print(f"  study    : {entry.get('study_or_consultation_title','?')[:70]}")
        print(f"  language : {entry.get('language','?')}   words: {len(words):,}")
        print(f"  --- first 120 words ---")
        print(f"  {snippet}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Extract text from data/raw/ to data/interim/")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Process at most N documents")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse but do not write files or update manifest")
    args = ap.parse_args()

    INTERIM.mkdir(parents=True, exist_ok=True)

    entries = load_manifest()
    manifest_dirty = False

    extracted: list[str] = []
    skipped_ocr: list[str] = []
    skipped_exists = 0
    processed = 0

    for doc_id, entry in entries.items():
        if args.limit is not None and processed >= args.limit:
            break

        file_type = entry.get("file_type", "pdf")
        raw_path = RAW / f"{doc_id}.{file_type}"
        interim_path = INTERIM / f"{doc_id}.txt"

        # Idempotency: already extracted
        if interim_path.exists() and entry.get("language") is not None:
            skipped_exists += 1
            continue

        # Already flagged needs_ocr from a previous run
        if entry.get("needs_ocr") is True:
            skipped_ocr.append(doc_id)
            continue

        if not raw_path.exists():
            print(f"  MISSING {raw_path.name} — skipping")
            continue

        processed += 1

        if file_type == "pdf":
            text, needs_ocr = extract_pdf(raw_path)
            if needs_ocr:
                print(f"  OCR     {doc_id}")
                if not args.dry_run:
                    entry["needs_ocr"] = True
                    manifest_dirty = True
                skipped_ocr.append(doc_id)
                continue
        else:  # html
            html_text = extract_html(raw_path)
            pdf_url = entry.get("pdf_url")
            word_count = len(html_text.split())
            if word_count < HTML_SPARSE_WORDS:
                # DocumentViewer HTML is paginated — saved file is cover page only.
                if pdf_url:
                    print(f"  HTML sparse ({word_count} words) — fetching full PDF via pdf_url")
                    if args.dry_run:
                        print(f"  DRY-RUN   {doc_id} — would download pdf_url")
                        continue
                    text, needs_ocr = download_and_extract_pdf_url(pdf_url)
                    if needs_ocr:
                        print(f"  OCR     {doc_id}")
                        entry["needs_ocr"] = True
                        manifest_dirty = True
                        skipped_ocr.append(doc_id)
                        continue
                    if text is None:
                        print(f"  EMPTY   {doc_id} (pdf_url returned nothing)")
                        continue
                else:
                    print(f"  SPARSE  {doc_id} ({word_count} words, no pdf_url) — skipping")
                    continue
            else:
                text = html_text
                needs_ocr = False

        if not text or not text.strip():
            print(f"  EMPTY   {doc_id}")
            continue

        lang = detect_language(text)
        print(f"  EXTRACT {doc_id}  lang={lang}  words={len(text.split()):,}")

        if not args.dry_run:
            interim_path.write_text(text, encoding="utf-8")
            entry["language"] = lang
            entry["needs_ocr"] = False
            manifest_dirty = True

        extracted.append(doc_id)

    if manifest_dirty and not args.dry_run:
        save_manifest(entries)

    print_report(entries, extracted, skipped_ocr, skipped_exists)


if __name__ == "__main__":
    main()
