"""Change history, closure and re-opening at the Store level."""
from pipeline.change_tracker import Store, make_posting_id


def rec(**kw):
    base = {"source_key": "greenhouse:stripe:1001", "source": "greenhouse", "board": "stripe", "source_job_id": "1001",
            "url": "https://boards.greenhouse.io/stripe/jobs/1001", "outbound_urls": [], "content_hash": "h1",
            "company": "Stripe", "company_key": "stripe", "title": "Senior Data Engineer", "title_key": "senior data engineer",
            "seniority": "senior", "city": "San Francisco", "country": "US", "remote_policy": "onsite",
            "salary_min": 180000.0, "salary_max": 250000.0, "currency": "USD", "employment_type": "full_time",
            "skills": ["Python"], "visa_sponsorship": None, "posted_date": "2026-10-20",
            "evidence": {"salary_min": "$180,000 - $250,000"}}
    base.update(kw)
    return base


def history(conn):
    return [(r["field"], r["old_value"], r["new_value"]) for r in
            conn.execute("SELECT field, old_value, new_value FROM change_history ORDER BY id")]


def test_new_posting_writes_no_history_and_stable_id(conn):
    pid, action = Store(conn, "r1", "2026-11-01T00:00:00+00:00").upsert(rec())
    assert action == "new" and pid == make_posting_id("greenhouse:stripe:1001") and history(conn) == []


def test_unchanged_rerun_writes_no_history(conn):
    Store(conn, "r1", "2026-11-01T00:00:00+00:00").upsert(rec())
    pid, action = Store(conn, "r2", "2026-11-02T00:00:00+00:00").upsert(rec())
    assert action == "seen" and history(conn) == []
    row = conn.execute("SELECT first_seen, last_seen FROM postings").fetchone()
    assert row["first_seen"].startswith("2026-11-01") and row["last_seen"].startswith("2026-11-02")


def test_salary_and_location_change_recorded(conn):
    Store(conn, "r1", "2026-11-01T00:00:00+00:00").upsert(rec())
    Store(conn, "r2", "2026-11-02T00:00:00+00:00").upsert(rec(salary_min=190000.0, salary_max=260000.0,
                                                               city="New York", content_hash="h2"))
    assert set(history(conn)) == {("salary_min", "180000.0", "190000.0"), ("salary_max", "250000.0", "260000.0"),
                                  ("city", "San Francisco", "New York")}
    assert conn.execute("SELECT city, salary_min FROM postings").fetchone()[:] == ("New York", 190000.0)


def test_closed_when_missing_from_complete_board_and_reopened(conn):
    Store(conn, "r1", "2026-11-01T00:00:00+00:00").upsert(rec())
    s2 = Store(conn, "r2", "2026-11-02T00:00:00+00:00")
    assert s2.close_missing("greenhouse", "stripe") == 1
    assert conn.execute("SELECT status FROM postings").fetchone()[0] == "closed"
    Store(conn, "r3", "2026-11-03T00:00:00+00:00").upsert(rec())
    assert history(conn) == [("status", "open", "closed"), ("status", "closed", "open")]


def test_close_missing_ignores_rows_seen_this_run_and_other_boards(conn):
    s = Store(conn, "r1", "2026-11-01T00:00:00+00:00")
    s.upsert(rec())
    s.upsert(rec(source_key="greenhouse:airbnb:1", board="airbnb", source_job_id="1", company="Airbnb", company_key="airbnb"))
    assert s.close_missing("greenhouse", "stripe") == 0
    assert Store(conn, "r2", "2026-11-02T00:00:00+00:00").close_missing("greenhouse", "stripe") == 1
    assert conn.execute("SELECT COUNT(*) FROM postings WHERE status='open'").fetchone()[0] == 1


def test_merged_posting_stays_open_while_any_source_active(conn):
    s = Store(conn, "r1", "2026-11-01T00:00:00+00:00")
    s.upsert(rec())
    pid, action = s.upsert(rec(source_key="hackernews:t:9:senior-data-engineer", source="hackernews", board="t",
                               source_job_id="9", url="https://news.ycombinator.com/item?id=9", salary_min=None,
                               salary_max=None, currency=None))
    assert action == "merged"
    Store(conn, "r2", "2026-11-02T00:00:00+00:00").close_missing("greenhouse", "stripe")
    assert conn.execute("SELECT status FROM postings WHERE posting_id=?", (pid,)).fetchone()[0] == "open"


def test_ats_values_outrank_hn_but_hn_fills_gaps(conn):
    s = Store(conn, "r1", "2026-11-01T00:00:00+00:00")
    s.upsert(rec(salary_min=None, salary_max=None, currency=None, visa_sponsorship=None))
    s.upsert(rec(source_key="hackernews:t:9:x", source="hackernews", board="t", source_job_id="9",
                 url="https://news.ycombinator.com/item?id=9", title="Sr. Data Engineer", salary_min=170000.0,
                 salary_max=200000.0, visa_sponsorship=True, evidence={"salary_min": "$170k-$200k"}))
    row = conn.execute("SELECT title, salary_min, visa_sponsorship FROM postings").fetchone()
    assert row["title"] == "Senior Data Engineer" and row["salary_min"] == 170000.0 and row["visa_sponsorship"] == 1
    ev = conn.execute("SELECT snippet, source_key FROM evidence WHERE field='salary_min'").fetchone()
    assert ev["source_key"].startswith("hackernews")
