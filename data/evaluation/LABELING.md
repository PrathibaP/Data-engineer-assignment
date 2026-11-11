# Hand-labelling protocol (20 postings, >= 10 from Hacker News)

1. After a live run: `python -m pipeline label-template` -> `labels_template.csv`.
2. Copy to `labels.csv`. Hide or ignore the `pred_*` columns while labelling (blind labels).
3. For each row open `url` and fill `truth_*` **from the posting text only**:
   - empty cell = the posting does not state it (correct answer is null)
   - `?` = you cannot judge it -> excluded and reported as "not evaluable"
   - salary: annual numbers, e.g. `180000`; only if the posting states a salary for this role
   - skills: `;`-separated list of technologies named in the posting
   - seniority: junior | mid | senior | staff_plus (only if the title/text says so)
   - remote_policy: onsite | hybrid | remote; employment_type: full_time | contract | intern
   - visa_sponsorship: true | false | empty
   - posted_date: YYYY-MM-DD (we use source metadata: Greenhouse first_published, Lever createdAt,
     HN comment timestamp)
4. `python -m pipeline evaluate` -> `evaluation_report.md`, `evaluation_report.json`,
   `evaluation_results.csv` (one row per posting x field with the outcome).

Outcome meanings: correct, correct_null (both null), incorrect, missing (stated but we returned null),
spurious (we returned a value the posting does not state = hallucination), not_evaluable.
