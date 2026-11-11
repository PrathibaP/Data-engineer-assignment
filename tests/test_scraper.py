"""Polite client: robots.txt, rate limiting, retries/backoff, content-type validation, failure logging."""
from conftest import ScriptedTransport, html_resp, json_resp

from pipeline.scraper_base import Response

API = "https://api.example.com/jobs"


def test_robots_disallow_is_respected_and_logged(make_client, conn):
    t = ScriptedTransport({"https://site.test/robots.txt": [Response(200, {}, "User-agent: *\nDisallow: /private")],
                           "https://site.test/private/x": [html_resp("secret")]})
    r = make_client(t).get("https://site.test/private/x")
    assert not r.ok and r.error == "disallowed_by_robots_txt"
    assert "https://site.test/private/x" not in t.calls
    assert conn.execute("SELECT reason FROM fetch_failures").fetchone()[0] == "disallowed_by_robots_txt"


def test_robots_404_allows_and_5xx_disallows(make_client):
    ok = make_client(ScriptedTransport({API: [json_resp({"a": 1})]})).get(API, expect="json")
    assert ok.ok
    t = ScriptedTransport({"https://api.example.com/robots.txt": [Response(503, {}, "")], API: [json_resp({})]})
    assert make_client(t).get(API).error.startswith("robots_txt_unreachable")


def test_transient_robots_failure_is_retried(make_client):
    t = ScriptedTransport({"https://api.example.com/robots.txt": [ConnectionError("blip"), Response(404, {}, "")],
                           API: [json_resp({"ok": 1})]})
    assert make_client(t).get(API).ok


def test_rate_limit_one_request_per_second_per_domain(make_client):
    t = ScriptedTransport({API: [json_resp({})], API + "?p=2": [json_resp({})]})
    clock = iter([0.0, 0.0, 0.2, 0.2, 0.3, 0.3, 5, 5, 5, 5])
    c = make_client(t, clock=lambda: next(clock))
    c.get(API)
    c.get(API + "?p=2")
    assert c.sleeps and all(s <= 1.0 for s in c.sleeps) and max(c.sleeps) > 0.5


def test_crawl_delay_is_honoured(make_client):
    t = ScriptedTransport({"https://slow.test/robots.txt": [Response(200, {}, "User-agent: *\nCrawl-delay: 7")],
                           "https://slow.test/a": [html_resp("x")]})
    c = make_client(t, clock=lambda: 0.0)
    c.get("https://slow.test/a")
    assert c._crawl_delay["slow.test"] == 7.0 and 7.0 in c.sleeps


def test_retries_with_backoff_then_succeeds(make_client):
    t = ScriptedTransport({API: [ConnectionError("reset"), html_resp("", 503), json_resp({"ok": True})]})
    c = make_client(t)
    r = c.get(API, expect="json")
    assert r.ok and r.attempts == 3 and r.json() == {"ok": True}
    assert len([s for s in c.sleeps if s >= 1]) >= 2  # exponential backoff sleeps


def test_retry_after_header_is_used(make_client):
    t = ScriptedTransport({API: [html_resp("", 429, {"retry-after": "9"}), json_resp({})]})
    c = make_client(t)
    assert c.get(API).ok and 9.0 in c.sleeps


def test_404_fails_fast_without_retry(make_client):
    t = ScriptedTransport({API: [html_resp("nope", 404)]})
    r = make_client(t).get(API)
    assert not r.ok and r.attempts == 1 and t.calls.count(API) == 1


def test_content_type_mismatch_fails_after_retries_without_raising(make_client, conn):
    t = ScriptedTransport({API: [html_resp("<html>maintenance</html>")]})
    r = make_client(t).get(API, expect="json")
    assert not r.ok and "content_type_mismatch" in r.error and r.attempts == 3
    assert conn.execute("SELECT COUNT(*) FROM fetch_failures").fetchone()[0] == 1


def test_raw_responses_cached_and_deduplicated(make_client, conn):
    t = ScriptedTransport({API: [json_resp({"x": 1})]})
    c = make_client(t)
    c.get(API)
    c.get(API)
    row = conn.execute("SELECT COUNT(*), MAX(fetch_count) FROM raw_responses").fetchone()
    assert tuple(row) == (1, 2)
    off = make_client(ScriptedTransport({}), offline=True).get(API)
    assert off.ok and off.from_cache and off.json() == {"x": 1}
