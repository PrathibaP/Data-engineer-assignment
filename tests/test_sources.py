"""Source parsers: Greenhouse JSON + escaped HTML, Lever schema + content-type, HN pagination & comments."""
import json
from datetime import date

from conftest import FIXTURES, ScriptedTransport, html_resp, json_resp

from pipeline.scraper_base import FixtureTransport
from pipeline.sources import greenhouse, hackernews, lever


def _load(name):
    return json.loads((FIXTURES / "live_v1" / name).read_text())


def test_greenhouse_parses_escaped_html_and_filters_roles_and_location():
    res = greenhouse.parse_board("stripe", _load("gh_stripe.json"))
    ids = {p.source_job_id for p in res.postings}
    assert res.total_seen == 5
    assert ids == {"1001", "1002", "1004"}  # Account Executive and Toronto role filtered out
    p = next(p for p in res.postings if p.source_job_id == "1001")
    assert "<p>" not in p.text and "&lt;" not in p.text
    assert "$180,000 - $250,000" in p.text
    assert p.posted_date_hint == date(2026, 10, 20) and p.company_hint == "Stripe"


def test_greenhouse_platform_engineer_needs_data_context():
    res = greenhouse.parse_board("databricks", _load("gh_databricks.json"))
    assert [p.title_hint for p in res.postings] == ["Sr. Platform Engineer - Data Infrastructure"]


def test_greenhouse_malformed_job_does_not_drop_board():
    payload = _load("gh_stripe.json")
    payload["jobs"].insert(0, {"title": "Data Engineer", "location": {"name": "Austin, TX"}})  # no id
    res = greenhouse.parse_board("stripe", payload)
    assert len(res.postings) == 3 and res.errors


def test_greenhouse_unexpected_payload():
    assert not greenhouse.parse_board("x", {"error": "x"}).ok


def test_lever_normalises_second_schema():
    res = lever.parse_site("palantir", _load("lever_palantir.json"))
    assert [p.title_hint for p in res.postings] == ["Data Engineer", "Machine Learning Engineer"]  # London dropped
    p = res.postings[0]
    assert p.structured["salaryRange"]["min"] == 130000 and p.structured["workplaceType"] == "onsite"
    assert "Commitment: Full-time" in p.text


def test_lever_html_response_is_a_recorded_failure(make_client):
    url = lever.URL.format(site="plaid")
    t = ScriptedTransport({url: [html_resp("<html>oops</html>")]})
    res = lever.scrape(make_client(t, min_interval_s=0), "plaid")
    assert not res.ok and not res.complete and "content_type_mismatch" in res.errors[0]


def test_lever_api_error_payload():
    assert not lever.parse_site("x", {"ok": False, "error": "Document not found"}).ok


def test_hn_thread_discovery_picks_current_month():
    html = (FIXTURES / "live_v1" / "hn_submitted.html").read_text()
    tid, title, more = hackernews.find_thread(html, date(2026, 11, 11))
    assert tid == "46000000" and "November 2026" in title and more
    tid2, _, _ = hackernews.find_thread(html, date(2026, 10, 3))
    assert tid2 == "45700000"
    assert hackernews.find_thread(html, date(2027, 1, 2))[0] is None


def test_hn_comment_parsing_skips_replies_and_deleted():
    comments, more = hackernews.parse_item_page((FIXTURES / "live_v1" / "hn_item_p1.html").read_text())
    ids = [c["id"] for c in comments]
    assert "46000106" not in ids  # reply (indent 1)
    assert "46000107" not in ids  # deleted
    assert len(ids) == 7 and more.endswith("item?id=46000000&p=2")
    plaid = next(c for c in comments if c["id"] == "46000102")
    assert any("jobs.lever.co/plaid" in u for u in plaid["links"])
    assert comments[0]["posted"] == date(2026, 11, 2)


def test_hn_scrape_follows_more_pagination_and_prefilters(make_client):
    client = make_client(FixtureTransport(FIXTURES / "live_v1"), min_interval_s=0)
    res = hackernews.scrape(client, date(2026, 11, 11))
    assert res.ok and res.complete and res.total_seen == 12
    ids = {p.source_job_id for p in res.postings}
    assert "46000110" in ids  # from page 2
    assert "46000104" not in ids and "46000114" not in ids  # Berlin / Toronto only
    assert "46000105" not in ids  # no data/ML role


def test_hn_partial_thread_is_marked_incomplete(make_client):
    client = make_client(FixtureTransport(FIXTURES / "live_v2"), min_interval_s=0)
    res = hackernews.scrape(client, date(2026, 11, 11))
    assert res.ok and not res.complete and any("page 2" in e for e in res.errors)
    assert len(res.postings) > 0  # page 1 still used


def test_hn_thread_id_override_skips_discovery(make_client):
    client = make_client(FixtureTransport(FIXTURES / "live_v1"), min_interval_s=0)
    res = hackernews.scrape(client, date(2020, 1, 1), thread_id="46000000")
    assert res.ok and res.postings
    assert not any("submitted" in u for u in client.transport.requests)
