"""Print post-harvest summary: counts per study/source_type, failed.csv, raw size.

Run:  uv run python scrape/harvest_summary.py
"""

import collections
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent

# ---- manifest counts ----
by_study: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
by_type: collections.Counter = collections.Counter()

with (ROOT / "manifest.jsonl").open(encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        st = d["source_type"]
        title = d.get("study_or_consultation_title", "?")
        by_study[title][st] += 1
        by_type[st] += 1

print(f"\n{'Study':<55} {'brief':>6} {'report':>7} {'gov_r':>6}")
print("-" * 78)
for title in sorted(by_study):
    row = by_study[title]
    print(
        f"{title[:54]:<55}"
        f" {row.get('hesa_brief', 0):>6}"
        f" {row.get('hesa_report', 0):>7}"
        f" {row.get('gov_response', 0):>6}"
    )
print("-" * 78)
print(f"\nTotals by source_type:")
for st, n in sorted(by_type.items()):
    print(f"  {st:<20} {n}")
print(f"  {'TOTAL':<20} {sum(by_type.values())}")

# ---- failed.csv ----
failed = (ROOT / "failed.csv").read_text(encoding="utf-8").strip().splitlines()
print(f"\nfailed.csv ({len(failed) - 1} failure row(s)):")
for row in failed:
    print(f"  {row}")

# ---- data/raw size ----
raw_files = list((ROOT / "data" / "raw").iterdir())
raw_files = [f for f in raw_files if f.name != ".gitkeep"]
total_bytes = sum(f.stat().st_size for f in raw_files)
print(f"\ndata/raw/: {len(raw_files)} files, {total_bytes / 1_048_576:.1f} MB total")
pdf_count = sum(1 for f in raw_files if f.suffix == ".pdf")
html_count = sum(1 for f in raw_files if f.suffix == ".html")
print(f"  {pdf_count} PDF  |  {html_count} HTML")
