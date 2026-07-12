# Decision Log — CanHealth Corpus

One line per decision: what was decided, why, and what it affected.
Append at the end of every working session. This log is the raw material for
the final report's data & methods narrative.

## Scoping
- Project scoped to Canadian federal health-policy stakeholder input (asks →
  committee recommendations → government commitments) after surveying sources;
  raw patient/physician free-text feedback rejected as inaccessible behind
  health-authority agreements or scraping ToS.
- Classification sources narrowed to HESA only (time constraint); Open
  Government consultations, CMA PolicyBase, StatCan marked DEFERRED in
  CLAUDE.md with specs retained for reactivation.
- Separate corpus repo from course repo: course repo is instructor-owned and
  synced weekly; corpus repo commits code + manifest + processed outputs,
  gitignores raw/interim; sync via copy script.

## Harvest
- HESA Work tab only: briefs + reports + gov responses. Transcripts skipped
  (multi-speaker, no single stakeholder/topic label — noted as future work);
  minutes/notices/webcasts skipped (procedural, no substantive positions).
- Procedural-title filter with committed whitelist by study_activity_id;
  discards logged. Whitelisted: C-64 pharmacare, C-31 dental, C-293 pandemic
  (topic coverage). Seven condition-specific bills left discarded to avoid an
  unlabeled pile (revisit if a topic runs short).
- Sessions 44-1 + 45-1 harvested (562 docs, 0 failures). 44-1 prioritized for
  complete brief→report→response chains; 45-1 added ~74 docs and diluted the
  COVID share. Session encoded in URL path; parliament_session in manifest.
- Reports/gov responses: DocumentViewer HTML preferred as text source
  (structured headings/recommendations); pdf_url kept for provenance;
  cited by section/recommendation instead of page.

## Taxonomy & weak labels (topic_rules.yaml)
- Ten topics. womens_health added (69 briefs unmatched by original eight);
  public_health added (C-293 + COVID material); pharmacare widened to drugs &
  medical devices (patented medicines, devices studies); keywords added for
  pharmaceutical sovereignty and antimicrobial resistance after 45-1 harvest.
- Title-exempt studies (topic from card_text, not study title): COVID
  emergency study (title = occasion, not topic; would have mislabeled 160
  briefs as public_health) and the immigration/healthcare study (genuinely
  mixed workforce vs access briefs).
- Stakeholder type gains sixth class: individual (HESA accepts personal
  briefs; forcing them into org categories would be wrong; distinct voice).
- First-match-wins rule order = precedence (specific topics above broad ones).

## Extraction & cleaning
- Stage separation preserved: no network in clean/; sparse-HTML repair moved
  to a harvest-side command (extraction stays offline and rerunnable).
- OCR layer added (ocrmypdf → interim/ocr/, extractor prefers it, raw
  immutable) after the Workforce gov_response — a gap-analysis chain document —
  turned out to be scanned. Same doc doubles as the week-7 multimodal sample
  (Tesseract vs Gemma 4 vision comparison).
- Single-page empty PDFs folded into needs_ocr (the 200-char rule originally
  covered multi-page only); garbled FASD PDF (fake "de" langdetect) routed to
  needs_ocr.
- Contact scrubbing ([CONTACT] for emails/phones/addresses, signature blocks
  stripped): zero topic signal, embedding noise, hygiene for a possibly
  public corpus. Names in manifest metadata retained for attribution.
- layout_mangled flag (short-line fraction + digit density, tuned on the 988
  infographic brief as known-positive): detect, report, never auto-drop.
- 150-word floor dropped 17 docs, mostly short personal COVID letters — the
  rule trims the individual-citizen voice at its thinnest; noted as a
  limitation.
- Org masking: submitting org + parenthetically defined acronym alias → [ORG]
  (CDA case), word-boundary matched so other orgs' acronyms (CDAA) survive;
  masked variants generated for briefs only (masking the committee's own name
  in reports produced nonsense).
- Title-echo running headers (report title repeated as multi-line page header)
  stripped by matching the document's own first-page title — found by manual
  verification of the breast-cancer report; the >50%-of-pages rule missed the
  split-line variant.
- Report cleaning verified end-to-end on the breast-cancer report: 15,192 →
  5,378 words, body chapters intact, 13 recommendations preserved once
  (duplicated copies in ToC/back matter correctly removed).

## Classification design
- Unit = card_text (~250 words: title + head of org-masked text), one card per
  document. Rationale: signal concentration (briefs front-load thesis), fair
  three-way method comparison on identical inputs, BERT-family 512-token
  baseline fits, cheap iteration. RAG uses separate 300–800-token
  heading-bounded chunks — different question, different unit.
- Tiered card builder (summary section → head+recommend-sentences → plain
  head) implemented behind a --card flag; simple is default; tiered vs simple
  is a planned week-4 ablation on the gold set.
- Single-label topic for the course; multi-label named as extension (CMA
  ministerial letter kept as the out-of-distribution week-8 demo document —
  spans ~8 topics, demonstrates the limit).

## Evaluation plan
- Gold set ~200 briefs, stratified by topic and stakeholder type, frozen
  before model work; macro-F1 (imbalance observed: COVID study = 1/3 of
  briefs pre-correction); intra-annotator kappa on a 50-doc relabel (solo
  annotator; second-pass two weeks later).
- COVID downsampling / per-study cap at train time noted as imbalance
  handling for the write-up.

## Labeling redesign (2026-07-11)

**Cross-cutting topics removed:** `digital_health`, `wait_times`, `funding`,
`primary_care` removed from topic_rules.yaml. These themes appear as
subordinate concerns across virtually every brief (e.g., pharmacare briefs
mention drug funding; workforce briefs mention primary-care access gaps).
Single-label keyword matching on cross-cutting themes produces noisy labels
driven by incidental co-occurrence rather than a document's primary subject.
Reserved for a multi-label extension (binary vector per topic, one classifier
head per label) planned for Week 9.

**Content-first labeling:** Changed from title-first to content-first strategy.
Old: study title (1+ keyword) → card_text (1+ keyword) → unlabeled.
New: card_text (≥2 distinct keyword hits) → study title (1+ keyword,
non-exempt) → unlabeled. Rationale: title-first assigned the same study-level
label to all briefs in a study regardless of the brief's actual content,
conflating topic of study with topic of the brief. The ≥2 content-hit
threshold trades recall for precision; the study-title fallback preserves recall
for unambiguous studies. Unlabeled fraction rose to ~21 %; addressed by strong
keywords and keyword expansion (see below).

**Card construction fix:** `card_text` is now the first 250 words of the
org-masked document text only — no study title injected. Rationale: injecting
the study title into the card leaks the title-derived label directly into the
model input, creating a shortcut that will not generalize to out-of-study
documents at inference time. The `study_title` column stays in corpus.csv for
stratification and analysis; it is never part of the model input.

**Strong-keyword support (implemented 2026-07-11):** A `strong_keywords` list
per topic in topic_rules.yaml; any match labels the doc without needing ≥2.
Regular `keywords` still require ≥2 distinct hits. Strong keywords are reserved
for terms so specific they cannot appear incidentally in a brief about a
different topic: e.g., `pharmacare`, `first nations`, `paediatric`, `oncology`,
`opioid`, `biosimilar`, `internationally trained`, `carcinogen`. TF-IDF mining
over 107 unlabeled cards informed which keywords to promote. New keyword added:
`long covid` (public_health strong), `internationally trained` (workforce strong),
`indigenous health` (indigenous_health strong), `carcinogen` (cancer strong),
`medical device` moved to pharmacare strong_keywords.

**Final corpus (v0.2, 2026-07-11):** 506 rows, 8 topics, content-first labeling
with strong keywords. Unlabeled fraction: 14.4 % (73 docs — below the 15 %
target). Topic distribution: public_health 28.1 %, pharmacare 19.8 %,
womens_health 16.4 %, mental_health 8.9 %, workforce 5.5 %, indigenous_health
2.8 %, cancer 2.2 %, childrens_health 2.0 %. Outputs deterministic (run ×2,
identical counts). Corpus committed as v0.2-corpus.
