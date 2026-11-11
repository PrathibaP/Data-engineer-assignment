"""End-to-end: full pipeline over fixture snapshots; idempotency, stable IDs, partial-failure safety."""
from datetime import date

from conftest import FIXTURES

from pipeline.database import connect
from pipeline.main import run


def _run(settings, snap, day, now):
    settings.fixtures_dir = FIXTURES / snap
    return run(settings, today=date.fromisoformat(day), now=now)


def snapshot(db):
    c = connect(db)
    s = {
        "postings": {r[0]: tuple(r[1:]) for r in c.execute(
            "SELECT posting_id, title, city, salary_min, salary_max, status FROM postings")},
        "sources": c.execute("SELECT COUNT(*) FROM source_postings").fetchone()[0],
        "history": c.execute("SELECT COUNT(*) FROM change_history").fetchone()[0],
        "llm_calls": c.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0],
    }
    c.close()
    return s


def test_first_run_dedups_and_substitutes_empty_board(settings):
    st = _run(settings, "live_v1", "2026-11-11", "2026-11-11T10:00:00+00:00")
    assert any("figma" in n and "dropbox" in n for n in st["notes"])
    assert st["merged_sources"] == 4 and st["postings_total"] == 17
    c = connect(settings.db_path)
    plaid = c.execute("SELECT GROUP_CONCAT(match_method) FROM source_postings sp JOIN postings p USING(posting_id) "
                      "WHERE p.company='Plaid' AND p.title='Senior Data Engineer'").fetchone()[0]
    assert "url_link" in plaid
    stripe_sea = c.execute("SELECT COUNT(*) FROM postings WHERE company='Stripe' AND city='Seattle'").fetchone()[0]
    assert stripe_sea == 2  # Senior (GH) and Staff (HN) kept separate


def test_second_identical_run_is_idempotent(settings):
    _run(settings, "live_v1", "2026-11-11", "2026-11-11T10:00:00+00:00")
    a = snapshot(settings.db_path)
    st = _run(settings, "live_v1", "2026-11-11", "2026-11-11T11:00:00+00:00")
    b = snapshot(settings.db_path)
    assert a["postings"] == b["postings"]  # same stable IDs, same values
    assert a["sources"] == b["sources"] and b["history"] == 0
    assert b["llm_calls"] == a["llm_calls"] and st["cache_hits"] == st["llm_calls"] + st["cache_hits"]


def test_changed_snapshot_updates_records_and_history_without_false_closures(settings):
    _run(settings, "live_v1", "2026-11-11", "2026-11-11T10:00:00+00:00")
    st = _run(settings, "live_v2", "2026-11-12", "2026-11-12T10:00:00+00:00")
    c = connect(settings.db_path)
    hist = {(r[0], r[1], r[2], r[3]) for r in c.execute(
        "SELECT p.title, h.field, h.old_value, h.new_value FROM change_history h JOIN postings p USING(posting_id)")}
    assert ("Senior Data Engineer, Platform", "salary_min", "180000.0", "190000.0") in hist
    assert ("Analytics Engineer", "city", "Austin", "New York") in hist
    assert ("Machine Learning Engineer, Fraud", "status", "open", "closed") in hist
    assert len(hist) == 4 and st["new_postings"] == 0
    # Plaid answered HTML and HN page 2 failed: nothing from them may be closed
    assert c.execute("SELECT COUNT(*) FROM postings WHERE company='Plaid' AND status='open'").fetchone()[0] == 2
    assert c.execute("SELECT status FROM postings WHERE company='Globex'").fetchone()[0] == "open"
    failed = {r[0] for r in c.execute("SELECT url FROM fetch_failures")}
    assert any("plaid" in u for u in failed) and any("p=2" in u for u in failed)
    assert st["llm_calls"] == 2  # only the two postings whose text changed were re-extracted


def test_exports_written(settings):
    _run(settings, "live_v1", "2026-11-11", "2026-11-11T10:00:00+00:00")
    for f in ["jobs.csv", "canonical_records.csv", "raw_source_records.csv", "change_history.csv",
              "run_summary.json", "dedup_review.csv", "fetch_failures.csv"]:
        assert (settings.export_dir / f).exists(), f
    header = (settings.export_dir / "jobs.csv").read_text().splitlines()[0]
    assert header.startswith("posting_id,company,title,seniority,city,country,remote_policy,salary_min")


def test_offline_replay_uses_cache_only(settings):
    _run(settings, "live_v1", "2026-11-11", "2026-11-11T10:00:00+00:00")
    settings.fixtures_dir = FIXTURES / "does-not-exist"  # would crash if touched
    settings.fixtures_dir = None
    st = run(settings, today=date(2026, 11, 11), offline=True, now="2026-11-11T12:00:00+00:00")
    assert st["new_postings"] == 0 and st["changes"] == 0 and st["llm_calls"] == 0
