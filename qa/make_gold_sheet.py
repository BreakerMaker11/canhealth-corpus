"""qa/make_gold_sheet.py — sample 200 docs for gold labeling.

Sampling rules (applied to corpus.csv):
  1. Exclude rows with a non-empty near_dup_cluster (cluster members are
     ineligible for gold sampling per CLAUDE.md).
  2. Sample up to 20 docs from the unlabeled pool.
  3. Sample up to 180 docs from labeled topics, stratified:
       - Floor of 10 per class (or all if class has < 10 eligible docs).
       - Remaining quota distributed proportionally to eligible class size,
         using the largest-remainder method to avoid off-by-one from rounding.
  4. Combine, shuffle with --seed, write labeling_sheet.csv (weak label omitted).
  5. Draw 50 rows from the same 200 → second_pass_sheet.csv (kappa relabel set).

Usage:
  uv run python -m qa.make_gold_sheet [--seed N] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import random
from collections import defaultdict
from pathlib import Path

ROOT      = Path(__file__).parent.parent
PROCESSED = ROOT / "data" / "processed"

TOTAL_GOLD  = 200
UNLABELED_N = 20
LABELED_N   = TOTAL_GOLD - UNLABELED_N   # 180
FLOOR       = 10

OUTPUT_COLS = ["doc_id", "org", "card_text", "gold_topic", "gold_stakeholder", "notes"]


def load_corpus() -> list[dict]:
    path = PROCESSED / "corpus.csv"
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _allocate(eligible_per_topic: dict[str, int], target: int, floor: int) -> dict[str, int]:
    """Stratified allocation with floor + proportional remainder.

    Any class with fewer eligible docs than floor gets all of them.
    Remaining quota goes proportionally to the classes that still have
    headroom, resolved with the largest-remainder method.
    """
    alloc = {t: min(floor, n) for t, n in eligible_per_topic.items()}
    used  = sum(alloc.values())
    quota = target - used

    if quota <= 0:
        return alloc

    rem_elig  = {t: eligible_per_topic[t] - alloc[t] for t in eligible_per_topic}
    total_rem = sum(rem_elig.values())

    if total_rem == 0:
        return alloc

    # Largest-remainder method
    real_parts = {t: quota * rem_elig[t] / total_rem for t in rem_elig}
    int_parts  = {t: int(q) for t, q in real_parts.items()}
    remainders = {t: real_parts[t] - int_parts[t] for t in real_parts}

    extra = quota - sum(int_parts.values())
    top   = sorted(remainders, key=lambda x: -remainders[x])[:max(0, extra)]
    for t in top:
        int_parts[t] += 1

    for t in alloc:
        alloc[t] += min(int_parts[t], rem_elig[t])

    return alloc


def main() -> None:
    ap = argparse.ArgumentParser(description="Build gold labeling sheets (Stage QA)")
    ap.add_argument("--seed",    type=int, default=42,
                    help="Random seed for reproducibility (default: 42)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print sampling plan without writing files")
    args = ap.parse_args()

    rng = random.Random(args.seed)

    all_rows = load_corpus()

    # ── eligibility filter ────────────────────────────────────────────────────
    excluded_cluster = [r for r in all_rows if r.get("near_dup_cluster")]
    eligible         = [r for r in all_rows if not r.get("near_dup_cluster")]

    unlabeled_pool = [r for r in eligible if r["topic_seed"] == "unlabeled"]
    labeled_pool   = [r for r in eligible if r["topic_seed"] != "unlabeled"]

    # ── unlabeled sample ──────────────────────────────────────────────────────
    rng.shuffle(unlabeled_pool)
    unlabeled_sample = unlabeled_pool[:UNLABELED_N]

    # ── labeled stratified sample ─────────────────────────────────────────────
    by_topic: dict[str, list[dict]] = defaultdict(list)
    for r in labeled_pool:
        by_topic[r["topic_seed"]].append(r)

    for topic_rows in by_topic.values():
        rng.shuffle(topic_rows)

    eligible_counts = {t: len(rows) for t, rows in by_topic.items()}
    alloc           = _allocate(eligible_counts, LABELED_N, FLOOR)

    labeled_sample: list[dict] = []
    for topic, n in alloc.items():
        labeled_sample.extend(by_topic[topic][:n])

    # ── combine and shuffle ───────────────────────────────────────────────────
    gold = unlabeled_sample + labeled_sample
    rng.shuffle(gold)

    # ── report ────────────────────────────────────────────────────────────────
    sampled_by_topic: dict[str, int] = defaultdict(int)
    for r in labeled_sample:
        sampled_by_topic[r["topic_seed"]] += 1

    print(f"\nGold sheet sampling  (seed={args.seed})")
    print("=" * 54)
    print(f"  Corpus rows              : {len(all_rows)}")
    print(f"  Excluded (cluster)       : {len(excluded_cluster)}")
    print(f"  Eligible                 : {len(eligible)}")
    print(f"    unlabeled pool         : {len(unlabeled_pool)}")
    print(f"    labeled pool           : {len(labeled_pool)}")
    print(f"  Sampled unlabeled        : {len(unlabeled_sample)}  (target {UNLABELED_N})")
    print(f"  Sampled labeled          : {len(labeled_sample)}  (target {LABELED_N})")
    print(f"  Total gold               : {len(gold)}  (target {TOTAL_GOLD})")
    print()
    print(f"  {'topic':<24} {'eligible':>8}  {'alloc':>5}  {'sampled':>7}")
    print("  " + "-" * 50)
    for t in sorted(eligible_counts, key=lambda x: -eligible_counts[x]):
        elig = eligible_counts[t]
        alc  = alloc[t]
        samp = sampled_by_topic[t]
        flag = "  ← all taken" if elig <= FLOOR else ""
        print(f"  {t:<24} {elig:>8}  {alc:>5}  {samp:>7}{flag}")

    if args.dry_run:
        print("\n  [dry-run] No files written.")
        return

    # ── labeling_sheet.csv ────────────────────────────────────────────────────
    sheet_path = PROCESSED / "labeling_sheet.csv"
    with sheet_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLS)
        w.writeheader()
        for r in gold:
            w.writerow({
                "doc_id":           r["doc_id"],
                "org":              r["org"],
                "card_text":        r["card_text"],
                "gold_topic":       "",
                "gold_stakeholder": "",
                "notes":            "",
            })
    print(f"\n  Wrote {len(gold)} rows → {sheet_path.relative_to(ROOT)}")

    # ── second_pass_sheet.csv (50 rows for kappa) ─────────────────────────────
    second_pass      = rng.sample(gold, min(50, len(gold)))
    second_pass_path = PROCESSED / "second_pass_sheet.csv"
    with second_pass_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLS)
        w.writeheader()
        for r in second_pass:
            w.writerow({
                "doc_id":           r["doc_id"],
                "org":              r["org"],
                "card_text":        r["card_text"],
                "gold_topic":       "",
                "gold_stakeholder": "",
                "notes":            "",
            })
    print(f"  Wrote {len(second_pass)} rows → {second_pass_path.relative_to(ROOT)}")
    print(f"\n  second_pass_sheet is a subset of labeling_sheet"
          f" — label both in one pass, freeze second_pass_sheet before relabeling.")


if __name__ == "__main__":
    main()
