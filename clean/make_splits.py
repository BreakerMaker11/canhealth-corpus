"""clean/make_splits.py — split corpus into train/dev for classifier training.

Inputs:
  data/processed/corpus.csv      — full weak-labeled corpus
  data/processed/gold_test.csv   — hand-labeled gold set (excluded from train/dev)
  manifest.jsonl                 — read duplicate_of field for safety check

Outputs:
  data/processed/train.csv
  data/processed/dev.csv

Algorithm:
  1. Drop any row whose doc_id appears in gold_test.csv.
  2. Drop duplicate_of docs (corpus.csv should already exclude them; safety check via
     manifest.jsonl — fixture runs without manifest.jsonl just get an empty dup set).
  3. Find contaminated clusters: any near_dup_cluster that contains at least one gold
     doc_id. Remove all members of those clusters from the eligible pool.
  4. Build split units: each unclustered doc is its own unit; each safe cluster is one
     unit, assigned the dominant topic (mode of member topics; ties broken alphabetically).
  5. Sort units by unit_key before shuffling (determinism), then stratified 90/10 split
     by topic with a fixed seed. Every topic gets at least 1 dev unit.
  6. Expand cluster units back to individual docs; sort outputs by doc_id.

--check mode:
  Reads existing train.csv and dev.csv, then asserts:
    (a) no gold doc_id appears in train or dev
    (b) no near_dup_cluster straddles both splits
    (c) no duplicate_of doc_id appears in train or dev
  Prints a pass/fail line per check. Exits 0 if all pass, 1 if any violation.

Usage:
  # Against real data (gold_test.csv must exist):
  uv run python -m clean.make_splits [--seed N] [--dry-run]

  # Against synthetic fixtures (no gold_test.csv required yet):
  uv run python -m clean.make_splits \\
      --corpus fixtures/corpus_fixture.csv \\
      --gold   fixtures/gold_test_fixture.csv

  # Validate existing splits:
  uv run python -m clean.make_splits --check
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
import random as _random

ROOT      = Path(__file__).parent.parent
PROCESSED = ROOT / "data" / "processed"
MANIFEST  = ROOT / "manifest.jsonl"

DEV_FRAC = 0.10
SEED     = 42


# ── loaders ───────────────────────────────────────────────────────────────────

def _load_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str).fillna("")


def _load_gold_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    df = _load_csv(path)
    return set(df["doc_id"].str.strip())


def _load_dup_ids() -> set[str]:
    """doc_ids marked duplicate_of in manifest.jsonl. Empty if manifest absent."""
    if not MANIFEST.exists():
        return set()
    ids: set[str] = set()
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            obj = json.loads(line)
            if obj.get("duplicate_of"):
                ids.add(obj["doc_id"])
    return ids


# ── core split logic ──────────────────────────────────────────────────────────

def make_splits(
    corpus_df: pd.DataFrame,
    gold_ids:  set[str],
    dup_ids:   set[str],
    seed:      int = SEED,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """
    Returns (train_df, dev_df, stats).
    Fully deterministic: given identical inputs and seed, outputs are identical.
    """
    stats: dict = {}
    stats["total_corpus"] = len(corpus_df)

    # ── 1. Drop duplicate_of ──────────────────────────────────────────────────
    in_dup      = corpus_df["doc_id"].isin(dup_ids)
    corpus_df   = corpus_df[~in_dup].copy()
    stats["dropped_dup"] = int(in_dup.sum())

    # ── 2. Drop gold ──────────────────────────────────────────────────────────
    in_gold  = corpus_df["doc_id"].isin(gold_ids)
    eligible = corpus_df[~in_gold].copy()
    stats["dropped_gold"] = int(in_gold.sum())

    # ── 3. Find contaminated clusters ─────────────────────────────────────────
    # A cluster is contaminated if any of its members (in the full corpus, pre-gold-drop)
    # appears in gold_ids.
    gold_cluster_ids: set[str] = set(
        corpus_df.loc[corpus_df["doc_id"].isin(gold_ids), "near_dup_cluster"]
        .replace("", None).dropna().unique()
    )

    in_contaminated = (
        eligible["near_dup_cluster"].isin(gold_cluster_ids) &
        eligible["near_dup_cluster"].ne("")
    )
    excluded_cluster_docs = eligible.loc[in_contaminated, "doc_id"].tolist()
    eligible = eligible[~in_contaminated].copy()

    stats["contaminated_clusters"]    = sorted(gold_cluster_ids)
    stats["dropped_contaminated"]     = len(excluded_cluster_docs)
    stats["excluded_cluster_doc_ids"] = excluded_cluster_docs
    stats["eligible"]                 = len(eligible)

    # ── 4. Build split units ──────────────────────────────────────────────────
    eligible = eligible.sort_values("doc_id").reset_index(drop=True)

    has_cluster = eligible["near_dup_cluster"].ne("")
    clustered   = eligible[has_cluster]
    unclustered = eligible[~has_cluster]

    # (unit_key, dominant_topic, [doc_ids]) — sorted for determinism
    units: list[tuple[str, str, list[str]]] = []

    for _, row in unclustered.iterrows():
        units.append((row["doc_id"], row["topic_seed"], [row["doc_id"]]))

    cluster_unit_ids: list[str] = []
    for cluster_id, grp in clustered.sort_values("doc_id").groupby("near_dup_cluster", sort=True):
        topic_counts = Counter(grp["topic_seed"].tolist())
        dominant     = min(  # alphabetical tiebreak → deterministic
            (t for t, n in topic_counts.items() if n == topic_counts.most_common(1)[0][1])
        )
        units.append((str(cluster_id), dominant, sorted(grp["doc_id"].tolist())))
        cluster_unit_ids.append(str(cluster_id))

    stats["safe_cluster_ids"] = cluster_unit_ids

    # ── 5. Stratified 90/10 split ─────────────────────────────────────────────
    by_topic: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    for unit_key, topic, doc_ids in units:
        by_topic[topic].append((unit_key, doc_ids))

    rng = _random.Random(seed)
    train_ids: set[str] = set()
    dev_ids:   set[str] = set()
    per_topic: dict[str, dict] = {}

    for topic in sorted(by_topic):          # sorted → deterministic iteration
        topic_units = sorted(by_topic[topic], key=lambda x: x[0])   # sort before shuffle
        rng.shuffle(topic_units)

        n_dev       = max(1, round(len(topic_units) * DEV_FRAC))
        dev_units   = topic_units[:n_dev]
        train_units = topic_units[n_dev:]

        dev_docs   = [d for _, ids in dev_units   for d in ids]
        train_docs = [d for _, ids in train_units for d in ids]

        dev_ids.update(dev_docs)
        train_ids.update(train_docs)
        per_topic[topic] = {"n_units": len(topic_units),
                            "train":   len(train_docs),
                            "dev":     len(dev_docs)}

    stats["per_topic"] = per_topic

    # ── 6. Build output DataFrames ────────────────────────────────────────────
    train_df = (eligible[eligible["doc_id"].isin(train_ids)]
                .sort_values("doc_id").reset_index(drop=True))
    dev_df   = (eligible[eligible["doc_id"].isin(dev_ids)]
                .sort_values("doc_id").reset_index(drop=True))

    stats["train_total"] = len(train_df)
    stats["dev_total"]   = len(dev_df)

    return train_df, dev_df, stats


# ── reporting ─────────────────────────────────────────────────────────────────

def _print_summary(stats: dict, corpus_path: Path, gold_path: Path) -> None:
    print(f"\nTrain/dev split")
    print("=" * 58)
    print(f"  Corpus        : {corpus_path.name}  ({stats['total_corpus']} rows)")
    print(f"  Gold          : {gold_path.name}  ({stats['dropped_gold']} ids)")
    print()
    print(f"  Dropped — duplicate_of          : {stats['dropped_dup']}")
    print(f"  Dropped — in gold               : {stats['dropped_gold']}")
    if stats["contaminated_clusters"]:
        print(f"  Excluded — contaminated clusters: {stats['dropped_contaminated']}"
              f"  {stats['contaminated_clusters']}")
    else:
        print(f"  Excluded — contaminated clusters: 0")
    print(f"  Safe clusters moved intact      : {len(stats['safe_cluster_ids'])}"
          + (f"  {stats['safe_cluster_ids']}" if stats["safe_cluster_ids"] else ""))
    print()
    print(f"  Eligible for split : {stats['eligible']}")
    print(f"  Train              : {stats['train_total']}")
    print(f"  Dev                : {stats['dev_total']}")
    print()
    print(f"  {'topic':<24} {'units':>5}  {'train':>6}  {'dev':>4}")
    print("  " + "-" * 44)
    for topic in sorted(stats["per_topic"]):
        t = stats["per_topic"][topic]
        print(f"  {topic:<24} {t['n_units']:>5}  {t['train']:>6}  {t['dev']:>4}")


# ── check mode ────────────────────────────────────────────────────────────────

def run_check(
    train_path: Path,
    dev_path:   Path,
    gold_ids:   set[str],
    dup_ids:    set[str],
) -> bool:
    """Return True if all checks pass."""
    if not train_path.exists() or not dev_path.exists():
        print("ERROR: train.csv or dev.csv not found — run without --check first.")
        return False

    train_df = _load_csv(train_path)
    dev_df   = _load_csv(dev_path)
    passed   = True

    # (a) No gold doc_id in train or dev
    gold_in_train = set(train_df["doc_id"]) & gold_ids
    gold_in_dev   = set(dev_df["doc_id"])   & gold_ids
    if gold_in_train or gold_in_dev:
        print(f"FAIL (a) gold ids in train={gold_in_train}  dev={gold_in_dev}")
        passed = False
    else:
        print("PASS (a) no gold doc_id in train or dev")

    # (b) No cluster straddles both splits
    train_clusters = set(train_df.loc[train_df["near_dup_cluster"].ne(""), "near_dup_cluster"])
    dev_clusters   = set(dev_df.loc[dev_df["near_dup_cluster"].ne(""),   "near_dup_cluster"])
    straddling     = train_clusters & dev_clusters
    if straddling:
        print(f"FAIL (b) clusters in both splits: {straddling}")
        passed = False
    else:
        print("PASS (b) no near_dup_cluster straddles both splits")

    # (c) No duplicate_of doc in train or dev
    dup_in_train = set(train_df["doc_id"]) & dup_ids
    dup_in_dev   = set(dev_df["doc_id"])   & dup_ids
    if dup_in_train or dup_in_dev:
        print(f"FAIL (c) duplicate_of ids in train={dup_in_train}  dev={dup_in_dev}")
        passed = False
    else:
        print(f"PASS (c) no duplicate_of doc in train or dev"
              + ("  (manifest not found — dup check skipped)" if not MANIFEST.exists() else ""))

    return passed


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Build train/dev splits (Stage post-6)")
    ap.add_argument("--corpus",  type=Path, default=PROCESSED / "corpus.csv")
    ap.add_argument("--gold",    type=Path, default=PROCESSED / "gold_test.csv")
    ap.add_argument("--seed",    type=int,  default=SEED)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print split plan without writing files")
    ap.add_argument("--check",   action="store_true",
                    help="Validate existing train.csv / dev.csv and exit")
    args = ap.parse_args()

    gold_ids = _load_gold_ids(args.gold)
    dup_ids  = _load_dup_ids()

    if args.check:
        ok = run_check(
            PROCESSED / "train.csv",
            PROCESSED / "dev.csv",
            gold_ids,
            dup_ids,
        )
        sys.exit(0 if ok else 1)

    if not args.corpus.exists():
        print(f"ERROR: corpus not found: {args.corpus}")
        sys.exit(1)

    corpus_df = _load_csv(args.corpus)

    # ── run split ────────────────────────────────────────────────────────────
    train_df, dev_df, stats = make_splits(corpus_df, gold_ids, dup_ids, args.seed)
    _print_summary(stats, args.corpus, args.gold)

    # ── determinism assertion ────────────────────────────────────────────────
    train2, dev2, _ = make_splits(corpus_df.copy(), gold_ids, dup_ids, args.seed)
    assert list(train_df["doc_id"]) == list(train2["doc_id"]), "Non-deterministic train!"
    assert list(dev_df["doc_id"])   == list(dev2["doc_id"]),   "Non-deterministic dev!"
    print("\n  Determinism check: PASS (two runs produced identical splits)")

    if args.dry_run:
        print("  [dry-run] No files written.")
        return

    # ── write outputs ────────────────────────────────────────────────────────
    train_path = PROCESSED / "train.csv"
    dev_path   = PROCESSED / "dev.csv"
    train_df.to_csv(train_path, index=False)
    dev_df.to_csv(dev_path,   index=False)
    print(f"  Wrote {len(train_df)} rows → {train_path.relative_to(ROOT)}")
    print(f"  Wrote {len(dev_df)} rows → {dev_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
