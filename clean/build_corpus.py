"""Stage 6: Build data/processed/corpus.csv.

Reads:
  manifest.jsonl, topic_rules.yaml, org_lookup.csv
  data/interim/clean/{doc_id}.txt        — plain cleaned text
  data/interim/clean/{doc_id}_masked.txt — org-masked text (hesa_brief only)

Writes (default run):
  data/processed/corpus.csv

Corpus columns:
  doc_id, Name, org, source_type, date, language,
  study_title,        — study/consultation title; for stratification only, never in model input
  card_text,          — doc's own title + first 250 masked words; the classifier input unit
  text_org_masked_path, full_text_path,
  topic_seed, stakeholder_type, label_source, near_dup_cluster

Inclusion rules:
  - source_type == hesa_brief  (reports/gov_responses go to RAG only)
  - not duplicate_of
  - not dropped_short
  - not needs_ocr
  - language is set

Labeling strategy (content-first):
  1. Match topic_rules.yaml against card_text; require ≥2 distinct keyword hits.
  2. If no content label, fall back to study-title matching (1+ hit, exempt studies skipped).
  3. Else: unlabeled.

Card construction:
  Name      = document's own title (first non-blank text block from masked file)
  card_text = first 250 words of masked text   — NO study title injected

Usage:
  uv run python -m clean.build_corpus [--card simple|tiered] [--compare] [--dry-run]

  --compare  Run both the old title-first and new content-first labelers over all docs;
             print the disagreement matrix, new counts, and 20 moved-label samples.
             Does NOT write corpus.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT      = Path(__file__).parent.parent
CLEAN     = ROOT / "data" / "interim" / "clean"
PROCESSED = ROOT / "data" / "processed"
MANIFEST  = ROOT / "manifest.jsonl"
RULES_F   = ROOT / "topic_rules.yaml"
ORG_LU_F  = ROOT / "org_lookup.csv"

CORPUS_COLS = [
    "doc_id", "Name", "org", "source_type", "date", "language",
    "study_title",
    "card_text", "text_org_masked_path", "full_text_path",
    "topic_seed", "stakeholder_type", "label_source", "near_dup_cluster",
]

CARD_WORDS   = 250
LABEL_SOURCE = "weak"

_INDIVIDUAL_RE = re.compile(
    r"^[A-Za-záàâäéèêëíìîïóòôöúùûüÉÈÊÀÂÄÔÛÙ\-]+"
    r",\s+"
    r"[A-Za-záàâäéèêëíìîïóòôöúùûü]",
)


# ── loaders ───────────────────────────────────────────────────────────────────

def load_manifest() -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            obj = json.loads(line)
            entries[obj["doc_id"]] = obj
    return entries


def load_rules() -> dict:
    with RULES_F.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_org_lookup() -> dict[str, str]:
    lu: dict[str, str] = {}
    if not ORG_LU_F.exists():
        return lu
    with ORG_LU_F.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            lu[row["org"].strip()] = row["stakeholder_type"].strip()
    return lu


# ── card construction ─────────────────────────────────────────────────────────

def _doc_title(masked_text: str, max_lines: int = 4) -> str:
    """First block of non-empty lines (up to max_lines or first blank gap)."""
    title_lines: list[str] = []
    for line in masked_text.splitlines():
        s = line.strip()
        if not s or s.startswith("[[page"):
            if title_lines:
                break
            continue
        title_lines.append(s)
        if len(title_lines) >= max_lines:
            break
    title = " ".join(title_lines)
    return title[:200]  # cap to avoid pathological first blocks


def _first_n_words(text: str, n: int) -> str:
    clean = re.sub(r"\[\[page \d+\]\]", " ", text)
    return " ".join(clean.split()[:n])


def make_card_simple(masked_text: str) -> str:
    """First 250 words of masked text. Doc's own title is the first line — no study title."""
    return _first_n_words(masked_text, CARD_WORDS)


def make_card_tiered(masked_text: str) -> str:
    title = _doc_title(masked_text)
    body  = _first_n_words(masked_text, CARD_WORDS)
    return f"[ORG] [ORG]\n[TITLE] {title}\n\n{body}"


# ── topic matching ─────────────────────────────────────────────────────────────

_Compiled = list[tuple[str, list[re.Pattern], list[re.Pattern]]]


def _compile_rules(rules: dict) -> _Compiled:
    """Return list of (code, strong_patterns, regular_patterns) per topic."""
    compiled: _Compiled = []
    for topic in rules.get("topics", []):
        def _pats(kw_list: list) -> list[re.Pattern]:
            out: list[re.Pattern] = []
            for kw in kw_list:
                if kw.startswith("re:"):
                    out.append(re.compile(kw[3:], re.IGNORECASE))
                else:
                    out.append(re.compile(re.escape(kw), re.IGNORECASE))
            return out
        compiled.append((
            topic["code"],
            _pats(topic.get("strong_keywords", [])),
            _pats(topic.get("keywords", [])),
        ))
    return compiled


def _match_any(text: str, compiled: _Compiled) -> str | None:
    """First topic with ≥1 match in strong_keywords or keywords."""
    for code, strong_pats, regular_pats in compiled:
        if any(p.search(text) for p in strong_pats):
            return code
        if any(p.search(text) for p in regular_pats):
            return code
    return None


def _match_content_first(text: str, compiled: _Compiled) -> str | None:
    """First topic with ≥1 strong keyword OR ≥2 distinct regular keyword matches."""
    for code, strong_pats, regular_pats in compiled:
        if any(p.search(text) for p in strong_pats):
            return code
        if sum(1 for p in regular_pats if p.search(text)) >= 2:
            return code
    return None


# ── two labeling strategies ───────────────────────────────────────────────────

def assign_topic_title_first(
    entry: dict,
    compiled: _Compiled,
    exempt_lower: set[str],
    fallback: str,
    card_text: str,
) -> str:
    """Old strategy: study title (1+ match) → card_text (1+ match) → fallback."""
    study = (entry.get("study_or_consultation_title") or "").lower()
    if study and study not in exempt_lower:
        hit = _match_any(study, compiled)
        if hit:
            return hit
    hit = _match_any(card_text.lower(), compiled)
    if hit:
        return hit
    return fallback


def assign_topic_content_first(
    entry: dict,
    compiled: _Compiled,
    exempt_lower: set[str],
    fallback: str,
    card_text: str,
) -> str:
    """New strategy: card_text (≥1 strong OR ≥2 regular) → study title (1+) → fallback."""
    hit = _match_content_first(card_text.lower(), compiled)
    if hit:
        return hit
    study = (entry.get("study_or_consultation_title") or "").lower()
    if study and study not in exempt_lower:
        hit = _match_any(study, compiled)
        if hit:
            return hit
    return fallback


# ── stakeholder ───────────────────────────────────────────────────────────────

def assign_stakeholder(org: str | None, org_lookup: dict[str, str]) -> str | None:
    if not org:
        return None
    if org in org_lookup:
        return org_lookup[org]
    for comp in (o.strip() for o in org.split(" / ")):
        if comp in org_lookup:
            return org_lookup[comp]
    if _INDIVIDUAL_RE.match(org):
        return "individual"
    return None


# ── comparison report ─────────────────────────────────────────────────────────

def run_compare(
    rows: list[dict],
    rules: dict,
) -> None:
    """Print disagreement matrix, new per-topic counts, and 20 moved-label samples."""
    code_to_name = {t["code"]: t["name"] for t in rules.get("topics", [])}
    code_to_name["unlabeled"] = "unlabeled"

    old_labels = [r["_label_title_first"] for r in rows]
    new_labels = [r["_label_content_first"] for r in rows]

    moved = [r for r in rows if r["_label_title_first"] != r["_label_content_first"]]

    # ── 1. Disagreement matrix ────────────────────────────────────────────────
    all_codes = sorted({l for l in old_labels + new_labels})
    matrix: dict[tuple[str, str], int] = Counter(
        (o, n) for o, n in zip(old_labels, new_labels) if o != n
    )

    print("\n" + "=" * 70)
    print("LABELING COMPARISON")
    print("=" * 70)
    print(f"  Total docs         : {len(rows)}")
    print(f"  Label unchanged    : {len(rows) - len(moved)}")
    print(f"  Label moved        : {len(moved)}")

    print(f"\n{'':30} {'NEW LABEL →':}")
    header_codes = [c for c in all_codes if any(
        matrix.get((o, c), 0) + matrix.get((c, n), 0) > 0
        for o in all_codes for n in all_codes
    )]
    # Print all codes that appear in either column
    new_codes = sorted({n for _, n in matrix})
    old_codes = sorted({o for o, _ in matrix})
    all_move_codes = sorted(set(old_codes) | set(new_codes))

    print(f"\n  Title-first → Content-first disagreements:")
    print(f"  {'old \\ new':<24} " + "  ".join(f"{c:<16}" for c in all_move_codes))
    for oc in all_move_codes:
        row_vals = [matrix.get((oc, nc), 0) for nc in all_move_codes]
        if any(v > 0 for v in row_vals):
            print(f"  {oc:<24} " + "  ".join(f"{v:<16}" for v in row_vals))

    # ── 2. New per-topic counts ───────────────────────────────────────────────
    new_counts = Counter(new_labels)
    old_counts = Counter(old_labels)
    n = len(rows)

    print(f"\n{'Topic':<24} {'old':>5}  {'new':>5}  {'Δ':>5}")
    print("  " + "-" * 44)
    all_topic_codes = sorted(set(old_counts) | set(new_counts))
    highlight = {"primary_care", "digital_health", "wait_times", "funding"}
    for code in sorted(all_topic_codes, key=lambda c: -new_counts.get(c, 0)):
        old_n = old_counts.get(code, 0)
        new_n = new_counts.get(code, 0)
        delta = new_n - old_n
        flag  = " ◄" if code in highlight else ""
        print(f"  {code:<24} {old_n:>5}  {new_n:>5}  {delta:>+5}{flag}")

    # ── 3. 20 random moved-label samples ──────────────────────────────────────
    random.seed(42)
    sample = random.sample(moved, min(20, len(moved)))
    sample.sort(key=lambda r: (r["_label_title_first"], r["_label_content_first"]))

    print(f"\n{'─'*70}")
    print(f"20 random docs whose label moved (title-first → content-first)")
    print(f"{'─'*70}")
    for r in sample:
        print(f"\n  {r['doc_id']}")
        print(f"  org   : {r['org'][:65]}")
        print(f"  OLD   : {r['_label_title_first']:<20}  NEW: {r['_label_content_first']}")
        snippet = r["card_text"][:220].replace("\n", " | ")
        print(f"  card  : {snippet}…")


# ── qa report ─────────────────────────────────────────────────────────────────

def _qa_report(rows: list[dict], skipped: int, rules: dict) -> None:
    topic_counts = Counter(r["topic_seed"] for r in rows)
    st_counts    = Counter(r["stakeholder_type"] or "unknown" for r in rows)
    n            = len(rows)
    n_unlabeled  = topic_counts.get("unlabeled", 0)
    n_clustered  = sum(1 for r in rows if r["near_dup_cluster"])

    code_to_name = {t["code"]: t["name"] for t in rules.get("topics", [])}
    code_to_name["unlabeled"] = "unlabeled"

    print("\n" + "=" * 62)
    print("CORPUS QA")
    print("=" * 62)
    print(f"  Rows in corpus      : {n}")
    print(f"  Skipped             : {skipped}  (non-brief, dup, dropped, ocr)")
    print(f"  In near-dup cluster : {n_clustered}")
    print(f"  Unlabeled fraction  : {n_unlabeled/n*100:.1f}%  (target < 15%)")

    print("\nPer-topic counts:")
    for code, cnt in topic_counts.most_common():
        bar = "█" * int(cnt / n * 40)
        print(f"  {code:<22} {cnt:>4}  ({cnt/n*100:4.1f}%)  {bar}")

    print("\nPer-stakeholder counts:")
    for stype, cnt in st_counts.most_common():
        bar = "█" * int(cnt / n * 40)
        print(f"  {stype:<22} {cnt:>4}  ({cnt/n*100:4.1f}%)  {bar}")

    random.seed(99)
    sample = random.sample(rows, min(5, n))
    print("\n5 random cards:")
    print("-" * 62)
    for r in sample:
        print(f"\n  doc_id  : {r['doc_id']}")
        print(f"  Name    : {r['Name'][:70]}")
        print(f"  org     : {r['org'][:60]}")
        print(f"  topic   : {r['topic_seed']}")
        print(f"  stk     : {r['stakeholder_type']}")
        print(f"  cluster : {r['near_dup_cluster'] or '—'}")
        snippet = r["card_text"][:300].replace("\n", " | ")
        print(f"  card    : {snippet}…")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Build corpus.csv (Stage 6)")
    ap.add_argument("--card",    choices=["simple", "tiered"], default="simple")
    ap.add_argument("--compare", action="store_true",
                    help="Run both labelers and print comparison; do NOT write corpus.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    entries    = load_manifest()
    rules      = load_rules()
    org_lookup = load_org_lookup()

    compiled      = _compile_rules(rules)
    fallback      = rules.get("fallback", "unlabeled")
    exempt_lower  = {s.lower() for s in rules.get("title_exempt_studies", [])}

    PROCESSED.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    skipped = 0

    for doc_id, entry in entries.items():
        if entry.get("source_type") != "hesa_brief":
            skipped += 1; continue
        if entry.get("duplicate_of"):
            skipped += 1; continue
        if entry.get("dropped_short") or entry.get("needs_ocr"):
            skipped += 1; continue
        if not entry.get("language"):
            skipped += 1; continue

        plain_path  = CLEAN / f"{doc_id}.txt"
        masked_path = CLEAN / f"{doc_id}_masked.txt"
        if not plain_path.exists():
            skipped += 1; continue

        masked_text = (masked_path.read_text(encoding="utf-8")
                       if masked_path.exists()
                       else plain_path.read_text(encoding="utf-8"))

        card = make_card_tiered(masked_text) if args.card == "tiered" else make_card_simple(masked_text)
        name = _doc_title(masked_text) or entry.get("study_or_consultation_title") or ""

        label_tf = assign_topic_title_first(entry, compiled, exempt_lower, fallback, card)
        label_cf = assign_topic_content_first(entry, compiled, exempt_lower, fallback, card)

        org    = entry.get("organization")
        date   = entry.get("published_date") or entry.get("fetch_date")

        rows.append({
            "doc_id":               doc_id,
            "Name":                 name,
            "org":                  org or "",
            "source_type":          entry.get("source_type", ""),
            "date":                 date or "",
            "language":             entry.get("language", ""),
            "study_title":          entry.get("study_or_consultation_title") or "",
            "card_text":            card,
            "text_org_masked_path": str(masked_path.relative_to(ROOT)) if masked_path.exists() else "",
            "full_text_path":       str(plain_path.relative_to(ROOT)),
            "topic_seed":           label_cf,          # content-first is the new default
            "stakeholder_type":     assign_stakeholder(org, org_lookup) or "",
            "label_source":         LABEL_SOURCE,
            "near_dup_cluster":     entry.get("near_dup_cluster") or "",
            # comparison-only fields (stripped before writing)
            "_label_title_first":   label_tf,
            "_label_content_first": label_cf,
        })

    rows.sort(key=lambda r: r["doc_id"])

    if args.compare:
        run_compare(rows, rules)
        return

    # Strip comparison-only fields and write
    clean_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]

    if not args.dry_run:
        out_path = PROCESSED / "corpus.csv"
        with out_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CORPUS_COLS)
            w.writeheader()
            w.writerows(clean_rows)
        print(f"  Wrote {len(clean_rows)} rows to {out_path.relative_to(ROOT)}")

    _qa_report(clean_rows, skipped, rules)


if __name__ == "__main__":
    main()
