"""Stage 4: Clean text from data/interim/ into data/interim/clean/.

Reads  : data/interim/{doc_id}.txt
Writes : data/interim/clean/{doc_id}.txt         — cleaned plain text
         data/interim/clean/{doc_id}_masked.txt  — org-masked version

Pipeline (in order):
  1. Split on [[page N]] markers.
  2. Detect and strip repeating headers/footers (appear on >50 % of pages).
  3. Fix Unicode ligatures and hyphenated line-breaks.
  4. Normalize whitespace.
  5. Capture org acronyms defined in text (before boilerplate removal).
  6. Drop table-of-contents sections (dot-leader lines).
  7. Drop "About [Org]" boilerplate sections.
  8. hesa_report / gov_response only: strip front matter and back-matter
     appendices; preserve LIST OF RECOMMENDATIONS.
  9. Scrub contact info (emails, phones, postal codes, street addresses) → [CONTACT].
 10. Strip trailing signature blocks.
 11. Produce org-masked parallel version ([ORG] substitution).
 12. Detect layout_mangled (high short-line fraction AND high digit density).
 13. Drop docs under 150 words (set dropped_short=true in manifest).
 14. Manifest housekeeping: near-zero PDFs and garbled FASD doc → needs_ocr=true.

Usage:
  uv run python -m clean.clean_text [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

ROOT     = Path(__file__).parent.parent
INTERIM  = ROOT / "data" / "interim"
CLEAN    = INTERIM / "clean"
MANIFEST = ROOT / "manifest.jsonl"
RAW      = ROOT / "data" / "raw"

MIN_WORDS       = 150
HF_THRESHOLD    = 0.50   # fraction of pages a line must appear on to be header/footer
LAYOUT_SL_FRAC  = 0.40   # short-line fraction threshold for layout_mangled
LAYOUT_DIG_DENS = 0.02   # digit density threshold (measured before contact scrubbing)
SIG_TAIL_WORDS  = 200    # strip signature only if fewer words follow

FASD_DOC = "hesa441_canadas_health_workforce_0018"

LIGATURE_MAP = {
    # Standard Unicode ligatures (U+FB00–FB06)
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
    "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
    # PDF font-encoding corruption: glyph tables map ligature glyphs to wrong
    # Unicode codepoints in Latin Extended-B range (observed in HESA PDFs)
    "Ɵ": "ti",   # U+019F — most common: "recommendaƟons", "pracƟce"
    "ƞ": "tf",   # U+019E — "breasƞeeding"
    "Ʃ": "tt",   # U+01A9 — "aƩract"
    "ƫ": "tti",  # U+01AB — "geƫng"
    "Ư": "ff",   # U+01AF — "eƯorts"
    # Private-Use-Area bullet substitutions (Symbol/Wingdings fonts)
    "": "•", "": "•",
    # Bidirectional control characters and zero-width spaces — strip
    "​": "", "‬": "", "‭": "",
}

# ── compiled patterns ──────────────────────────────────────────────────────────

PAGE_MARKER_RE  = re.compile(r"^\[\[page \d+\]\]$")
PAGE_SPLIT_RE   = re.compile(r"\[\[page \d+\]\]")

EMAIL_RE   = re.compile(r"[\w.+%-]+@[\w-]+\.[\w.]+")
PHONE_RE   = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")
POSTAL_RE  = re.compile(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b")
ADDR_RE    = re.compile(
    r"\b\d+\s+(?:\w+\s+){0,3}"
    r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Road|Rd|"
    r"Lane|Ln|Place|Pl|Way|Court|Ct|Suite|Ste)\b",
    re.IGNORECASE,
)

SIG_RE = re.compile(
    r"(?im)^(sincerely|respectfully submitted|respectfully yours|"
    r"yours truly|regards|best regards|yours sincerely|"
    r"submitted respectfully|thank you for your consideration)[,.]?\s*$"
)

TOC_HDR_RE  = re.compile(r"(?i)^\s*(table\s+of\s+contents|contents)\s*$")
TOC_LINE_RE = re.compile(r"^.{3,}\.{5,}\s*\d+\s*$")
ROMAN_RE    = re.compile(r"^[ivxlcdmIVXLCDM]+$")

ABOUT_RE = re.compile(
    r"(?i)^about\s+(?!approximately|around|\d)\w[\w\s&,.()/\'-]{1,80}\s*$"
)

CONTENT_START_RE = re.compile(
    r"(?m)^(SUMMARY|INTRODUCTION|BACKGROUND|OVERVIEW|EXECUTIVE SUMMARY|"
    r"PREAMBLE|GOVERNMENT RESPONSE|CONTEXT|MANDATE|THE EVIDENCE|FINDINGS?)\s*$"
)

BACK_MATTER_RE = re.compile(
    r"(?im)^(APPENDIX\s+[A-Z0-9]|ANNEX\s+[A-Z0-9]|"
    r"LIST\s+OF\s+WITNESSES|DISSENTING\s+(REPORT|OPINION)|"
    r"SUPPLEMENTARY\s+(REPORT|OPINION)|MINORITY\s+OPINION|"
    r"BRIEFS\s+RECEIVED|WRITTEN\s+SUBMISSIONS\s+RECEIVED|"
    r"LIST\s+OF\s+(WRITTEN\s+)?SUBMISSIONS|LIST\s+OF\s+BRIEFS|"
    r"REQUEST\s+FOR\s+GOVERNMENT\s+RESPONSE)\b"
)

RECS_RE = re.compile(r"(?im)^list\s+of\s+recommendations\s*$")

_TITLE_STOP_RE = re.compile(
    r"(?i)^(report\s+of|standing\s+committee|parliament|speaker|"
    r"published\s+under|january|february|march|april|may|june|july|"
    r"august|september|october|november|december|\d{4}$|"
    r"[a-z]+,\s+chair|submitted\s+by|prepared\s+by)"
)


# ── page splitting / joining ───────────────────────────────────────────────────

def split_pages(text: str) -> tuple[list[str], list[str]]:
    markers = PAGE_SPLIT_RE.findall(text)
    parts   = PAGE_SPLIT_RE.split(text)
    return parts, markers


def rejoin_pages(parts: list[str], markers: list[str]) -> str:
    out = parts[0]
    for marker, part in zip(markers, parts[1:]):
        if out and not out.endswith("\n"):
            out += "\n"  # ensure marker is on its own line when HF stripping removes the preceding line
        out += marker + "\n" + part
    return out


# ── title-echo removal ────────────────────────────────────────────────────────

def _sig_words(text: str) -> frozenset[str]:
    """Significant words (5+ chars) after stripping punctuation and lowercasing."""
    normalized = re.sub(r"[^\w\s]", " ", text.lower())
    return frozenset(w for w in normalized.split() if len(w) >= 5)


def _extract_title_words(first_page: str) -> frozenset[str]:
    """Collect significant words from the first title block on page 1."""
    title_lines: list[str] = []
    for line in first_page.splitlines():
        s = line.strip()
        if not s:
            if title_lines:
                break
            continue
        if _TITLE_STOP_RE.match(s):
            break
        title_lines.append(s)
        if len(title_lines) >= 8:
            break
    return _sig_words(" ".join(title_lines))


def strip_title_echoes(parts: list[str], markers: list[str]) -> tuple[list[str], int]:
    """Remove running title echoes from pages 1+ (page 0 is kept intact).

    For each subsequent page, if the first 2-6 non-blank lines contain ≥80% of
    the title's significant words AND ≥80% of the window words are in the title,
    those lines are removed (greedy: smallest matching window wins).
    """
    if len(parts) < 2:
        return parts, 0

    title_words = _extract_title_words(parts[0])
    if len(title_words) < 4:
        return parts, 0

    result    = [parts[0]]
    echo_count = 0

    for part in parts[1:]:
        lines = part.splitlines()
        # Collect first 6 non-blank lines (by index)
        nonblank: list[tuple[int, str]] = []
        for i, line in enumerate(lines):
            if line.strip():
                nonblank.append((i, line))
                if len(nonblank) >= 6:
                    break

        removed = False
        if len(nonblank) >= 2:
            for size in range(2, len(nonblank) + 1):
                window    = nonblank[:size]
                win_text  = " ".join(l for _, l in window)
                win_words = _sig_words(win_text)
                if len(win_words) < 3:
                    continue
                in_title  = len(win_words & title_words)
                win_cov   = in_title / len(win_words)
                title_cov = in_title / len(title_words)
                if win_cov >= 0.80 and title_cov >= 0.80:
                    last_idx  = window[-1][0]
                    remainder = lines[last_idx + 1:]
                    while remainder and not remainder[0].strip():
                        remainder = remainder[1:]
                    result.append("\n".join(remainder))
                    echo_count += 1
                    removed = True
                    break

        if not removed:
            result.append(part)

    return result, echo_count


# ── header / footer ───────────────────────────────────────────────────────────

def _norm(line: str) -> str:
    """Lowercase + collapse digit runs to # — for HF fingerprinting."""
    return re.sub(r"\d+", "#", line.strip().lower())


def find_hf_norms(pages: list[str]) -> set[str]:
    n = len(pages)
    if n < 2:
        return set()
    counts: dict[str, int] = {}
    for page in pages:
        seen: set[str] = set()
        for line in page.splitlines():
            if not line.strip():
                continue
            nm = _norm(line)
            if nm not in seen:
                counts[nm] = counts.get(nm, 0) + 1
                seen.add(nm)
    return {nm for nm, c in counts.items() if c / n > HF_THRESHOLD}


def strip_page_hf(page: str, hf: set[str]) -> tuple[str, int]:
    kept, removed = [], 0
    for line in page.splitlines():
        if line.strip() and _norm(line) in hf:
            removed += 1
        else:
            kept.append(line)
    return "\n".join(kept), removed


# ── text normalisation ────────────────────────────────────────────────────────

_DIGIT7_RE = re.compile(r"(?<=[a-zA-Z])7(?=[a-zA-Z])")

def fix_ligatures(text: str) -> str:
    for lig, rep in LIGATURE_MAP.items():
        text = text.replace(lig, rep)
    # "7" used as "ti" ligature in some font encodings (byte 0x37 mapping).
    # Only replace when flanked by letters — avoids corrupting real numbers.
    text = _DIGIT7_RE.sub("ti", text)
    return text


def fix_hyphenation(text: str) -> str:
    return re.sub(r"(\w)-\n(\w)", r"\1\2", text)


def normalize_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip()


# ── boilerplate removal ───────────────────────────────────────────────────────

def drop_toc(text: str) -> str:
    """Remove ToC sections (dot-leader lines after a 'Table of Contents' header).
    Does NOT reset in_toc at page markers — ToC can span page boundaries."""
    lines  = text.splitlines()
    result: list[str] = []
    in_toc = False

    for line in lines:
        s = line.strip()
        if PAGE_MARKER_RE.match(s):
            result.append(line)      # always keep markers; do NOT reset in_toc
            continue
        if TOC_HDR_RE.match(s):
            in_toc = True
            continue
        if in_toc:
            if TOC_LINE_RE.match(s) or not s or s.isdigit() or ROMAN_RE.match(s):
                continue             # still in ToC
            in_toc = False           # first non-ToC line ends the section
        result.append(line)

    return "\n".join(result)


def drop_about_sections(text: str) -> str:
    """Remove 'About [Org]' boilerplate paragraphs.
    Skipping ends at a [[page N]] marker or after two consecutive blank lines."""
    lines  = text.splitlines()
    result: list[str] = []
    skip   = False
    blanks = 0

    for line in lines:
        s = line.strip()
        if PAGE_MARKER_RE.match(s):
            skip = False
            result.append(line)
            continue
        if ABOUT_RE.match(s):
            skip = True
            blanks = 0
            continue
        if skip:
            if not s:
                blanks += 1
                if blanks >= 2:
                    skip = False
            else:
                blanks = 0
            continue
        result.append(line)

    return "\n".join(result)


# ── report front / back matter ────────────────────────────────────────────────

def strip_front_matter(text: str) -> str:
    """Strip pages before the first substantive content heading."""
    m = CONTENT_START_RE.search(text)
    if m:
        return text[m.start():]
    return text


def strip_back_matter(text: str) -> str:
    """Strip appendices, witness lists, etc.
    Searches only AFTER the list of recommendations so that section is preserved."""
    rec   = RECS_RE.search(text)
    start = rec.end() if rec else 0
    back  = BACK_MATTER_RE.search(text, start)
    if back:
        return text[: back.start()].rstrip()
    return text


# ── contact scrubbing + signature ─────────────────────────────────────────────

def scrub_contacts(text: str) -> tuple[str, int]:
    count = 0

    def _rep(m: re.Match) -> str:
        nonlocal count
        count += 1
        return "[CONTACT]"

    text = EMAIL_RE.sub(_rep, text)
    text = PHONE_RE.sub(_rep, text)
    text = POSTAL_RE.sub(_rep, text)
    text = ADDR_RE.sub(_rep, text)
    return text, count


def strip_signature(text: str) -> str:
    """Strip the last signature salutation to end, only if it is near the end."""
    total = len(text.split())
    best: re.Match | None = None
    for m in SIG_RE.finditer(text):
        tail = len(text[m.start() :].split())
        if tail < min(SIG_TAIL_WORDS, max(1, int(total * 0.10))):
            best = m
    if best:
        return text[: best.start()].rstrip()
    return text


# ── org masking ───────────────────────────────────────────────────────────────

def _parse_org_names(organization: str | None) -> list[str]:
    if not organization:
        return []
    return [o.strip() for o in organization.split(" / ") if o.strip()]


def find_acronyms(text: str, org_names: list[str]) -> list[str]:
    """Find uppercase acronyms defined in text as 'Org Name (ACR)'."""
    found: list[str] = []
    for org in org_names:
        pat = re.compile(re.escape(org) + r"\s*\(([A-Z][A-Z0-9]{1,7})\)", re.IGNORECASE)
        for m in pat.finditer(text):
            acr = m.group(1)
            if acr not in found:
                found.append(acr)
    return found


def mask_orgs(text: str, names: list[str]) -> str:
    """Replace org names + acronyms with [ORG] (longest first to avoid partial hits)."""
    for name in sorted(names, key=len, reverse=True):
        if len(name) < 3:
            continue
        pat = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
        text = pat.sub("[ORG]", text)
    return text


# ── layout-mangled detection ──────────────────────────────────────────────────

def detect_layout_mangled(text: str) -> bool:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    short_frac = sum(1 for l in lines if len(l.split()) < 4) / len(lines)
    nospace    = text.replace(" ", "").replace("\n", "")
    dig_dens   = sum(1 for c in nospace if c.isdigit()) / max(len(nospace), 1)
    return short_frac > LAYOUT_SL_FRAC and dig_dens > LAYOUT_DIG_DENS


# ── per-doc pipeline ──────────────────────────────────────────────────────────

@dataclass
class CleanResult:
    cleaned:        str
    masked:         str | None   # None for non-hesa_brief docs
    hf_removed:     int
    title_echoes:   int
    contact_count:  int
    layout_mangled: bool
    dropped_short:  bool
    words_before:   int
    words_after:    int


def clean_doc(text: str, source_type: str, organization: str | None) -> CleanResult:
    words_before = len(text.split())

    # 1. Split into pages
    parts, markers = split_pages(text)

    # 2. Strip repeating headers/footers
    hf = find_hf_norms(parts)
    hf_removed = 0
    clean_parts: list[str] = []
    for part in parts:
        p, n = strip_page_hf(part, hf)
        clean_parts.append(p)
        hf_removed += n

    # 2b. Strip running title echoes (multi-line title at top of content pages)
    clean_parts, title_echoes = strip_title_echoes(clean_parts, markers)

    text = rejoin_pages(clean_parts, markers)

    # 3. Text normalisation
    text = fix_ligatures(text)
    text = fix_hyphenation(text)
    text = normalize_whitespace(text)

    # 4. Capture acronyms BEFORE About removal (definition often lives there)
    org_names     = _parse_org_names(organization)
    acronyms      = find_acronyms(text, org_names)
    names_to_mask = org_names + acronyms

    # 5. Drop ToC and About boilerplate
    text = drop_toc(text)
    text = drop_about_sections(text)

    # 6. Report / gov_response front & back matter
    if source_type in ("hesa_report", "gov_response"):
        text = strip_front_matter(text)
        text = strip_back_matter(text)

    # 7. Layout-mangled flag BEFORE contact scrubbing (phone digits still present)
    layout_mangled = detect_layout_mangled(text)

    # 8. Contacts + signature
    text, contact_count = scrub_contacts(text)
    text = strip_signature(text)

    # 9. Final whitespace pass
    text = normalize_whitespace(text)

    # 10. Word count
    words_after   = len(text.split())
    dropped_short = words_after < MIN_WORDS

    # 11. Org masking (hesa_brief only — reports have no single submitting org)
    masked = mask_orgs(text, names_to_mask) if source_type == "hesa_brief" else None

    return CleanResult(
        cleaned=text,
        masked=masked,
        hf_removed=hf_removed,
        title_echoes=title_echoes,
        contact_count=contact_count,
        layout_mangled=layout_mangled,
        dropped_short=dropped_short,
        words_before=words_before,
        words_after=words_after,
    )


# ── manifest ──────────────────────────────────────────────────────────────────

def load_manifest() -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            obj = json.loads(line)
            entries[obj["doc_id"]] = obj
    return entries


def save_manifest(entries: dict[str, dict]) -> None:
    with MANIFEST.open("w", encoding="utf-8") as f:
        for entry in entries.values():
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── manifest housekeeping ─────────────────────────────────────────────────────

def fix_needs_ocr(entries: dict[str, dict]) -> int:
    """Set needs_ocr=true for the garbled FASD doc and near-zero single-page PDFs."""
    fixed = 0
    for doc_id, entry in entries.items():
        if entry.get("needs_ocr") is True:
            continue
        if doc_id == FASD_DOC:
            entry["needs_ocr"] = True
            fixed += 1
            continue
        if entry.get("file_type", "pdf") != "pdf":
            continue
        # PDF with raw file but no interim text → scanned single-page image
        if (RAW / f"{doc_id}.pdf").exists() and not (INTERIM / f"{doc_id}.txt").exists():
            entry["needs_ocr"] = True
            fixed += 1
    return fixed


# ── summary report ────────────────────────────────────────────────────────────

def print_report(
    results: list[tuple[str, CleanResult]],
    entries: dict[str, dict],
    skipped: int,
) -> None:
    all_r   = [r for _, r in results]
    dropped = [(d, r) for d, r in results if r.dropped_short]
    mangled = [(d, r) for d, r in results if r.layout_mangled]

    total_contacts  = sum(r.contact_count  for r in all_r)
    total_echoes    = sum(r.title_echoes   for r in all_r)
    docs_with_echoes = sum(1 for r in all_r if r.title_echoes > 0)
    docs_with_hf    = sum(1 for r in all_r if r.hf_removed > 0)
    total_hf        = sum(r.hf_removed     for r in all_r)
    avg_hf          = total_hf / docs_with_hf if docs_with_hf else 0.0

    before = sorted(r.words_before for r in all_r)
    after  = sorted(r.words_after  for r in all_r if not r.dropped_short)

    def pct(lst: list[int], p: float) -> int:
        return lst[int(len(lst) * p)] if lst else 0

    print("\n" + "=" * 60)
    print("CLEANING SUMMARY")
    print("=" * 60)
    print(f"  Cleaned this run        : {len(all_r)}")
    print(f"  Skipped (already clean) : {skipped}")
    print(f"  Dropped (< {MIN_WORDS} words)  : {len(dropped)}")
    print(f"  layout_mangled          : {len(mangled)}")
    print(f"  Total [CONTACT] subs    : {total_contacts}")
    print(f"  Title echoes removed    : {total_echoes}  ({docs_with_echoes} docs)")
    print(f"  Avg hf lines removed    : {avg_hf:.1f}  ({total_hf} total, {docs_with_hf} docs)")

    print("\nWord-count percentiles (before → after, non-dropped):")
    for label, p in [("p10", 0.10), ("median", 0.50), ("p90", 0.90)]:
        print(f"  {label:<8}  {pct(before, p):>7,}  →  {pct(after, p):>7,}")

    print(f"\nDropped-short ({len(dropped)}):")
    for doc_id, r in dropped:
        e = entries.get(doc_id, {})
        print(f"  {doc_id}  ({r.words_after} words)  [{e.get('source_type','')}]")

    print(f"\nlayout_mangled ({len(mangled)}):")
    for doc_id, r in mangled:
        e = entries.get(doc_id, {})
        print(f"  {doc_id}  [{e.get('source_type','')}]  words={r.words_after}")


def print_samples(entries: dict[str, dict], results: list[tuple[str, CleanResult]]) -> None:
    CDA_ID    = "hesa441_canadas_health_workforce_0001"
    REPORT_ID = "hesa441_breast_cancer_screening_guid_0016"

    result_map = {d: r for d, r in results}

    # Pick a random brief that is not CDA, not dropped, not mangled
    random.seed(42)
    candidates = [
        d for d, r in results
        if entries.get(d, {}).get("source_type") == "hesa_brief"
        and d != CDA_ID
        and not r.dropped_short
        and not r.layout_mangled
    ]
    random_id = random.choice(candidates) if candidates else None

    def _show(label: str, doc_id: str | None, masked: bool = False, cap: int | None = None) -> None:
        if doc_id is None:
            print(f"\n[{label}] — no eligible document found")
            return
        suffix = "_masked" if masked else ""
        path   = CLEAN / f"{doc_id}{suffix}.txt"
        if not path.exists():
            print(f"\n[{label}] — file missing: {path.name}")
            return
        text   = path.read_text(encoding="utf-8")
        words  = text.split()
        e      = entries.get(doc_id, {})
        r      = result_map.get(doc_id)
        print(f"\n{'─'*60}")
        print(f"[{label}]")
        print(f"  doc_id : {doc_id}")
        print(f"  org    : {e.get('organization', '?')[:70]}")
        print(f"  type   : {e.get('source_type', '?')}")
        if r:
            print(f"  words  : {r.words_before} → {r.words_after}  hf_removed={r.hf_removed}  contacts={r.contact_count}")
        if cap and len(words) > cap:
            print(f"  (showing first {cap} of {len(words):,} words — full text in {path.name})")
            print("\n" + " ".join(words[:cap]) + " …")
        else:
            print(f"  words  : {len(words):,}")
            print()
            print(text)

    print("\n" + "=" * 60)
    print("SAMPLES")
    print("=" * 60)
    _show("CDA brief — plain text",    CDA_ID,     masked=False)
    _show("CDA brief — org masked",    CDA_ID,     masked=True)
    _show("Report sample",             REPORT_ID,  masked=False, cap=500)
    _show("Random brief",              random_id,  masked=False)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Clean interim/ text into interim/clean/")
    ap.add_argument("--limit",   type=int, default=None, metavar="N")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force",   action="store_true", help="Overwrite existing clean files")
    args = ap.parse_args()

    CLEAN.mkdir(parents=True, exist_ok=True)

    entries = load_manifest()
    manifest_dirty = False

    # Fix 2 cleanup: delete masked files for non-brief source types
    if not args.dry_run:
        deleted_masked = 0
        for masked_path in CLEAN.glob("*_masked.txt"):
            doc_id_from_path = masked_path.stem[:-7]  # strip "_masked"
            src_type = entries.get(doc_id_from_path, {}).get("source_type", "")
            if src_type and src_type != "hesa_brief":
                masked_path.unlink()
                deleted_masked += 1
        if deleted_masked:
            print(f"  Deleted {deleted_masked} non-brief masked files")

    # Manifest housekeeping
    fixed_ocr = fix_needs_ocr(entries)
    if fixed_ocr:
        manifest_dirty = True
        print(f"  OCR-flagged {fixed_ocr} additional docs in manifest")

    results:   list[tuple[str, CleanResult]] = []
    skipped    = 0
    processed  = 0

    for doc_id, entry in entries.items():
        if args.limit is not None and processed >= args.limit:
            break

        # Skip docs without extractable text
        if entry.get("needs_ocr") or entry.get("language") is None:
            continue

        src  = INTERIM / f"{doc_id}.txt"
        dest = CLEAN   / f"{doc_id}.txt"

        if not src.exists():
            continue

        if dest.exists() and not args.force:
            skipped += 1
            continue

        processed += 1
        text = src.read_text(encoding="utf-8")

        result = clean_doc(
            text=text,
            source_type=entry.get("source_type", ""),
            organization=entry.get("organization"),
        )

        tag = ("DROP" if result.dropped_short
               else "MANG" if result.layout_mangled
               else "OK  ")
        print(
            f"  {tag} {doc_id}"
            f"  hf={result.hf_removed}"
            f"  echo={result.title_echoes}"
            f"  contacts={result.contact_count}"
            f"  words={result.words_before}→{result.words_after}"
        )

        if not args.dry_run:
            dest.write_text(result.cleaned, encoding="utf-8")
            if result.masked is not None:
                (CLEAN / f"{doc_id}_masked.txt").write_text(result.masked, encoding="utf-8")
            entry["layout_mangled"] = result.layout_mangled
            entry["dropped_short"]  = result.dropped_short
            manifest_dirty = True

        results.append((doc_id, result))

    if manifest_dirty and not args.dry_run:
        save_manifest(entries)

    print_report(results, entries, skipped)

    if not args.dry_run and results:
        print_samples(entries, results)


if __name__ == "__main__":
    main()
