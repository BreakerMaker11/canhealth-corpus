# CanHealth Corpus — Scraping & Cleaning Pipeline

Personal project: build a corpus of public Canadian health-policy documents
(parliamentary briefs, consultation records, policy/position statements) for an
LLM course project (topic classification + summarization + RAG). This repo
contains ONLY data collection and preparation. Modelling code lives in a
separate course repo; processed outputs are copied there via `sync_to_course.sh`.

## Current scope (July 2026)

- **ACTIVE — HESA only**: briefs (`hesa_brief`, PDF), plus reports and
  government responses (`hesa_report`, `gov_response`) as HTML — fetch the
  DocumentViewer HTML page as the text source (`file_type: html`, headings and
  recommendations are structured markup), and record the linked PDF URL in a
  `pdf_url` manifest field for provenance. These docs are cited by
  section/recommendation number, not page number.
- **Whitelist**: `studies_whitelist.csv` (committed, columns
  `study_activity_id,study_title,note`) overrides the procedural title filter
  by study_activity_id; whitelisted studies are logged as "whitelisted", not
  discarded.
- **DEFERRED — do not build**: Open Government consultations (CKAN),
  CMA PolicyBase, StatCan. Their specs below stay for future reactivation;
  do not implement or scrape them unless explicitly asked.
- Classification corpus = HESA briefs only. Reports/gov responses remain
  reference-layer (RAG / gap analysis), never classifier train/eval.

## Hard rules (never violate)

- **Politeness**: max 1 request/second per host (`time.sleep(1)` minimum between
  requests). Set User-Agent to
  `"canhealth-corpus/0.1 (personal research; contact: <MY_EMAIL>)"`.
  Check robots.txt per host before adding a new source. Back off (30s, then stop
  and report) on HTTP 429/403 — never retry-loop against a refusing server.
- **Fetch once**: never re-download a file that exists in `data/raw/`. All
  network code must be idempotent and resumable.
- **Raw is immutable**: files in `data/raw/` are never modified or deleted by
  cleaning code. Cleaning reads raw, writes to `data/interim/` and
  `data/processed/`.
- **Every scraper takes `--limit N` and `--dry-run` flags.** Dry-run prints what
  would be fetched without network writes. Develop and debug with `--limit 5`.
- **Public data only**: only publicly posted documents (Parliament, Government
  of Canada, publicly accessible policy repositories). No login-walled content,
  no personal data beyond organization names already published with the
  documents.
- **Develop parsers against fixtures**: save one sample listing page and one
  sample document page as HTML into `fixtures/` and write/iterate parsers
  against those files, not against the live site.

## Repo layout

```
scrape/            # one script per source: hesa.py, consultations.py, policy_docs.py
clean/             # extract_text.py, clean_text.py, build_corpus.py, dedupe.py, chunk_reports.py
qa/                # qa_report.py
fixtures/          # saved sample HTML pages for parser development (committed)
data/raw/          # fetched PDFs/HTML, named {doc_id}.{ext}   — GITIGNORED
data/interim/      # extracted per-doc text {doc_id}.txt       — GITIGNORED
data/processed/    # final CSVs / JSONL for the course repo    — committed
manifest.jsonl     # one line per document                     — committed
failed.csv         # fetch/extract failures for retry          — committed
sync_to_course.sh  # copies data/processed/* into <course_repo>/data/health/
```

## Manifest schema (`manifest.jsonl`, append-only)

One JSON object per document:

```json
{"doc_id": "hesa_workforce_0042", "source_url": "...", "fetch_date": "2026-07-05",
 "source_type": "hesa_brief", "organization": "Canadian Nurses Association",
 "study_or_consultation_title": "Canada's Health Workforce", "language": null,
 "file_type": "pdf", "needs_ocr": null}
```

- `doc_id` format: `{source}_{studyslug}_{NNNN}` — stable, never renamed.
- `source_type` ∈ `hesa_brief | hesa_report | gov_response | consultation |
  policy | statcan_analysis`.
- `parliament_session` (e.g. `"44-1"`) for all HESA documents; null otherwise.
  The session is part of ourcommons.ca URL paths
  (`/DocumentViewer/en/{parl}-{session}/HESA/...`) — build session-specific
  URLs directly; never rely on the site's default (current) session. Verify
  listing-page session parameters from a saved fixture, not guesswork.
  Priority: 44-1 (completed studies with full brief→report→response chains),
  then 45-1 (briefs; reports/responses may not exist yet for active studies).
  `inventory.csv` carries a `parl_session` column per study.
- `language` and `needs_ocr` are filled in by the extract stage, not the scraper.
- Scrapers must not write duplicate `doc_id` or duplicate `source_url` lines;
  check the manifest before fetching.

## Pipeline stages (run in order; each stage re-runnable)

1. **Inventory** (manual + API): `inventory.csv` lists listing-page URLs per
   source. Before parsing HTML, check whether the site offers a structured
   export (CSV/JSON/XML endpoint) and prefer it. For consultations and
   federal policy documents, use the Open Government Portal CKAN Action API
   (GET-only, params in the URL):
   `https://open.canada.ca/data/en/api/3/action/package_search?q=<terms>&fq=organization:<org>&rows=100&start=<n>`.
   Filter to prose resources (PDF/HTML reports, "What We Heard"); skip
   dataset/table resources. Store the CKAN package id in the manifest as
   `ckan_package_id` (null for non-CKAN sources). HESA briefs are not on the
   portal and keep their own HTML scraper.
2. **Harvest** (`scrape/*.py`): read inventory → fetch listings → resolve
   document links + organization names → download to `data/raw/` → append
   manifest. Log failures to `failed.csv`; never crash the run on one bad doc.
   **HESA scope — Work tab only**: harvest all Written Briefs per study
   (`hesa_brief`), plus each study's final Report (`hesa_report`) and the
   Government Response (`gov_response`). Do NOT harvest Evidence/transcripts,
   Minutes, Notices of Meeting, or Webcasts (multi-speaker transcripts have no
   single stakeholder/topic label; noted as future work, not in scope).
3. **Extract** (`clean/extract_text.py`): PyMuPDF, blocks mode sorted by
   position. Write a page-boundary marker line `[[page N]]` between pages
   (needed later for page-level RAG citations). If extracted text < 200 chars
   for a multi-page PDF, set
   `needs_ocr=true` and skip (these become week-7 multimodal samples).
   Detect language (langdetect on first 1,000 chars), record in manifest.
4. **Clean** (`clean/clean_text.py`): strip repeating headers/footers (lines on
   >50% of pages), fix hyphenated line-breaks and ligatures, normalize
   whitespace, drop ToC and "About <org>" boilerplate, drop docs < 150 words.
   For `hesa_report` / `gov_response`: additionally strip front matter
   (committee membership lists, procedural boilerplate) and back-matter
   appendices (lists of witnesses and briefs received, dissenting/supplementary
   opinions). Preserve `[[page N]]` markers through all cleaning.
5. **Dedupe** (`clean/dedupe.py`): exact dedupe by normalized-text hash; report
   near-duplicates (embedding cosine > 0.9) for manual review, especially
   across future train/test splits.
6. **Build corpus** (`clean/build_corpus.py`): write
   `data/processed/corpus.csv`, one row per doc:
   `doc_id, Name, org, source_type, date, language, card_text,
   text_org_masked_path, full_text_path, topic_seed, stakeholder_type,
   label_source`.
   - `Name` = document title (column name kept for course-repo compatibility).
   - `card_text` = title + first ~250 words of org-masked text (classification unit).
   - `text_org_masked` = full text with submitting-org name/letterhead replaced
     by `[ORG]` (prevents label leakage for stakeholder-type classification).
   - `stakeholder_type` from `org_lookup.csv` (hand-maintained: org → one of
     physician_org | patient_advocacy | government | industry | academic).
   - `topic_seed` from study title + keyword rules in `topic_rules.yaml`.
   - `label_source` = `weak` for all rows here; gold labels live only in
     `data/processed/gold_test.csv` and are hand-made, never generated.
   - Classifier routing: only `hesa_brief`, `consultation`, and `policy` rows
     get train/eval labels. `hesa_report`, `gov_response`, and
     `statcan_analysis` rows are reference-layer only (no single stakeholder)
     and are excluded from classifier training and gold sampling.
7. **Chunk long documents** (`clean/chunk_reports.py`): for `hesa_report`,
   `gov_response`, `policy`, and `statcan_analysis` docs, produce
   `data/processed/rag_chunks.jsonl` for the RAG store. Chunk at heading
   boundaries into ~300–800 token pieces with small overlap; each chunk carries
   `doc_id`, section title, and page range (from `[[page N]]` markers). Never
   feed whole reports to a model. Special case: extract the report's "List of
   Recommendations" section as one chunk per numbered recommendation, and
   parse the Government Response's per-recommendation replies the same way,
   keyed by recommendation number — these aligned pairs are the seed of the
   future policy-gap analysis.
8. **QA** (`qa/qa_report.py`): print counts per source/topic/stakeholder,
   length distribution, language breakdown, dedupe stats, chunk counts per
   document type, and 5 random cleaned
   samples. Run after every cleaning change.

## Conventions

- Python 3.12, managed with `uv`; run everything as `uv run python -m ...`.
- Dependencies: requests, beautifulsoup4, pymupdf, langdetect, pandas.
- Small pure functions; each stage is a CLI script with `argparse`.
- Deterministic outputs: same inputs → same CSV (sort rows by doc_id).
- Never put credentials, cookies, or session tokens anywhere in this repo.
- Do not fabricate metadata: if organization or date can't be parsed, leave the
  field null and log it — no guessing.
- Tag the commit whenever `data/processed/` changes meaningfully
  (`v0.1-first-corpus`, `v0.2-gold-frozen`, ...).

## Definition of done (per session)

A stage is done when: it runs end-to-end with `--limit 5` and without limit,
`failed.csv` has been retried once, `qa_report.py` output has been shown to me,
and code + manifest + processed outputs are committed.
