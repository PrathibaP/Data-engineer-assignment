# Job-postings market-intelligence pipeline

This pipeline scrapes data/ML/analytics/platform engineering roles in the US (including US-remote) from Greenhouse (Stripe, Airbnb, Databricks, Figma), Lever (Palantir, Plaid) and the current Hacker News "Who is hiring?" thread. An LLM extracts structured, evidence-backed records from each posting. The pipeline then deduplicates them across sources and stores them in SQLite with change history. Re-runs are incremental and idempotent.

## Run it

```bash
make setup                 # venv + pip install -r requirements.txt + .env from .env.example
# edit .env: ANTHROPIC_API_KEY (or LLM_PROVIDER=openai + OPENAI_API_KEY), USER_AGENT with your email
make run                   # == python -m pipeline run   (one command, live)
make test                  # 67 tests, all offline (mocked HTTP + fixtures)
make demo                  # offline: v1 -> v1 (idempotent) -> v2 (changes) on synthetic fixtures
make label && make evaluate   # 20-posting accuracy report (see data/evaluation/LABELING.md)
```
Outputs go to `data/jobs.db` (SQLite) and `data/exports/`. The exports are `jobs.csv` (the final dataset in the target schema), `canonical_records.csv`, `raw_source_records.csv`, `change_history.csv`, `dedup_review.csv`, `fetch_failures.csv` and `run_summary.json`. Logs are JSON lines in `logs/pipeline.jsonl`. Without a key, `LLM_PROVIDER=mock` runs a regex heuristic so the pipeline still works end to end. The mock is **not** an LLM and its numbers aren't reported as results.

## Architecture

```
sources/{greenhouse,lever,hackernews}.py   fetch + parse -> RawPosting   (deterministic pre-filter)
        │  scraper_base.PoliteClient: robots.txt, ≤1 req/s/domain (+Crawl-delay), backoff, raw cache
extractor.py     snippet -> LLM -> Pydantic -> evidence guard -> roles   (cache keyed by input hash)
main.to_record   merge LLM fields with authoritative ATS metadata, annualise salary, US + role check
deduplicator.py  match against stored postings (pure, unit-tested)
change_tracker.py  upsert source rows, rebuild canonical record, write change_history, close missing
exporter.py / evaluator.py
```
Tables: `postings` (canonical records), `source_postings` (source-to-posting mapping, with a stable `source_key` such as `greenhouse:stripe:1001` or `hackernews:<thread>:<comment>:<title-slug>`), `evidence`, `change_history`, `dedup_review`, `raw_responses` (content-addressed bodies in `data/raw/`), `fetch_failures`, `extraction_cache`, `llm_calls` and `runs`.

**Idempotency.** `posting_id = sha1(first source_key)` is persisted. A re-seen source key updates `last_seen` and never inserts. The canonical record is rebuilt from its source rows (ATS outranks HN, and HN only fills nulls). A history row is written only when a tracked field actually differs. A source row goes inactive only if its board was fetched **completely** in this run and the posting was missing. A failed fetch, a page that returned HTML instead of JSON, a partial HN thread or an LLM failure never closes anything. A posting closes when all its sources are inactive and reopens, with history, if one comes back.

**Extraction.** Only targeted text goes to the LLM: the posting's head plus lines mentioning pay, location, visa, work type or stack, capped at 6k characters. Whole HTML pages are never sent. The prompt demands null over guesses and a verbatim snippet for every field. After Pydantic validation (with one repair retry on invalid output), an **evidence guard** nulls any field whose snippet isn't found in the input text. It also nulls a salary whose numbers aren't inside its snippet, and any skill that doesn't appear in the text. Salaries are annualised in code from the stated period. A "$150k" with no period counts as annual, and a bare "$90" is dropped. Company and title come from the board for ATS sources. HN comments with several roles produce several records.

**Dedup.** Candidates are blocked by normalised company. An HN comment that links to an ATS URL or job id is a definitive match. Two different job ids on the same board are never merged, because the same title in two cities means two requisitions. Otherwise the score is 0.55·title + 0.15·seniority + 0.15·location + 0.15·skill overlap, capped for conflicting seniority (Senior vs Staff) or cities. Scores ≥ 0.85 merge. Scores from 0.65 to 0.85 stay separate and go to `dedup_review` for a human decision. Title matching compares role families after expanding abbreviations ("Sr. Data Eng" equals "Senior Data Engineer, Platform"), so differing team qualifiers ("…, Ads" vs "…, Payments") don't match.

## Results (fill in after the live run, from `data/evaluation/evaluation_report.md`)

| | Value |
|---|---|
| Postings (open / total), merged source records | _TBD_ |
| Field accuracy: company / title / seniority / location / remote / salary / employment / skills F1 / visa / posted date | _TBD_ |
| Spurious values (hallucinations) | _TBD_ |
| LLM model, cost per posting, total cost | _TBD_ |

## Trade-offs

- **SQLite** gives one file and zero setup, with constraints and indexes. It's single-writer, which is fine for one process.
- **Precision over recall.** The evidence guard throws away correct answers that the model paraphrased, and the strict US check drops HN posts that only say "Remote". Both choices are deliberate, and `run_summary.json` counts rejects by reason.
- **No LLM batching.** Calls are sequential and cached. The Batch API, at half price, is the first change at scale (see below).
- **`posted_date`** comes from source metadata (Greenhouse `first_published`, Lever `createdAt`, HN comment time), not from the text, because postings rarely state it.
- **HN postings never close by themselves.** Comments aren't withdrawn, so closure is only inferred from a fully re-scraped thread.

## What breaks first

1. **HN title drift.** If a comment is edited and the LLM renames the role, the slug in its `source_key` changes. Same-comment matching catches most cases, but some will land in `dedup_review`.
2. **HN politeness.** robots.txt may disallow `/submitted` or set a long Crawl-delay. The client obeys it, and `HN_THREAD_ID` skips discovery.
3. **Board tokens and schema drift.** A renamed or emptied board triggers the fallback substitution, but a silent schema change would only appear as a drop in counts. Next step: alert on counts falling below the previous run.
4. **Pre-filter recall.** Unusual titles such as "Member of Technical Staff, Data" are filtered out before reaching the LLM.

## Scaling to 5,000 sources weekly

- **Volume, as assumptions to verify.** About 50 jobs per board gives 250k postings a week. Around 10% pass the pre-filter, so 25k. The content hash is unchanged for about 80% week to week, so only about 5k need LLM calls.
- **LLM cost.** About 2k input and 300 output tokens per posting on a Haiku-class model ($1/$5 per M tokens) comes to roughly $0.0035 each. That's about **$18 a week**, or **about $9 with the Batch API**. The first backfill is about $45 through batch.
- **Further cost cuts.** Use a structured-output schema, skip extraction for ATS fields that are already structured (Lever salary and workplace type), and send only postings that fail a cheap regex extractor.
- **Fetching.** Postgres replaces SQLite, with the same tables plus partitioning of `raw_responses` by month. A `crawl_queue` table is processed by N worker processes, each holding a per-domain token bucket, so politeness stays per domain while unrelated domains run in parallel. That's still no Airflow or Kafka. Raw bodies move to S3 with content-addressed keys. ATS boards are one request each, so 5k boards at 1 req/s take about 1.5 hours on one domain.
- **Bespoke career sites.** First detect embedded Greenhouse, Lever or Ashby widgets and call their APIs. Use headless Playwright only as a fallback for JavaScript-rendered pages, which this repo doesn't implement because all three sources are static.
- **Monitoring.** Track per-source counts, the failure rate and the dedup-review backlog, and alert on more than 30% drift.

## Notes

- **Board substitution.** In the fixture data, Figma's board was set to return 0 jobs to test the fallback, which substituted `dropbox` (see `run_summary.json` → `notes`). Record here which board, if any, the live run substituted.
- **AI tools.** This repo was built with an AI coding assistant (Claude, by Anthropic) for implementation, tests and documentation, and reviewed and run by me. The extraction LLM is configurable and set in `.env`.
- **Fixtures.** `tests/fixtures/` holds **synthetic** responses in the real API and HTML formats, used by the tests and `make demo`. They aren't real postings.
