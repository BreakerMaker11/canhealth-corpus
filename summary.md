# CanHealth Corpus — Session Summary (2026-07-13)

## Current state

- **Branch**: `HESA_scrapping`
- **Tag**: `v0.3-gold-frozen` (commit 041b5e3)
- **Latest commit**: 4789abd (README update)

---

## What was built this session

### 1. Topic rules overhaul (`topic_rules.yaml`)

Redrew the `mental_health` / `public_health` boundary after gold labeling
revealed 13 opioid-study briefs were mislabeled as `mental_health`.

**New boundary:**
- `public_health` now includes: substance use, addiction, opioids, overdose,
  harm reduction, drug policy, safe supply
- `mental_health` now scoped to: mental illness, mood/anxiety disorders,
  psychiatric services, depression, suicide prevention

Changes to the YAML:
- Removed from `mental_health` keywords: `substance use`, `addiction`
- Removed from `mental_health` strong: `opioid`, `overdose`
- Added to `mental_health` keywords: `psychiatric`, `mood disorder`,
  `anxiety disorder`
- Added to `mental_health` strong: `depression`, `suicide prevention` (kept)
- Added to `public_health` keywords: `substance use`, `addiction`,
  `drug policy`, `toxic drug`
- Added to `public_health` strong: `opioid`, `overdose`, `harm reduction`,
  `safe supply`

Decision recorded in `decisions.md`.

### 2. Corpus rebuild (`data/processed/corpus.csv`)

506 rows, 13.6% unlabeled (under 15% target). Determinism verified.

v0.2 → v0.3 deltas:

| topic | v0.2 | v0.3 | delta |
|---|---|---|---|
| public_health | 142 | 176 | +34 |
| pharmacare | 100 | 95 | −5 |
| womens_health | 83 | 80 | −3 |
| unlabeled | 73 | 69 | −4 |
| mental_health | 45 | 24 | −21 |
| workforce | 28 | 27 | −1 |
| indigenous_health | 14 | 14 | 0 |
| cancer | 11 | 11 | 0 |
| childrens_health | 10 | 10 | 0 |

### 3. Gold set finalized (`data/processed/gold_test.csv`)

217 hand-labeled docs: 200 original + 17 top-up (targeting `mental_health`
post-boundary-redraw and remaining `indigenous_health`). `label_source=gold`.
**Frozen — never regenerate.**

Per-topic gold counts:

| gold_topic | count |
|---|---|
| other_none | 46 |
| pharmacare | 36 |
| womens_health | 32 |
| public_health | 30 |
| workforce | 24 |
| childrens_health | 19 |
| mental_health | 14 |
| cancer | 13 |
| indigenous_health | 3 |
| **TOTAL** | **217** |

`other_none` = 46 (21%) is the noise floor — almost entirely COVID
civil-rights/anti-mandate briefs from the Emergency Situation study with no
health-policy ask.

### 4. Train/dev splits

506 corpus − 217 gold − 3 contaminated-cluster docs = 286 eligible →
**258 train / 28 dev** (seed 42, stratified by topic, cluster constraints).

4 contaminated clusters excluded entirely; 13 safe clusters moved intact.
All 3 `--check` assertions pass.

**Class notes for model training:**
- `cancer` — 0 train docs after gold exclusion; eval-only or merge at
  training time
- `mental_health`, `indigenous_health` — eval-only classes (all weak examples
  ended up in gold); no training signal from weak labels

### 5. New scripts

| script | purpose |
|---|---|
| `clean/chunk_reports.py` | Stage 7 RAG chunker for reports/gov_responses |
| `clean/make_splits.py` | Train/dev splitter with gold exclusion + cluster constraints |
| `qa/qa_report.py` | Stage 8 QA report (counts, lengths, language, deduplication, gold summary, 5 random samples) |

Fixture files for testing `make_splits.py`:
`fixtures/corpus_fixture.csv`, `fixtures/gold_test_fixture.csv`

### 6. QA report (`qa/qa_report.py`)

Run: `uv run python -m qa.qa_report`

Key findings:
- 506 rows, 13.6% unlabeled ✓
- Card length floor: 0 docs under 150 words ✓
- Language: 95.7% English; 1 misdetected (garbled FASD PDF)
- 10 needs_ocr, 10 layout_mangled (flagged, not dropped)
- 177 unknown stakeholder (35%) — org_lookup.csv incomplete
- rag_chunks.jsonl: not yet written (pending `--rebuild`)

### 7. README

Full rewrite covering corpus at a glance, topic codebook with boundary rules,
gold set breakdown, split constraints, pipeline stage table with status,
all running commands, repo layout, manifest schema.

---

## Encoding corruption (discussed, not yet fixed)

Two PDF ligature corruption patterns found in `card_text`:

| Pattern | Example | Cause |
|---|---|---|
| Ɵ → ti | `pracƟƟoners`, `impacƟng` | Font encoding: "ti" glyph mapped to non-standard Unicode |
| 7 → ti | `nega7ve`, `func7on` | Font encoding: "ti" glyph at byte 0x37 (ASCII "7") |

**Fix plan** (not yet implemented):
- Direct substitution for Ɵ (never appears in English health prose)
- `re.sub(r'(?<=[a-zA-Z])7(?=[a-zA-Z])', 'ti', text)` for the digit case
- Goes in `clean/clean_text.py` as a pre-processing pass
- After fix: re-run `build_corpus.py`; gold labels survive via `doc_id` join

---

## Pending work (priority order)

1. **Ligature fix** — `clean/clean_text.py`, then rebuild corpus.csv
2. **`chunk_reports --rebuild`** — write `data/processed/rag_chunks.jsonl`
   (321 chunks / 91 rec chunks confirmed in dry-run)
3. **`org_lookup.csv` expansion** — 177 docs with unknown stakeholder; labour
   unions, coalitions, opioid-study orgs not yet mapped
4. **Kappa check** — `second_pass_sheet.csv` (50 docs); relabel independently
   a few days after first pass, compute Cohen's kappa
5. **Course repo sync** — `bash sync_to_course.sh` once rag_chunks written
6. **Model work** — in separate course repo; sync processed outputs across

---

## Key commands to resume

```bash
# Rebuild corpus after any rule change
uv run python -m clean.build_corpus

# Write RAG chunks
uv run python -m clean.chunk_reports --rebuild

# QA report
uv run python -m qa.qa_report

# Validate splits
uv run python -m clean.make_splits --check

# Re-run splits (e.g. after corpus changes)
uv run python -m clean.make_splits

# Sync to course repo
bash sync_to_course.sh
```
