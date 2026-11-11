"""CSV / JSON exports of the database (data/exports)."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

JOB_COLUMNS = ["posting_id", "company", "title", "seniority", "city", "country", "remote_policy", "salary_min",
               "salary_max", "currency", "employment_type", "skills", "visa_sponsorship", "posted_date", "status",
               "source_urls", "evidence", "first_seen", "last_seen"]


def canonical_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = []
    for p in conn.execute("SELECT * FROM postings ORDER BY company, title, posting_id"):
        srcs = conn.execute("SELECT url, source, match_method, match_score, active FROM source_postings "
                            "WHERE posting_id=? ORDER BY first_seen, source_key", (p["posting_id"],)).fetchall()
        ev = {r["field"]: r["snippet"] for r in
              conn.execute("SELECT field, snippet FROM evidence WHERE posting_id=? ORDER BY field", (p["posting_id"],))}
        rows.append({
            **{k: p[k] for k in p.keys() if k in JOB_COLUMNS},
            "skills": json.loads(p["skills_json"]),
            "visa_sponsorship": None if p["visa_sponsorship"] is None else bool(p["visa_sponsorship"]),
            "source_urls": [s["url"] for s in srcs],
            "evidence": ev,
            "_sources": [dict(s) for s in srcs],
        })
    return rows


def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in r.items()})


def _dump_table(conn, path: Path, sql: str) -> None:
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    _write_csv(path, [dict(zip(cols, r)) for r in cur.fetchall()], cols)


def export_all(conn: sqlite3.Connection, out: Path, run_stats: dict | None) -> None:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rows = canonical_rows(conn)
    # 1. final dataset, target schema exactly
    _write_csv(out / "jobs.csv", [{**r, "source_urls": " | ".join(r["source_urls"])} for r in rows], JOB_COLUMNS)
    # 2. deduplicated canonical records with merge metadata
    canon = [{**r, "n_sources": len(r["_sources"]),
              "match_methods": ",".join(sorted({s["match_method"] for s in r["_sources"]})),
              "min_match_score": min((s["match_score"] for s in r["_sources"] if s["match_score"] is not None), default=None)}
             for r in rows]
    _write_csv(out / "canonical_records.csv", canon, JOB_COLUMNS + ["n_sources", "match_methods", "min_match_score"])
    # 3. raw (per-source) records and the raw HTTP cache index
    _dump_table(conn, out / "raw_source_records.csv",
                "SELECT source_key, posting_id, source, board, source_job_id, url, title_raw, location_raw, "
                "content_hash, match_method, match_score, active, first_seen, last_seen, extracted_json "
                "FROM source_postings ORDER BY source, board, source_job_id")
    _dump_table(conn, out / "raw_responses_index.csv",
                "SELECT url, status_code, content_type, content_hash, body_path, first_fetched, last_fetched, "
                "fetch_count FROM raw_responses ORDER BY url, last_fetched")
    # 4. change history, dedup review queue, failures
    _dump_table(conn, out / "change_history.csv", "SELECT * FROM change_history ORDER BY changed_at, id")
    _dump_table(conn, out / "dedup_review.csv", "SELECT * FROM dedup_review ORDER BY score DESC")
    _dump_table(conn, out / "fetch_failures.csv", "SELECT * FROM fetch_failures ORDER BY failed_at, id")
    with (out / "jobs.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps({k: r[k] for k in JOB_COLUMNS}, default=str) + "\n")
    # 6. run summary
    if run_stats is not None:
        (out / "run_summary.json").write_text(json.dumps(run_stats, indent=2, default=str))
    runs = [dict(r) for r in conn.execute("SELECT run_id, started_at, finished_at, status FROM runs ORDER BY started_at")]
    (out / "runs.json").write_text(json.dumps(runs, indent=2))
