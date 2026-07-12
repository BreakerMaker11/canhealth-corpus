"""Stage 5: Deduplicate the cleaned corpus.

Exact dedupe (two passes):
  Pass 1 — hash: normalize cleaned text → SHA-256 → group; keep earliest per group,
            set duplicate_of={kept_id} for others.
  Pass 2 — semantic: embed hesa_briefs with all-MiniLM-L6-v2; pairs at cosine ≥ 0.98
            are treated the same as hash duplicates (keep earliest not-yet-dup doc).

Near-duplicate clustering (union-find over all pairs with cosine > 0.9):
  Every connected component of size ≥ 2 gets a cluster ID (ndc_001, …) stored in
  the manifest as near_dup_cluster.  Members include both kept and dup docs so the
  "kept" doc carries the cluster even after its duplicate is marked duplicate_of.

Near-duplicate report:
  Remaining pairs 0.9 < cosine < 0.98 are listed for manual review.  No action taken.

Usage:
  uv run python -m clean.dedupe [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT     = Path(__file__).parent.parent
CLEAN    = ROOT / "data" / "interim" / "clean"
MANIFEST = ROOT / "manifest.jsonl"

PAGE_MARKER_RE    = re.compile(r"\[\[page \d+\]\]")
SIM_NEAR          = 0.90   # lower bound for near-dup reporting + clustering
SIM_EXACT         = 0.98   # semantic pairs at or above this → exact dup treatment


# ── union-find ────────────────────────────────────────────────────────────────

class UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {x: x for x in items}
        self.rank   = {x: 0  for x in items}

    def find(self, x: str) -> str:
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1

    def components(self) -> list[list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            groups[self.find(x)].append(x)
        return [sorted(g) for g in groups.values() if len(g) >= 2]


# ── normalisation + hashing ───────────────────────────────────────────────────

def normalize_for_hash(text: str) -> str:
    text = PAGE_MARKER_RE.sub(" ", text)
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── manifest I/O ─────────────────────────────────────────────────────────────

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


# ── pass 1: hash exact dedupe ─────────────────────────────────────────────────

def run_hash_dedupe(entries: dict[str, dict], dry_run: bool) -> list[list[str]]:
    """Return list of duplicate groups [[kept, dup, …], …]."""
    hash_to_docs: dict[str, list[str]] = {}
    for doc_id, entry in entries.items():
        if entry.get("needs_ocr") or entry.get("language") is None:
            continue
        if entry.get("dropped_short"):
            continue
        path = CLEAN / f"{doc_id}.txt"
        if not path.exists():
            continue
        h = sha256(normalize_for_hash(path.read_text(encoding="utf-8")))
        hash_to_docs.setdefault(h, []).append(doc_id)

    groups: list[list[str]] = []
    for doc_ids in hash_to_docs.values():
        if len(doc_ids) < 2:
            continue
        kept, *dups = sorted(doc_ids)
        groups.append([kept] + dups)
        if not dry_run:
            for d in dups:
                entries[d].setdefault("duplicate_of", kept)
    return groups


# ── pass 2: semantic dedupe + clustering ──────────────────────────────────────

def _eligible_for_embed(entries: dict[str, dict]) -> list[str]:
    return [
        doc_id for doc_id, e in entries.items()
        if e.get("source_type") == "hesa_brief"
        and not e.get("needs_ocr")
        and e.get("language") is not None
        and not e.get("dropped_short")
        and (CLEAN / f"{doc_id}.txt").exists()
    ]


def run_semantic_dedupe_and_cluster(
    entries: dict[str, dict],
    dry_run: bool,
) -> tuple[list[list[str]], list[tuple[float, str, str]]]:
    """
    Returns:
      semantic_groups  — [[kept, dup, …], …] for pairs ≥ SIM_EXACT
      near_pairs       — [(sim, d1, d2), …] for pairs in (SIM_NEAR, SIM_EXACT)
    Also writes near_dup_cluster to entries (unless dry_run).
    """
    import numpy as np
    from fastembed import TextEmbedding

    # Clear stale cluster IDs before reassigning
    if not dry_run:
        for e in entries.values():
            e.pop("near_dup_cluster", None)

    eligible = _eligible_for_embed(entries)
    if not eligible:
        return [], []

    print(f"  Embedding {len(eligible)} briefs with all-MiniLM-L6-v2 …")
    texts  = [(CLEAN / f"{d}.txt").read_text(encoding="utf-8") for d in eligible]
    model  = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
    embeds = np.array(list(model.embed(texts)), dtype="float32")

    # Normalise → cosine = dot product
    norms  = np.linalg.norm(embeds, axis=1, keepdims=True)
    embeds = embeds / np.maximum(norms, 1e-9)
    sim_matrix = embeds @ embeds.T

    n = len(eligible)
    all_near:  list[tuple[float, str, str]] = []   # > SIM_NEAR, < SIM_EXACT
    all_exact: list[tuple[float, str, str]] = []   # >= SIM_EXACT

    for i in range(n):
        for j in range(i + 1, n):
            sim = float(sim_matrix[i, j])
            if sim >= SIM_EXACT:
                all_exact.append((sim, eligible[i], eligible[j]))
            elif sim > SIM_NEAR:
                all_near.append((sim, eligible[i], eligible[j]))

    # ── semantic exact dup groups ─────────────────────────────────────────────
    # Union-find on ≥ SIM_EXACT pairs to form semantic-exact components
    uf_exact = UnionFind(eligible)
    for _, d1, d2 in all_exact:
        uf_exact.union(d1, d2)

    semantic_groups: list[list[str]] = []
    for component in uf_exact.components():
        # Keep earliest doc that is NOT yet a duplicate
        not_yet_dup = [d for d in component if not entries[d].get("duplicate_of")]
        if not not_yet_dup:
            continue   # all already marked from hash pass; no new group to report
        kept = not_yet_dup[0]   # alphabetically earliest
        dups = [d for d in not_yet_dup if d != kept]
        if not dups:
            continue
        semantic_groups.append([kept] + dups)
        if not dry_run:
            for d in dups:
                entries[d]["duplicate_of"] = kept

    # ── near-dup clustering (all pairs > SIM_NEAR) ────────────────────────────
    uf_all = UnionFind(eligible)
    for _, d1, d2 in all_exact:
        uf_all.union(d1, d2)
    for _, d1, d2 in all_near:
        uf_all.union(d1, d2)

    if not dry_run:
        for idx, component in enumerate(
            sorted(uf_all.components(), key=lambda c: c[0])
        ):
            cluster_id = f"ndc_{idx + 1:03d}"
            for doc_id in component:
                entries[doc_id]["near_dup_cluster"] = cluster_id

    all_near.sort(key=lambda x: -x[0])
    return semantic_groups, all_near


# ── reporting ─────────────────────────────────────────────────────────────────

def _fmt_group(group: list[str], entries: dict[str, dict], label: str) -> None:
    kept = group[0]
    e    = entries[kept]
    print(f"  GROUP — study: {(e.get('study_or_consultation_title') or '')[:50]}")
    print(f"    [KEPT] {kept}")
    print(f"           {(e.get('organization') or '')[:60]}")
    for dup_id in group[1:]:
        ed = entries[dup_id]
        print(f"    [DUP]  {dup_id}  ({label})")
        print(f"           {(ed.get('organization') or '')[:60]}")
    print()


def _fmt_near_pairs(
    pairs: list[tuple[float, str, str]],
    entries: dict[str, dict],
) -> None:
    if not pairs:
        print("  (none)")
        return
    hdr  = f"  {'sim':>5}  {'doc_id_1':<42}  {'org_1':<35}  {'study_1':<35}"
    hdr2 = f"  {'':>5}  {'doc_id_2':<42}  {'org_2':<35}  {'study_2':<35}"
    print(hdr)
    print(hdr2)
    print("  " + "-" * 122)
    for sim, d1, d2 in pairs:
        e1, e2 = entries[d1], entries[d2]
        print(
            f"  {sim:.3f}  {d1:<42}  "
            f"{(e1.get('organization') or '')[:35]:<35}  "
            f"{(e1.get('study_or_consultation_title') or '')[:35]:<35}"
        )
        print(
            f"  {'':>5}  {d2:<42}  "
            f"{(e2.get('organization') or '')[:35]:<35}  "
            f"{(e2.get('study_or_consultation_title') or '')[:35]:<35}"
        )
        print()


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Deduplicate corpus (Stage 5)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    entries = load_manifest()
    manifest_dirty = False

    # ── Pass 1: hash exact dedupe ─────────────────────────────────────────────
    print("\nStage 5a — Hash exact dedupe")
    print("=" * 60)
    hash_groups = run_hash_dedupe(entries, dry_run=args.dry_run)
    if not hash_groups:
        print("  No hash-exact duplicates found.")
    else:
        print(f"  {sum(len(g)-1 for g in hash_groups)} duplicates in {len(hash_groups)} groups:\n")
        for g in hash_groups:
            _fmt_group(g, entries, "hash-exact dup")
        manifest_dirty = True

    # ── Pass 2: semantic dedupe + cluster assignment ───────────────────────────
    print("\nStage 5b — Semantic dedupe (cosine ≥ 0.98) + near-dup clustering")
    print("=" * 60)
    sem_groups, near_pairs = run_semantic_dedupe_and_cluster(entries, dry_run=args.dry_run)

    if not sem_groups:
        print("  No new semantic exact duplicates found.")
    else:
        print(f"  {sum(len(g)-1 for g in sem_groups)} new semantic duplicates in {len(sem_groups)} groups:\n")
        for g in sem_groups:
            _fmt_group(g, entries, "semantic-exact dup")
        manifest_dirty = True

    # Cluster summary
    cluster_ids = {e.get("near_dup_cluster") for e in entries.values()
                   if e.get("near_dup_cluster")}
    print(f"\n  Near-dup clusters assigned: {len(cluster_ids)} clusters")
    if cluster_ids:
        for cid in sorted(cluster_ids):
            members = [d for d, e in entries.items() if e.get("near_dup_cluster") == cid]
            print(f"    {cid}: {', '.join(members)}")
        manifest_dirty = True

    print(f"\n  Near-duplicate pairs (0.9 < cosine < 0.98): {len(near_pairs)}")
    _fmt_near_pairs(near_pairs, entries)

    if manifest_dirty and not args.dry_run:
        save_manifest(entries)
        total_dups = sum(len(g)-1 for g in hash_groups) + sum(len(g)-1 for g in sem_groups)
        print(f"\n  Manifest updated: {total_dups} docs marked duplicate_of, "
              f"{len(cluster_ids)} clusters assigned.")


if __name__ == "__main__":
    main()
