# Sample output from the offline demo (SYNTHETIC data)

Produced by `make demo`: three runs over the synthetic fixtures in `tests/fixtures/`
(v1, v1 again, then v2) with `LLM_PROVIDER=mock`. Companies, salaries and postings are invented
to exercise edge cases. **This is not the submission dataset.** It shows the export formats and
that re-runs are idempotent (run 2 added nothing) and that changes are tracked (run 3: salary
change, location change, one closure; Plaid's HTML response and HN's 503 closed nothing).

The real dataset is written to `data/exports/` by `make run`.
