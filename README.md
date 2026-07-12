# CanHealth Corpus

A scraping and cleaning pipeline that builds a corpus of public Canadian health-policy documents for an LLM course project (topic classification, summarization, RAG). This repo contains **data collection and preparation only**; modelling code lives in a separate course repo.

## Current state

### Stage 2 — Harvest (done)

| Session | Studies | Briefs (PDF) | Reports (HTML) | Gov responses (HTML) | Total |
|---------|---------|-------------|----------------|----------------------|-------|
| 44-1    | 16      | 474         | 10             | 4                    | 488   |
| 45-1    | 3       | 70          | 2              | 2                    | 74    |
| **Both**| **19**  | **544**     | **12**         | **6**                | **562 docs / 179 MB** |

Studies are filtered from the HESA Work tab: procedural items (estimates, departmental plans, routine bills) are discarded to `discarded_studies.csv`; specific health-relevant bills can be re-admitted via `studies_whitelist.csv` (currently: C-64 pharmacare, C-31 dental, C-293 pandemic prevention).

### Stage 3 — Extract text (done)

| Outcome | Count |
|---------|-------|
| Extracted successfully | 538 |
| needs_ocr (multi-page, < 200 chars extracted) | 10 |
| EMPTY — sparse HTML reports, no pdf_url | 5 |
| EMPTY — single-page scanned PDFs | 9 |

Language breakdown: 536 English · 25 unknown · 1 misdetected (garbled PDF encoding).

Word-count distribution (538 docs): min 2 · median 1,499 · p90 2,547 · max 36,371.

The 10 needs_ocr docs and 9 scanned-PDF empties are reserved as week-7 multimodal samples. The 5 sparse HTML reports without a linked PDF have no recoverable text.

## Pipeline stages

| Stage | Script | Status |
|-------|--------|--------|
| 1. Inventory | `inventory.csv` (manual) | Done |
| 2. Harvest | `scrape/hesa.py` | Done — 562 docs |
| 3. Extract text | `clean/extract_text.py` | Done — 538 extracted |
| 4. Clean text | `clean/clean_text.py` | Not started |
| 5. Dedupe | `clean/dedupe.py` | Not started |
| 6. Build corpus | `clean/build_corpus.py` | Not started |
| 7. Chunk reports | `clean/chunk_reports.py` | Not started |
| 8. QA report | `qa/qa_report.py` | Not started |

## Repo layout

```
scrape/              # one script per source
  hesa.py            # HESA briefs, reports, gov responses
  fetch_fixture.py   # one-shot: save sample pages to fixtures/
  explore_fixture.py # parser development against local fixtures
  harvest_summary.py # post-harvest counts, failed.csv, raw size
clean/               # text extraction and cleaning scripts
  extract_text.py    # Stage 3: PDF via PyMuPDF, HTML via pdf_url fallback
  clean_text.py      # Stage 4 (TODO)
  dedupe.py          # Stage 5 (TODO)
  build_corpus.py    # Stage 6 (TODO)
  chunk_reports.py   # Stage 7 (TODO)
qa/                  # qa_report.py (TODO)
fixtures/            # committed sample HTML pages for parser development
  hesa441_work_tab.html          # HESA 44-1 Work tab (study list)
  study_workforce.html           # study activity page (Canada's Health Workforce)
  study_workforce_briefs.html    # GetBriefs AJAX fragment (22 briefs)
  hesa441_report10.html          # DocumentViewer report page (confirmed PDF link pattern)
data/raw/            # fetched PDFs and HTML — gitignored, 179 MB
data/interim/        # extracted per-doc text — gitignored, 538 .txt files
data/processed/      # final CSVs/JSONL for course repo — committed (TODO)
manifest.jsonl       # one JSON line per document — committed
failed.csv           # fetch/extract failures for retry — committed
discarded_studies.csv # studies skipped by procedural filter — committed
studies_whitelist.csv # study_activity_ids that bypass the filter — committed
inventory.csv        # listing-page URLs per source (seed for scraper)
sync_to_course.sh    # copies data/processed/ into the course repo
```

## Running the pipeline

```bash
# Install dependencies
uv sync

# Harvest (Stage 2)
uv run python -m scrape.hesa --session 44-1 --dry-run   # preview
uv run python -m scrape.hesa --session 44-1             # full run
uv run python -m scrape.hesa --session 45-1

# Post-harvest summary
uv run python scrape/harvest_summary.py

# Extract text (Stage 3)
uv run python -m clean.extract_text --limit 5 --dry-run  # test
uv run python -m clean.extract_text                      # full run
```

## Manifest schema

Each line of `manifest.jsonl` is a JSON object:

```json
{
  "doc_id": "hesa441_canadas_health_workforce_0001",
  "source_url": "https://www.ourcommons.ca/Content/Committee/441/HESA/Brief/BR.../...-e.pdf",
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

Reports and gov_responses use `file_type: html` with an additional `pdf_url` field; the full text is extracted from that PDF at Stage 3. Joint briefs include `joint: true` and list all submitting organizations separated by ` / `.

`source_type` values: `hesa_brief` · `hesa_report` · `gov_response`

## Data sources

### HESA (active)

The [Standing Committee on Health (HESA)](https://www.ourcommons.ca/committees/en/HESA) organizes its Work tab by study. For each substantive study the scraper collects:

- **Written Briefs** — policy papers submitted by external stakeholders (PDFs, `hesa_brief`)
- **Committee Report** — final report tabled in the House (HTML via DocumentViewer, `hesa_report`)
- **Government Response** — Minister's formal response to report recommendations (HTML, `gov_response`)

Evidence/transcripts, Minutes, Notices of Meeting, and Webcasts are out of scope (multi-speaker, no single stakeholder label).

### Deferred (not yet implemented)

- Open Government Portal consultations (CKAN API)
- CMA PolicyBase
- Statistics Canada health analyses

## Whitelisting procedural studies

Add rows to `studies_whitelist.csv` to admit studies that would otherwise be discarded by the title filter (e.g., health-relevant bills):

```
study_activity_id,study_title,note
12729090,"Bill C-64, An Act respecting pharmacare",pharmacare topic coverage
```
