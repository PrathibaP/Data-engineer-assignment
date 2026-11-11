"""Evaluator scoring logic, and the real provider adapters' request/response handling (HTTP mocked)."""
import csv
import json
from datetime import date

import pytest
from conftest import FIXTURES

from pipeline.report import evaluate as evaluator
from pipeline.store.db import connect
from pipeline.extract import AnthropicProvider, OpenAIProvider
from pipeline.orchestrator import run


def test_compare_outcomes():
    c = evaluator._compare
    assert c("city", None, None) == "correct_null"
    assert c("salary_min", 180000.0, None) == "spurious"  # hallucination
    assert c("salary_min", None, 180000.0) == "missing"
    assert c("salary_min", 180500.0, 180000.0) == "correct"  # within 1%
    assert c("remote_policy", "remote", "hybrid") == "incorrect"
    assert c("skills", {"python", "spark"}, {"python", "spark", "dbt"}) == "correct"  # F1 0.8
    assert c("skills", {"python"}, {"python", "spark", "dbt"}) == "incorrect"


def test_label_template_and_evaluate_end_to_end(settings):
    settings.fixtures_dir = FIXTURES / "live_v1"
    run(settings, today=date(2026, 11, 11), now="2026-11-11T10:00:00+00:00")
    conn = connect(settings.db_path)
    tpl = evaluator.write_label_template(conn, settings.eval_dir, n=12, min_hn=6)
    rows = list(csv.DictReader(tpl.open()))
    assert len(rows) == 12 and sum(r["source"] == "hackernews" for r in rows) >= 6
    # simulate a labeller: truth = prediction, except one deliberate disagreement and one '?'
    for r in rows:
        for f in evaluator.FIELDS:
            r[f"truth_{f}"] = r[f"pred_{f}"]
    rows[0]["truth_city"] = "Nowhere"
    rows[1]["truth_posted_date"] = "?"
    with (settings.eval_dir / "labels.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    rep = evaluator.evaluate(conn, settings.eval_dir)
    s = rep["summary"]
    assert s["n_labelled"] == 12 and "MOCK" in s["warning"]
    assert rep["fields"]["posted_date"]["not_evaluable"] == 1
    city = rep["fields"]["city"]
    assert city.get("incorrect", 0) + city.get("missing", 0) == 1
    assert (settings.eval_dir / "evaluation_report.md").exists()


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_anthropic_adapter(monkeypatch):
    seen = {}

    def fake_post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, body=json)
        return _Resp({"content": [{"type": "text", "text": '{"roles": []}'}],
                      "usage": {"input_tokens": 1200, "output_tokens": 80}})

    monkeypatch.setattr("requests.post", fake_post)
    text, tin, tout = AnthropicProvider("sk-test", "claude-haiku-4-5").complete("sys", "user")
    assert json.loads(text) == {"roles": []} and (tin, tout) == (1200, 80)
    assert seen["headers"]["x-api-key"] == "sk-test" and seen["body"]["temperature"] == 0
    assert seen["body"]["system"] == "sys"


def test_openai_adapter(monkeypatch):
    monkeypatch.setattr("requests.post", lambda url, headers, json, timeout: _Resp(
        {"choices": [{"message": {"content": '{"roles": []}'}}], "usage": {"prompt_tokens": 900, "completion_tokens": 50}}))
    assert OpenAIProvider("k", "gpt-4o-mini").complete("s", "u")[1:] == (900, 50)


def test_missing_api_key_is_a_clear_error(settings):
    from pipeline.extract import make_provider

    settings.llm_provider, settings.llm_api_key = "anthropic", None
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        make_provider(settings)
