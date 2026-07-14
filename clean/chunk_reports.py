"""Stage 7: chunk hesa_report, gov_response, policy, statcan_analysis docs.

For each eligible document in manifest.jsonl:
  - Read cleaned text from data/interim/clean/{doc_id}.txt
  - Chunk at heading boundaries (ALL-CAPS headings + Recommendation N lines)
    into ~300–800 token pieces; large sections split with 50-word overlap
  - Each chunk carries doc_id, section_title, page_start/end (from [[page N]])

Special extraction for hesa_report:
  - LIST OF RECOMMENDATIONS → one chunk per recommendation, page-ref dots
    stripped, marked is_recommendation=True
  - Body "Recommendation N" sections → regular body chunks (in-context
    discussion; not marked is_recommendation)

Special extraction for gov_response:
  - Individual "Recommendation N" response blocks → is_recommendation=True
  - Grouped "Recommendations (N, M, …)" headers → treated as section
    boundaries; rec_numbers lists all covered recommendations
  - Preamble text before first recommendation → chunked normally

Output: data/processed/rag_chunks.jsonl (append-mode per run; use --rebuild
to clear first)

Usage:
  uv run python -m clean.chunk_reports [--limit N] [--dry-run] [--rebuild]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT      = Path(__file__).parent.parent
CLEAN     = ROOT / "data" / "interim" / "clean"
PROCESSED = ROOT / "data" / "processed"
MANIFEST  = ROOT / "manifest.jsonl"

ELIGIBLE_TYPES = {"hesa_report", "gov_response", "policy", "statcan_analysis"}

# Token approximation: English text ≈ 1.33 tokens/word
TARGET_MAX_WORDS = 600    # ≈ 800 tokens
OVERLAP_WORDS    = 50

PAGE_RE        = re.compile(r"^\[\[page (\d+)\]\]\s*$")
REC_RE         = re.compile(r"^(Recommendation)\s+(\d+)\s*(?::|$)", re.IGNORECASE)
REC_RANGE_RE   = re.compile(r"^Recommendations?\s+(\d+)\s*(?:to|-|–)\s*(\d+)", re.IGNORECASE)
REC_GROUP_RE   = re.compile(r"^Recommendations?\s*\(([^)]+)\)\s*$", re.IGNORECASE)
PAGE_REF_RE    = re.compile(r"\s*\.{4,}\s*\d+\s*$")   # trailing " ........... 25"
LIST_OF_RECS_RE = re.compile(r"^\s*LIST\s+OF\s+RECOMMENDATIONS?\s*$", re.IGNORECASE)

HEADING_MAX_LEN       = 120
HEADING_MIN_UPPER_FRAC = 0.70
HEADING_MIN_ALPHA      = 4


# ── helpers ───────────────────────────────────────────────────────────────────

def _approx_tokens(text: str) -> int:
    return int(len(text.split()) * 1.33)


def _is_all_caps_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > HEADING_MAX_LEN:
        return False
    alpha = [c for c in s if c.isalpha()]
    if len(alpha) < HEADING_MIN_ALPHA:
        return False
    return sum(1 for c in alpha if c.isupper()) / len(alpha) >= HEADING_MIN_UPPER_FRAC


def _parse_pages(text: str) -> list[tuple[int, str]]:
    """Return (page_number, line) pairs; page 0 = before first [[page N]]."""
    result: list[tuple[int, str]] = []
    current = 0
    for line in text.splitlines():
        m = PAGE_RE.match(line)
        if m:
            current = int(m.group(1))
        else:
            result.append((current, line))
    return result


def _rec_nums_from_group(s: str) -> list[int]:
    """Parse 'Recommendations (1, 2, 3 and 4)' or 'Recommendations 5 to 8' → list."""
    stripped = s.strip()
    # Range form: "Recommendations 5 to 8"
    m_range = REC_RANGE_RE.match(stripped)
    if m_range:
        return list(range(int(m_range.group(1)), int(m_range.group(2)) + 1))
    # Parenthetical form: "Recommendations (1, 2, 3 and 4)"
    m_grp = REC_GROUP_RE.match(stripped)
    if m_grp:
        inner = re.sub(r"\band\b|\bet\b|[.,;]", " ", m_grp.group(1), flags=re.IGNORECASE)
        return [int(n) for n in re.findall(r"\d+", inner)]
    return []


# ── section / chunk building ──────────────────────────────────────────────────

class _Section:
    __slots__ = ("title", "page_start", "page_end", "lines",
                 "is_recommendation", "rec_numbers")

    def __init__(self, title: str, page_start: int) -> None:
        self.title           = title
        self.page_start      = page_start
        self.page_end        = page_start
        self.lines: list[str] = []
        self.is_recommendation = False
        self.rec_numbers: list[int] = []

    def add(self, page: int, line: str) -> None:
        self.lines.append(line)
        if page > self.page_end:
            self.page_end = page

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def _sections_to_chunks(sections: list[_Section], doc_id: str, source_type: str) -> list[dict]:
    """Convert sections to output chunk dicts, splitting large sections."""
    chunks: list[dict] = []
    seq = [0]

    def _emit(sec: _Section, text: str) -> None:
        seq[0] += 1
        prefix = "rec" if sec.is_recommendation else f"{seq[0]:04d}"
        if sec.is_recommendation and sec.rec_numbers:
            prefix = f"rec_{sec.rec_numbers[0]:03d}"
        chunk_id = f"{doc_id}_{prefix}"
        chunks.append({
            "chunk_id":         chunk_id,
            "doc_id":           doc_id,
            "source_type":      source_type,
            "section_title":    sec.title,
            "page_start":       sec.page_start,
            "page_end":         sec.page_end,
            "token_approx":     _approx_tokens(text),
            "text":             text,
            "is_recommendation": sec.is_recommendation,
            "rec_numbers":      sec.rec_numbers,
        })

    for sec in sections:
        if not sec.text:
            continue
        if sec.is_recommendation:
            _emit(sec, sec.text)
            continue

        words = sec.text.split()
        if len(words) <= TARGET_MAX_WORDS:
            _emit(sec, sec.text)
        else:
            # Split with overlap; page range applies to all sub-chunks
            offset = 0
            while offset < len(words):
                chunk_words = words[offset : offset + TARGET_MAX_WORDS]
                _emit(sec, " ".join(chunk_words))
                if offset + TARGET_MAX_WORDS >= len(words):
                    break
                offset += TARGET_MAX_WORDS - OVERLAP_WORDS

    return chunks


# ── hesa_report chunker ───────────────────────────────────────────────────────

def _chunk_hesa_report(text: str, doc_id: str, source_type: str) -> list[dict]:
    page_lines = _parse_pages(text)

    # ── Find LIST OF RECOMMENDATIONS section ──────────────────────────────────
    lor_idx  = None   # index into page_lines where LIST OF RECS heading is
    body_idx = None   # index where body starts (next heading after LOR)

    for i, (_, line) in enumerate(page_lines):
        if LIST_OF_RECS_RE.match(line.strip()):
            lor_idx = i
        elif lor_idx is not None and i > lor_idx + 2 and _is_all_caps_heading(line):
            body_idx = i
            break

    sections: list[_Section] = []

    # ── Extract individual recommendations from LOR ───────────────────────────
    if lor_idx is not None:
        lor_end = body_idx if body_idx is not None else len(page_lines)
        lor_pages = [p for p, _ in page_lines[lor_idx:lor_end] if p > 0]
        lor_page_start = lor_pages[0]  if lor_pages else 0
        lor_page_end   = lor_pages[-1] if lor_pages else 0

        current_rec: _Section | None = None
        for _, line in page_lines[lor_idx + 1 : lor_end]:
            m = REC_RE.match(line.strip())
            if m:
                if current_rec is not None:
                    sections.append(current_rec)
                rec_num = int(m.group(2))
                current_rec = _Section(f"Recommendation {rec_num}", lor_page_start)
                current_rec.page_end = lor_page_end
                current_rec.is_recommendation = True
                current_rec.rec_numbers = [rec_num]
                current_rec.add(lor_page_start, line.strip())
            elif current_rec is not None:
                # Strip trailing page-reference dots
                clean = PAGE_REF_RE.sub("", line).rstrip()
                if clean:
                    current_rec.add(lor_page_start, clean)
        if current_rec is not None:
            sections.append(current_rec)

    # ── Chunk body text ───────────────────────────────────────────────────────
    body_start = body_idx if body_idx is not None else (
        0 if lor_idx is None else lor_idx
    )
    current: _Section = _Section("", 0)

    for page, line in page_lines[body_start:]:
        stripped = line.strip()
        if _is_all_caps_heading(stripped) and not LIST_OF_RECS_RE.match(stripped):
            if current.text:
                sections.append(current)
            current = _Section(stripped, page)
        elif REC_RE.match(stripped):
            # Body occurrence of "Recommendation N" — section boundary but NOT
            # marked is_recommendation (rec chunks come from LOR above)
            if current.text:
                sections.append(current)
            rec_num = int(REC_RE.match(stripped).group(2))
            current = _Section(f"Recommendation {rec_num}", page)
        else:
            current.add(page, line)

    if current.text:
        sections.append(current)

    return _sections_to_chunks(sections, doc_id, source_type)


# ── gov_response chunker ──────────────────────────────────────────────────────

def _chunk_gov_response(text: str, doc_id: str, source_type: str) -> list[dict]:
    page_lines = _parse_pages(text)

    def _is_rec_boundary(s: str) -> tuple[bool, list[int]]:
        """Return (is_boundary, rec_numbers). Handles individual, range, grouped."""
        m_rng = REC_RANGE_RE.match(s)
        if m_rng:
            return True, list(range(int(m_rng.group(1)), int(m_rng.group(2)) + 1))
        m_grp = REC_GROUP_RE.match(s)
        if m_grp:
            return True, _rec_nums_from_group(s)
        m_rec = REC_RE.match(s)
        if m_rec:
            return True, [int(m_rec.group(2))]
        return False, []

    # Check if doc has any rec-structured boundaries
    has_rec_structure = any(
        _is_rec_boundary(ln.strip())[0]
        for _, ln in page_lines
    )

    sections: list[_Section] = []
    current: _Section = _Section("Preamble", 0)

    for page, line in page_lines:
        stripped = line.strip()
        is_rec_bnd, rec_nums = _is_rec_boundary(stripped)

        if has_rec_structure and is_rec_bnd:
            if current.text:
                sections.append(current)
            title = f"Recommendation {rec_nums[0]}" if len(rec_nums) == 1 else stripped
            current = _Section(title, page)
            current.is_recommendation = True
            current.rec_numbers = rec_nums
            current.add(page, line)
        elif _is_all_caps_heading(stripped):
            if current.text:
                sections.append(current)
            current = _Section(stripped, page)
        else:
            current.add(page, line)

    if current.text:
        sections.append(current)

    return _sections_to_chunks(sections, doc_id, source_type)


# ── generic chunker (policy, statcan_analysis) ────────────────────────────────

def _chunk_generic(text: str, doc_id: str, source_type: str) -> list[dict]:
    page_lines = _parse_pages(text)
    sections: list[_Section] = []
    current = _Section("", 0)

    for page, line in page_lines:
        stripped = line.strip()
        if _is_all_caps_heading(stripped):
            if current.text:
                sections.append(current)
            current = _Section(stripped, page)
        else:
            current.add(page, line)

    if current.text:
        sections.append(current)

    return _sections_to_chunks(sections, doc_id, source_type)


# ── dispatch ──────────────────────────────────────────────────────────────────

def chunk_document(text: str, doc_id: str, source_type: str) -> list[dict]:
    if source_type == "hesa_report":
        return _chunk_hesa_report(text, doc_id, source_type)
    elif source_type == "gov_response":
        return _chunk_gov_response(text, doc_id, source_type)
    else:
        return _chunk_generic(text, doc_id, source_type)


# ── manifest I/O ─────────────────────────────────────────────────────────────

def load_manifest() -> list[dict]:
    return [
        json.loads(line)
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Chunk reports for RAG (Stage 7)")
    ap.add_argument("--limit",   type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true",
                    help="Delete existing rag_chunks.jsonl before writing")
    args = ap.parse_args()

    out_path = PROCESSED / "rag_chunks.jsonl"

    if args.rebuild and not args.dry_run and out_path.exists():
        out_path.unlink()
        print("  Deleted existing rag_chunks.jsonl")

    entries = [
        e for e in load_manifest()
        if e.get("source_type") in ELIGIBLE_TYPES
    ]
    if args.limit:
        entries = entries[: args.limit]

    total_chunks = 0
    total_recs   = 0
    doc_stats: list[tuple[str, str, int, int]] = []

    for entry in entries:
        doc_id      = entry["doc_id"]
        source_type = entry["source_type"]
        path        = CLEAN / f"{doc_id}.txt"

        if not path.exists() or path.stat().st_size < 200:
            print(f"  SKIP {doc_id}  (missing or too small)")
            continue

        text   = path.read_text(encoding="utf-8")
        chunks = chunk_document(text, doc_id, source_type)

        n_recs = sum(1 for c in chunks if c["is_recommendation"])
        doc_stats.append((doc_id, source_type, len(chunks), n_recs))
        total_chunks += len(chunks)
        total_recs   += n_recs

        if not args.dry_run:
            with out_path.open("a", encoding="utf-8") as f:
                for c in chunks:
                    f.write(json.dumps(c, ensure_ascii=False) + "\n")

    # ── report ────────────────────────────────────────────────────────────────
    print(f"\n{'doc_id':<55} {'type':<14} {'chunks':>6}  {'recs':>4}")
    print("  " + "-" * 82)
    for doc_id, stype, n, r in doc_stats:
        print(f"  {doc_id:<55} {stype:<14} {n:>6}  {r:>4}")

    print(f"\n  Total chunks : {total_chunks}")
    print(f"  Rec chunks   : {total_recs}")
    if not args.dry_run:
        print(f"  Written to   : {out_path.relative_to(ROOT)}")
    else:
        print("  [dry-run] No files written.")


if __name__ == "__main__":
    main()
