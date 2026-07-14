"""qa/qa_report.py — Stage 7 QA report.

Prints: counts per source/topic/stakeholder, length distribution, language
breakdown, near-dup / dedupe stats, chunk counts per doc type, gold-set
summary, and 5 random cleaned card samples.

Usage:
  uv run python -m qa.qa_report [--seed N]
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import statistics
from pathlib import Path

ROOT      = Path(__file__).parent.parent
PROCESSED = ROOT / "data" / "processed"
MANIFEST  = ROOT / "manifest.jsonl"

SEP  = "=" * 62
SEP2 = "-" * 62


def _bar(n: int, total: int, width: int = 20) -> str:
    filled = round(width * n / total) if total else 0
    return "█" * filled


def _pct(n: int, total: int) -> str:
    return f"{100 * n / total:.1f}%" if total else "—"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _load_csv(path: Path) -> list[dict]:
    import csv
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _word_count(text: str) -> int:
    return len(text.split())


def _dist_stats(values: list[int]) -> str:
    if not values:
        return "no data"
    return (f"min={min(values)}  p25={int(statistics.quantiles(values, n=4)[0])}"
            f"  median={int(statistics.median(values))}"
            f"  p75={int(statistics.quantiles(values, n=4)[2])}"
            f"  p95={int(statistics.quantiles(values, n=100)[94])}"
            f"  max={max(values)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="QA report (Stage 7)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)

    corpus   = _load_csv(PROCESSED / "corpus.csv")
    manifest = _load_jsonl(MANIFEST)
    chunks   = _load_jsonl(PROCESSED / "rag_chunks.jsonl")
    gold     = _load_csv(PROCESSED / "gold_test.csv")

    total = len(corpus)
    briefs = [r for r in corpus if r.get("source_type") == "hesa_brief"]

    print(SEP)
    print("  CANHEALTH CORPUS — QA REPORT")
    print(SEP)

    # ── 1. Corpus overview ────────────────────────────────────────────────────
    print(f"\n{'1. CORPUS OVERVIEW':}")
    print(f"   corpus.csv rows   : {total}")
    print(f"   manifest entries  : {len(manifest)}")
    print(f"   gold_test.csv     : {len(gold)} rows")

    # by source_type
    by_src = collections.Counter(r.get("source_type", "") for r in corpus)
    print(f"\n   By source_type:")
    for src, n in by_src.most_common():
        print(f"     {src:<28} {n:>4}  {_pct(n, total):>6}  {_bar(n, total)}")

    # ── 2. Topic distribution ─────────────────────────────────────────────────
    print(f"\n{'2. TOPIC DISTRIBUTION (briefs only)':}")
    by_topic = collections.Counter(r.get("topic_seed", "") for r in briefs)
    brief_total = len(briefs)
    for t, n in by_topic.most_common():
        label = t if t else "(blank)"
        print(f"   {label:<24} {n:>4}  {_pct(n, brief_total):>6}  {_bar(n, brief_total)}")
    unlabeled_n = by_topic.get("unlabeled", 0)
    print(f"\n   Unlabeled fraction: {_pct(unlabeled_n, brief_total)}  (target < 15%)")

    # ── 3. Stakeholder distribution ───────────────────────────────────────────
    print(f"\n{'3. STAKEHOLDER DISTRIBUTION (briefs)':}")
    by_stk = collections.Counter(r.get("stakeholder_type", "") for r in briefs)
    for stk, n in by_stk.most_common():
        label = stk if stk else "(unknown)"
        print(f"   {label:<24} {n:>4}  {_pct(n, brief_total):>6}  {_bar(n, brief_total)}")

    # ── 4. Language breakdown ─────────────────────────────────────────────────
    print(f"\n{'4. LANGUAGE BREAKDOWN':}")
    by_lang = collections.Counter(r.get("language", "") or "unknown" for r in manifest)
    for lang, n in by_lang.most_common():
        print(f"   {lang:<12} {n:>4}  {_pct(n, len(manifest)):>6}")

    # ── 5. Length distribution (card_text word counts) ────────────────────────
    print(f"\n{'5. CARD TEXT LENGTH  (words)':}")
    card_wc = [_word_count(r.get("card_text", "")) for r in corpus if r.get("card_text")]
    print(f"   All docs    : {_dist_stats(card_wc)}")
    brief_wc = [_word_count(r.get("card_text", "")) for r in briefs if r.get("card_text")]
    print(f"   Briefs only : {_dist_stats(brief_wc)}")
    short = sum(1 for w in card_wc if w < 150)
    print(f"   < 150 words : {short}  (should be 0 after cleaning floor)")

    # ── 6. Near-dup / dedupe stats ────────────────────────────────────────────
    print(f"\n{'6. NEAR-DUPLICATE STATS':}")
    clusters = collections.Counter(
        r["near_dup_cluster"] for r in corpus if r.get("near_dup_cluster")
    )
    in_cluster = sum(clusters.values())
    print(f"   Docs in a cluster       : {in_cluster}  ({_pct(in_cluster, total)})")
    print(f"   Distinct clusters       : {len(clusters)}")
    if clusters:
        sizes = list(clusters.values())
        print(f"   Cluster size dist       : {_dist_stats(sizes)}")

    from_manifest_dups = sum(1 for e in manifest if e.get("duplicate_of"))
    print(f"   duplicate_of in manifest: {from_manifest_dups}")

    ocr_needed = sum(1 for e in manifest if e.get("needs_ocr"))
    print(f"   needs_ocr               : {ocr_needed}")

    layout_mangled = sum(1 for e in manifest if e.get("layout_mangled"))
    if layout_mangled:
        print(f"   layout_mangled (flagged): {layout_mangled}")

    # ── 7. Chunk stats ────────────────────────────────────────────────────────
    print(f"\n{'7. RAG CHUNK STATS':}")
    if not chunks:
        print(f"   rag_chunks.jsonl not found or empty — run clean/chunk_reports.py")
    else:
        by_type  = collections.Counter(c.get("source_type", "") for c in chunks)
        rec_n    = sum(1 for c in chunks if c.get("is_recommendation"))
        tok_vals = [c.get("token_approx", 0) for c in chunks]
        print(f"   Total chunks            : {len(chunks)}")
        print(f"   Recommendation chunks   : {rec_n}")
        print(f"   Token approx dist       : {_dist_stats(tok_vals)}")
        print(f"   By source_type:")
        for src, n in by_type.most_common():
            print(f"     {src:<28} {n:>4} chunks")

    # ── 8. Gold set summary ───────────────────────────────────────────────────
    print(f"\n{'8. GOLD SET SUMMARY':}")
    if not gold:
        print(f"   gold_test.csv not found")
    else:
        by_gold_topic = collections.Counter(r.get("gold_topic", "") for r in gold)
        by_gold_stk   = collections.Counter(r.get("gold_stakeholder", "") for r in gold)
        print(f"   Total gold rows: {len(gold)}")
        print(f"\n   By gold_topic:")
        for t, n in by_gold_topic.most_common():
            print(f"     {t:<24} {n:>4}  {_pct(n, len(gold)):>6}  {_bar(n, len(gold), 15)}")
        print(f"\n   By gold_stakeholder:")
        for stk, n in by_gold_stk.most_common():
            label = stk if stk else "(unknown)"
            print(f"     {label:<24} {n:>4}  {_pct(n, len(gold)):>6}")
        other_none = by_gold_topic.get("other_none", 0)
        print(f"\n   other_none (noise floor): {other_none}  ({_pct(other_none, len(gold))})")

    # ── 9. 5 random cleaned card samples ─────────────────────────────────────
    print(f"\n{'9. FIVE RANDOM CARD SAMPLES':}")
    sample = rng.sample(briefs, min(5, len(briefs)))
    for r in sample:
        wc = _word_count(r.get("card_text", ""))
        print(SEP2)
        print(f"  doc_id  : {r['doc_id']}")
        print(f"  org     : {r['org']}")
        print(f"  topic   : {r['topic_seed']:<20}  stakeholder: {r['stakeholder_type']}")
        print(f"  words   : {wc}")
        snippet = r.get("card_text", "")[:300].replace("\n", " ")
        print(f"  card    : {snippet}…")
    print(SEP2)
    print()


if __name__ == "__main__":
    main()
