# CanHealth Corpus — Data Pipeline Stages (Reference)

Goal: a corpus of public Canadian health-policy documents for an LLM course
project (topic classification + summarization + RAG), organized as three layers:

| Layer | What it contains | Sources | Used for |
|---|---|---|---|
| Asks | Stakeholder submissions arguing what *should be* | HESA committee briefs, consultation submissions | Classifier training + gold eval |
| Policy | What government currently provides | Health Canada/CIHI/provincial strategies, CMA PolicyBase | Classifier + RAG + gap analysis |
| Evidence | Descriptive facts about what *is* | StatCan Health analysis articles & fact sheets | Week 6 RAG reference layer + gap analysis (never classifier labels) |

## Stage 0 — Ground rules
Scrape politely (1 req/sec, descriptive User-Agent with contact email, respect
robots.txt, back off on 429/403). Fetch each file once into immutable
`data/raw/`. Every document gets a line in append-only `manifest.jsonl`
(doc_id, source_url, fetch_date, source_type, organization, study/consultation
title, language, file_type, needs_ocr). All scrapers support `--limit N` and
`--dry-run`. Keep this repo separate from the course repo; copy only
`data/processed/` outputs across via `sync_to_course.sh`.

## Stage 1 — Inventory (manual)
Build `inventory.csv` of listing URLs before writing any scraper:
- HESA: 3–5 completed studies matching the topic taxonomy; note briefs-listing URLs.
- Consultations + federal policy docs: use the Open Government Portal CKAN
  Action API instead of HTML scraping — GET-only, e.g.
  `https://open.canada.ca/data/en/api/3/action/package_search?q=<terms>&fq=organization:hc-sc&rows=100`
  (paginate with `start=`). Filter results to prose resources (PDF/HTML
  reports, consultations, "What We Heard"), skip data tables. JSON gives
  title, org, dates, language, licence, and resource download URLs — record
  the CKAN package id in the manifest. The consolidated consultations CSV
  remains a useful seed list. HESA briefs are NOT on the portal — Parliament
  still needs its own scraper.
- Policy: hand-curated list of 30–80 CMA PolicyBase, Health Canada, CIHI,
  provincial strategy URLs.
- StatCan (evidence layer): 50–100 recent Health *analysis* pages (Health
  Reports, fact sheets) — skip data tables; `source_type: statcan_analysis`.
Always check for structured exports (CSV/JSON/XML) before parsing HTML.
StatCan pages carry Dublin Core meta tags (dcterms.title/issued/subject) —
read metadata from tags, not layout.

## Stage 2 — Harvest (scripted, one script per source)
Read inventory → fetch listings (handle pagination) → resolve document links +
organization names → download to `data/raw/{doc_id}.{ext}` → append manifest.
doc_id format `{source}_{studyslug}_{NNNN}`, stable forever. Log failures to
`failed.csv` and retry once at the end; never crash on one bad document.
Develop parsers against saved pages in `fixtures/`, with `--limit 5`.
Targets: 400–800 HESA briefs, 50–150 consultation docs, 50–100 policy docs,
50–100 StatCan analysis pages.

## Stage 3 — Extract text
PyMuPDF (blocks mode, position-sorted) → `data/interim/{doc_id}.txt`.
Near-empty extraction on a multi-page PDF ⇒ `needs_ocr=true`, set aside (these
become week 7 multimodal samples). Detect language on first 1,000 chars,
record in manifest; core corpus = English.

## Stage 4 — Clean
Strip repeating headers/footers (lines on >50% of pages); fix hyphenated
line-breaks, ligatures, whitespace; drop ToC and "About <org>" boilerplate;
drop docs under ~150 words. Create `text_org_masked`: submitting-org name and
letterhead replaced with [ORG] (prevents label leakage for stakeholder-type
classification).

## Stage 5 — Structure and weak-label
Write `data/processed/corpus.csv`, one row per document:
`doc_id, Name (title), org, source_type, date, language, card_text,
text_org_masked_path, full_text_path, topic_seed, stakeholder_type,
label_source`.
- `card_text` = title + first ~250 words of masked text — the classification
  unit (fits small-model context).
- `stakeholder_type` from hand-made `org_lookup.csv` (physician_org |
  patient_advocacy | government | industry | academic).
- `topic_seed` from study/consultation title + keyword rules
  (`topic_rules.yaml`). Mark all as `label_source=weak` — trains the model,
  never grades it.
- StatCan rows are excluded from classifier train/eval; they feed the RAG
  chunk store instead (chunk → embed → ChromaDB in week 6).
Exact-dedupe by text hash (orgs resubmit identical briefs across studies).

## Stage 6 — Gold set and splits
Stratified-sample 250–300 asks/policy documents; hand-label `gold_label` in a
spreadsheet; relabel 50 two weeks later in a `second_pass` column for Cohen's
kappa. Freeze as `gold_test.csv` (tag the commit, e.g. `v0.2-gold-frozen`).
Split the remainder 90/10 train/dev. Run near-duplicate check across splits
(embedding cosine > 0.9) so no test document has a sibling in training.

## Stage 7 — QA report
`qa_report.py` prints: counts per source/topic/stakeholder, length
distribution, language breakdown, dedupe stats, 5 random cleaned samples.
Run after every cleaning change; a stage isn't done until the report is clean.

## Effort map
Stages 1–2: one weekend. Stages 3–5: second weekend. Stage 6: spread over
course weeks 3–4 (gold set must exist before the week 4 eval). Stage 7: rerun
continuously. Dependencies beyond the course repo: requests, beautifulsoup4,
pymupdf, langdetect, pandas.
