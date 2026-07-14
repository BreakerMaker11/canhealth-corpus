# CanHealth Corpus

A scraping and cleaning pipeline that builds a corpus of public Canadian
health-policy documents for an LLM course project (topic classification,
summarization, RAG). This repo contains **data collection and preparation
only**; modelling code lives in a separate course repo and is synced via
`sync_to_course.sh`.

**Current tag:** `v0.3-gold-frozen` — corpus rebuilt, gold set frozen,
train/dev splits written.

---

## Corpus at a glance

| | Count |
|---|---|
| Raw documents harvested | 562 |
| Extracted successfully | 538 |
| Classifier corpus (briefs, `corpus.csv`) | 506 |
| Gold-labeled docs (`gold_test.csv`) | 217 |
| Train set (`train.csv`) | 258 |
| Dev set (`dev.csv`) | 28 |
| Unlabeled fraction (weak labels) | 13.6% |

### Document types in the corpus

| source_type | Description | Count | Used for |
|---|---|---|---|
| `hesa_brief` | Stakeholder written submissions (PDFs) | 506 | Classifier train / eval |
| `hesa_report` | HESA committee final reports (HTML) | 12 | RAG reference layer |
| `gov_response` | Government responses to report recommendations (HTML) | 6 | RAG reference layer |

Reports and gov responses are excluded from classifier training — they have no
single stakeholder and are used for RAG and policy-gap analysis only.

### Topic distribution (weak labels, briefs only)

| topic | count | % |
|---|---|---|
| public_health | 176 | 34.8% |
| pharmacare | 95 | 18.8% |
| womens_health | 80 | 15.8% |
| unlabeled | 69 | 13.6% |
| workforce | 27 | 5.3% |
| mental_health | 24 | 4.7% |
| indigenous_health | 14 | 2.8% |
| cancer | 11 | 2.2% |
| childrens_health | 10 | 2.0% |

**Topic codebook:**

| code | Covers | Excludes |
|---|---|---|
| `public_health` | Pandemic preparedness, infectious disease, immunization, substance use, opioids, harm reduction, drug policy, overdose | Mental illness |
| `pharmacare` | Drug coverage, drug pricing, patented medicines, biosimilars, formularies, medical devices | Drug addiction policy |
| `womens_health` | Reproductive health, maternal health, menopause, women's cancers, breast/cervical cancer | All-population cancer policy |
| `mental_health` | Mental illness, mood/anxiety disorders, depression, psychiatric services, suicide prevention | Substance use, addiction |
| `workforce` | Physician/nurse/allied-health supply, credential recognition, internationally trained professionals, scope of practice | |
| `indigenous_health` | First Nations, Inuit, Métis health, Jordan's Principle, NIHB | |
| `cancer` | Cancer prevention, treatment, screening, oncology (non-gendered) | Women's cancers (→ womens_health) |
| `childrens_health` | Paediatric health, neonatal, children's developmental health | |
| `unlabeled` | No confident topic from card text or study title | |
| `other_none` | Gold-labeled only — off-topic (civil liberties briefs, equity submissions with no policy ask) | |

### Gold set (217 hand-labeled docs)

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

Labeled in one pass from a stratified 200-doc sample plus a 17-doc top-up
targeting `mental_health` (post-boundary redraw) and `indigenous_health`
(high weak-label noise). Codebook, `topic_rules.yaml`, and gold labels were
aligned in a single session pre-freeze. `other_none` = 46 (21%) is the noise
floor; almost entirely COVID civil-rights/anti-mandate briefs from the
Emergency Situation study that carry no health-policy ask.

### Stakeholder types (`org_lookup.csv`)

| type | Description |
|---|---|
| `physician_org` | Medical and allied-health professional associations (physicians, nurses, dental, allied health) |
| `patient_advocacy` | Patient and disease-specific advocacy groups |
| `government` | Federal/provincial/territorial government bodies |
| `industry` | Pharmaceutical, device, and insurance industry associations |
| `academic` | Universities, research institutes, think tanks |
| `individual` | Personal submissions from members of the public |

35% of briefs have `stakeholder_type = unknown` — orgs not yet in
`org_lookup.csv` (labour unions, coalitions, ad-hoc groups from opioid and
pharmacare studies).

---

## Pipeline stages

| # | Stage | Script | Status |
|---|---|---|---|
| 1 | Inventory | `inventory.csv` (manual) | Done |
| 2 | Harvest | `scrape/hesa.py` | Done — 562 docs / 179 MB |
| 3 | Extract text | `clean/extract_text.py` | Done — 538 extracted |
| 4 | Clean text | `clean/clean_text.py` | Done |
| 5 | Dedupe | `clean/dedupe.py` | Done — 39 docs in 17 clusters |
| 6 | Build corpus | `clean/build_corpus.py` | Done — 506 rows, v0.3 |
| 7 | Chunk reports | `clean/chunk_reports.py` | Script done; `--rebuild` pending |
| 8 | QA report | `qa/qa_report.py` | Done |
| — | Gold set | `qa/make_gold_sheet.py` | Frozen at v0.3-gold-frozen |
| — | Train/dev splits | `clean/make_splits.py` | Done — 258 train / 28 dev |

### Known issues / pending fixes

- **Ligature encoding corruption** — Some PDFs encode the "ti" glyph as Ɵ
  (font encoding artifact) or as "7" (byte 0x37 in font encoding table),
  producing words like `pracƟƟoners` or `nega7ve`. Fix belongs in
  `clean/clean_text.py` as a pre-processing substitution pass; `data/raw/`
  is immutable. Present in some `card_text` values in current corpus.
- **`rag_chunks.jsonl` not yet written** — `clean/chunk_reports.py --rebuild`
  has been dry-run (321 chunks / 91 recommendation chunks confirmed) but not
  written to disk.
- **35% unknown stakeholder** — `org_lookup.csv` needs expansion for labour
  unions, coalitions, and cross-study orgs.

---

## Harvest details (Stage 2)

| Session | Studies | Briefs | Reports | Gov responses | Total |
|---|---|---|---|---|---|
| 44-1 | 16 | 474 | 10 | 4 | 488 |
| 45-1 | 3 | 70 | 2 | 2 | 74 |
| **Both** | **19** | **544** | **12** | **6** | **562** |

Studies are filtered from the HESA Work tab. Procedural items (estimates,
departmental plans, routine bills) are discarded to `discarded_studies.csv`.
Health-relevant bills are re-admitted via `studies_whitelist.csv`:

| study_activity_id | title | note |
|---|---|---|
| 12729090 | Bill C-64 (Pharmacare) | pharmacare topic coverage |
| 12623576 | Bill C-31 (Dental) | dental/workforce coverage |
| 12464694 | Bill C-293 (Pandemic Prevention) | public health coverage |

### Text extraction outcomes (Stage 3)

| Outcome | Count |
|---|---|
| Extracted successfully | 538 |
| `needs_ocr` — multi-page, < 200 chars extracted | 10 |
| Empty — single-page scanned PDFs | 9 |
| Empty — sparse HTML with no recoverable text | 5 |

The 19 OCR-needed / empty docs are reserved as week-7 multimodal samples
(Tesseract vs vision-model comparison). Language: 536 English · 25 unknown ·
1 misdetected (garbled FASD PDF encoding).

Word-count distribution (538 docs): min 2 · median 1,499 · p90 2,547 · max
36,371.

---

## Weak-labeling rules (`topic_rules.yaml`)

Content-first labeling: card text is matched first, study title used as
fallback only.

- **Strong keywords** (≥ 1 hit labels the doc): highly specific terms that
  cannot appear incidentally in a different-topic brief (e.g. `pharmacare`,
  `opioid`, `oncology`, `first nations`, `depression`).
- **Regular keywords** (≥ 2 distinct hits required): broader terms that need
  corroboration (e.g. `mental health`, `pandemic`, `pharmaceutical`).
- **Title-exempt studies**: COVID Emergency Situation study, Immigration/ITP
  study, and Children's Health study — study title too broad to assign a
  topic; content-only matching applies.

Rule order matters: first topic to reach threshold wins. Specific topics
(e.g. `indigenous_health`) appear before broad ones (e.g. `public_health`).

---

## Train / dev / test splits

| Split | Rows | Source |
|---|---|---|
| `train.csv` | 258 | Weak-labeled, gold-excluded |
| `dev.csv` | 28 | Weak-labeled, gold-excluded |
| `gold_test.csv` | 217 | Hand-labeled (label_source=gold) |

**Split constraints enforced by `clean/make_splits.py`:**
- All gold `doc_id`s excluded from train and dev entirely.
- Near-duplicate clusters containing any gold member excluded from both
  splits (contamination prevention): 4 clusters excluded.
- Remaining safe clusters (13) assigned intact to one split only — no cluster
  straddles both sides.
- Stratified 90/10 by `topic_seed`; fixed seed 42; deterministic (asserted
  by running twice).

**Class notes:**
- `cancer` has 0 train docs (all eligible went to dev or gold) — handle at
  training time (merge with `other` or eval-only).
- `mental_health` and `indigenous_health` are eval-only classes — all weak
  examples ended up in gold; no training signal from weak labels.

---

## Repo layout

```
scrape/
  hesa.py                    # Harvest briefs, reports, gov responses
  fetch_fixture.py           # Save sample pages to fixtures/ (one-shot)
  explore_fixture.py         # Parser development against local HTML
  harvest_summary.py         # Post-harvest counts and raw size
clean/
  extract_text.py            # Stage 3: PyMuPDF (blocks mode) + HTML fallback
  clean_text.py              # Stage 4: ligatures, headers/footers, masking
  dedupe.py                  # Stage 5: exact hash dedupe + near-dup clusters
  build_corpus.py            # Stage 6: corpus.csv from cleaned text
  chunk_reports.py           # Stage 7: RAG chunks for reports/responses
  make_splits.py             # Train/dev splits with cluster + gold constraints
qa/
  qa_report.py               # Stage 8: counts, lengths, languages, samples
  make_gold_sheet.py         # Stratified gold sampling sheet
fixtures/                    # Committed sample HTML pages for parser dev
  hesa441_work_tab.html
  study_workforce.html
  study_workforce_briefs.html
  hesa441_report10.html
  corpus_fixture.csv         # Synthetic fixture for make_splits.py dev
  gold_test_fixture.csv
data/raw/                    # Fetched PDFs and HTML — gitignored, 179 MB
data/interim/                # Extracted and cleaned text — gitignored
data/processed/              # Final outputs — committed
  corpus.csv                 # 506 rows, weak labels
  gold_test.csv              # 217 rows, hand-labeled, label_source=gold
  train.csv                  # 258 rows
  dev.csv                    # 28 rows
  gold_labels.csv            # Raw labeling sheet (filled)
  topup_sheet.csv            # Top-up labels (mental_health + indigenous)
  labeling_sheet.csv         # Original 200-doc gold sample sheet
  second_pass_sheet.csv      # 50-doc kappa relabel sheet
manifest.jsonl               # One JSON line per document — committed
failed.csv                   # Fetch/extract failures for retry
discarded_studies.csv        # Studies skipped by procedural filter
studies_whitelist.csv        # study_activity_ids that bypass the filter
inventory.csv                # Listing-page URLs (seed for scraper)
org_lookup.csv               # org → stakeholder_type mapping
topic_rules.yaml             # Weak-labeling keyword rules
decisions.md                 # Decision log (append each session)
sync_to_course.sh            # Copy data/processed/ to course repo
```

---

## Running the pipeline

```bash
# Install dependencies
uv sync

# Stage 2 — Harvest
uv run python -m scrape.hesa --session 44-1 --dry-run   # preview
uv run python -m scrape.hesa --session 44-1             # full run
uv run python -m scrape.hesa --session 45-1

# Stage 3 — Extract text
uv run python -m clean.extract_text --limit 5 --dry-run
uv run python -m clean.extract_text

# Stage 4 — Clean text
uv run python -m clean.clean_text --limit 5 --dry-run
uv run python -m clean.clean_text

# Stage 5 — Dedupe
uv run python -m clean.dedupe

# Stage 6 — Build corpus (deterministic; re-run any time)
uv run python -m clean.build_corpus

# Stage 7 — Chunk reports for RAG
uv run python -m clean.chunk_reports --dry-run          # preview
uv run python -m clean.chunk_reports --rebuild          # write rag_chunks.jsonl

# Stage 8 — QA report (run after any cleaning change)
uv run python -m qa.qa_report

# Gold sheet (one-off; gold is frozen at v0.3-gold-frozen)
uv run python -m qa.make_gold_sheet --dry-run

# Train/dev splits
uv run python -m clean.make_splits --dry-run            # preview
uv run python -m clean.make_splits                      # write train.csv / dev.csv
uv run python -m clean.make_splits --check              # validate existing splits

# Sync processed outputs to course repo
bash sync_to_course.sh
```

---

## Manifest schema

Each line of `manifest.jsonl` is one JSON object:

```json
{
  "doc_id": "hesa441_canadas_health_workforce_0001",
  "source_url": "https://www.ourcommons.ca/...",
  "fetch_date": "2026-07-05",
  "source_type": "hesa_brief",
  "organization": "Canadian Nurses Association",
  "study_or_consultation_title": "Canada's Health Workforce",
  "parliament_session": "44-1",
  "language": "en",
  "file_type": "pdf",
  "needs_ocr": false,
  "published_date": "2022-03-02"
}
```

Reports and gov responses carry `file_type: html` plus a `pdf_url` field.
Joint briefs carry `joint: true` and list all orgs separated by ` / `.

`source_type` values: `hesa_brief` · `hesa_report` · `gov_response`

---

## Data sources

### HESA (active)

The [Standing Committee on Health (HESA)](https://www.ourcommons.ca/committees/en/HESA)
organizes its Work tab by study. For each substantive study the scraper
collects **Written Briefs** (PDF, stakeholder submissions), the **Committee
Report** (HTML via DocumentViewer), and the **Government Response** (HTML).

Evidence transcripts, Minutes, Notices of Meeting, and Webcasts are out of
scope (multi-speaker, no single stakeholder label).

### Deferred (specs retained in CLAUDE.md)

- Open Government Portal consultations (CKAN API)
- CMA PolicyBase
- Statistics Canada health analyses

---

## Hard rules (summary)

- **Politeness**: 1 req/sec minimum; back off on 429/403.
- **Fetch once**: never re-download files already in `data/raw/`.
- **Raw is immutable**: `data/raw/` is never modified by cleaning code.
- **Public data only**: no login-walled content, no personal data beyond
  published organization names.
- **Gold is frozen**: `gold_test.csv` is hand-made and never regenerated.
  `label_source=gold` rows are never overwritten by pipeline code.

See `CLAUDE.md` for the full specification.
