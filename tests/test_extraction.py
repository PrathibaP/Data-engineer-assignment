"""Schema validation, null handling, evidence guard, invalid LLM output, caching, salary annualisation."""
import json

import pytest
from pydantic import ValidationError

from pipeline.extract import Extractor, MockProvider, build_snippet, evidence_guard
from pipeline.transform.records import to_record
from pipeline.models import ExtractedRole, RawPosting
from pipeline.transform.normalize import annualize, clean_title, company_key, is_target_title, looks_us, normalize_company

TEXT = ("Acme | Senior Data Engineer | San Francisco, CA | Hybrid | $180k-$220k\n"
        "We use Python, Spark and Airflow.")


class FakeProvider:
    model = "fake-1"

    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), 0

    def complete(self, system, user):
        self.calls += 1
        return self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0], 1000, 200


def raw(text=TEXT, source="hackernews"):
    return RawPosting(source=source, board="t1", source_job_id="c1", url="https://news.ycombinator.com/item?id=1",
                      company_hint="Acme", text=text)


GOOD = {"roles": [{"title": "Senior Data Engineer", "company": "Acme", "seniority": "senior", "city": "San Francisco",
                   "state": "CA", "country": "US", "remote_policy": "hybrid", "salary_min": 180000, "salary_max": 220000,
                   "currency": "USD", "salary_period": None, "employment_type": None, "skills": ["Python", "Spark", "Airflow"],
                   "visa_sponsorship": None,
                   "evidence": {"company": "Acme", "seniority": "Senior Data Engineer", "city": "San Francisco, CA",
                                "state": "San Francisco, CA", "country": "San Francisco, CA", "remote_policy": "Hybrid",
                                "salary_min": "$180k-$220k", "salary_max": "$180k-$220k", "currency": "$180k-$220k"}}]}


def test_schema_rejects_bad_enum():
    with pytest.raises(ValidationError):
        ExtractedRole(title="x", seniority="ninja")
    with pytest.raises(ValidationError):
        ExtractedRole(title="x", remote_policy="sometimes")


def test_missing_fields_default_to_null():
    r = ExtractedRole(title="Data Engineer")
    assert r.salary_min is None and r.visa_sponsorship is None and r.skills == [] and r.city is None


def test_currency_and_salary_order_normalised():
    r = ExtractedRole(title="x", currency="usd", salary_min=200, salary_max=100)
    assert r.currency == "USD" and (r.salary_min, r.salary_max) == (100, 200)
    assert ExtractedRole(title="x", currency="dollars").currency is None


def test_valid_output_extracted(conn):
    ex = Extractor(FakeProvider([json.dumps(GOOD)]), conn)
    [(role, dropped)] = ex.extract(raw())
    assert role.salary_min == 180000 and role.remote_policy.value == "hybrid" and dropped == []


def test_hallucinated_fields_are_nulled_by_evidence_guard():
    role = ExtractedRole(title="Senior Data Engineer", salary_min=250000, currency="USD", visa_sponsorship=True,
                         city="New York", skills=["Python", "Kubernetes"],
                         evidence={"salary_min": "$250k", "currency": "$250k", "visa_sponsorship": "we sponsor visas",
                                   "city": "New York"})
    role, dropped = evidence_guard(role, TEXT)
    assert role.salary_min is None and role.visa_sponsorship is None and role.city is None
    assert role.skills == ["Python"]  # Kubernetes not in text
    assert {"salary_min", "visa_sponsorship", "city"} <= set(dropped)


def test_salary_number_must_appear_in_its_snippet():
    role = ExtractedRole(title="x", salary_min=190000, currency="USD",
                         evidence={"salary_min": "$180k-$220k", "currency": "$180k-$220k"})
    role, dropped = evidence_guard(role, TEXT)
    assert role.salary_min is None and "salary_min" in dropped


def test_field_without_evidence_is_nulled():
    role, dropped = evidence_guard(ExtractedRole(title="x", remote_policy="remote"), TEXT)
    assert role.remote_policy is None and dropped == ["remote_policy"]


def test_invalid_llm_output_is_retried_then_succeeds(conn):
    p = FakeProvider(["Sure! here you go: {not json", "```json\n" + json.dumps(GOOD) + "\n```"])
    ex = Extractor(p, conn)
    out = ex.extract(raw())
    assert p.calls == 2 and len(out) == 1
    assert conn.execute("SELECT COUNT(*) FROM llm_calls WHERE ok=0").fetchone()[0] == 1


def test_persistently_invalid_output_fails_gracefully(conn):
    ex = Extractor(FakeProvider(['{"roles": [{"title": "x", "seniority": "wizard"}]}']), conn)
    assert ex.extract(raw()) == [] and ex.stats["failures"] == 1


def test_extraction_cache_prevents_repeat_llm_calls_and_tracks_cost(conn):
    p = FakeProvider([json.dumps(GOOD)])
    ex = Extractor(p, conn, price_in=1.0, price_out=5.0)
    ex.extract(raw())
    ex.extract(raw())
    assert p.calls == 1 and ex.stats["cache_hits"] == 1
    cost = conn.execute("SELECT SUM(cost_usd) FROM llm_calls").fetchone()[0]
    assert cost == pytest.approx(1000 / 1e6 * 1 + 200 / 1e6 * 5)


def test_budget_stops_llm_spend(conn):
    from pipeline.extract import BudgetExceeded

    ex = Extractor(FakeProvider([json.dumps(GOOD)]), conn, budget_usd=0.0)
    with pytest.raises(BudgetExceeded):
        ex.extract(raw())


def test_snippet_keeps_salary_line_and_trims_boilerplate():
    long = "Title: Data Engineer\n" + "\n".join(f"About our culture paragraph {i}." for i in range(400)) + \
           "\nThe base salary range is $150,000 - $190,000 per year.\n" + "Benefits blah.\n" * 200
    snip = build_snippet(raw(long, "greenhouse"), 3000)
    assert len(snip) <= 3100 and "$150,000 - $190,000" in snip


def test_annualize_only_with_defensible_period():
    assert annualize(50, "hour") == 104000
    assert annualize(10000, "month") == 120000
    assert annualize(180000, None) == 180000  # "$180k" convention
    assert annualize(90, None) is None  # ambiguous "$90" -> null, never guessed


def test_mock_provider_multi_role_comment():
    text = ("Acme Analytics | Multiple roles | REMOTE (US) | Full-time\nWe're hiring:\n"
            "- Senior Data Engineer ($170k-$200k): Python\n- Machine Learning Engineer ($160k-$190k): PyTorch\n"
            "- Frontend Engineer: React")
    out, _, _ = MockProvider().complete("", "<<<TEXT\n" + text + "\nTEXT>>>")
    roles = json.loads(out)["roles"]
    assert [r["title"] for r in roles] == ["Senior Data Engineer", "Machine Learning Engineer"]
    assert roles[1]["salary_min"] == 160000


def test_to_record_requires_us_and_target_role():
    r = raw()
    rec, why = to_record(r, ExtractedRole(title="Senior Data Engineer", country="Canada"), "h")
    assert rec is None and why == "not_us"
    rec, why = to_record(r, ExtractedRole(title="Frontend Engineer", country="US"), "h")
    assert rec is None and why == "not_target_role"
    rec, _ = to_record(r, ExtractedRole(title="Sr. Data Engineer", state="CA"), "h")
    assert rec["country"] == "US" and rec["title"] == "Senior Data Engineer" and rec["seniority"] == "senior"


def test_salary_without_currency_is_dropped():
    rec, _ = to_record(raw(), ExtractedRole(title="Data Engineer", country="US", salary_min=150000), "h")
    assert rec["salary_min"] is None


def test_normalisation_helpers():
    assert normalize_company("Stripe, Inc.") == "Stripe" and company_key("Palantir Technologies") == "palantir"
    assert clean_title("Sr. ML Eng") == "Senior Machine Learning Engineer"
    assert is_target_title("Analytics Engineer") and not is_target_title("Engineering Manager, Data Platform")
    assert not is_target_title("Data Scientist") and not is_target_title("Platform Engineer", "React CI tooling")
    assert looks_us("Remote (US)") and looks_us("Berlin, Germany") is False and looks_us("Remote") is None
    assert looks_us("Join us in Berlin") is False
