"""LLM extraction: targeted snippets in, validated + evidence-checked roles out.

Anti-hallucination layers
1. Prompt: "null unless explicitly stated", quote evidence verbatim.
2. Pydantic validation of types/enums (one repair retry with the validation error, then give up).
3. Evidence guard: every non-null field must have an evidence snippet that occurs in the input text;
   salary numbers must appear inside their snippet; each skill must occur in the text. Otherwise -> null.
4. Salary annualisation is done in code from the stated period, never by the LLM.

Cost controls: deterministic pre-filter upstream, snippet trimming, extraction cache keyed by
(prompt version, model, snippet hash), per-call token/cost logging, hard per-run budget.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError

from .logger import get_logger, log
from .models import ExtractedRole, ExtractionResult, RawPosting
from .normalizer import squash
from .scraper_base import utcnow

lg = get_logger("extractor")
PROMPT_VERSION = "v3"

SYSTEM_PROMPT = """You extract structured job-posting data. Return ONLY a JSON object:
{"roles": [ {"title": str, "company": str|null, "seniority": "junior"|"mid"|"senior"|"staff_plus"|null,
  "city": str|null, "state": str|null, "country": str|null, "remote_policy": "onsite"|"hybrid"|"remote"|null,
  "salary_min": number|null, "salary_max": number|null, "currency": "USD"|...|null,
  "salary_period": "year"|"month"|"week"|"day"|"hour"|null,
  "employment_type": "full_time"|"contract"|"intern"|null, "skills": [str],
  "visa_sponsorship": true|false|null, "evidence": {field_name: verbatim_snippet} } ]}

Rules:
- One entry per distinct role. A Hacker News comment may list several roles; list each one that is a data,
  machine-learning, analytics or data/ML-platform engineering role. Ignore other roles (frontend, sales, ...).
- A field that is not explicitly stated MUST be null. Never guess. Do not infer visa sponsorship, salary,
  location or employment type from company reputation or typical practice.
- seniority: from explicit words only (Junior/Associate/Entry/I -> junior; II/Mid -> mid; Senior/Sr/III/Lead ->
  senior; Staff/Principal/Distinguished -> staff_plus). Otherwise null.
- salary: copy the numbers as written (expand "k" to thousands) and the stated period. If no period is
  written, salary_period is null. Only salary for THIS role. Equity/bonus are not salary.
- country: ISO-like name or "US" only when stated or when a US city/state is stated.
- remote_policy: "remote" only if the posting says remote is allowed for the role; "hybrid" if it says
  hybrid / N days in office; "onsite" if it says onsite / in-office only.
- skills: concrete technologies named in the text (e.g. Python, Spark, dbt, Kafka, Airflow, AWS). Max 15.
- evidence: for EVERY non-null field (and skills), a short snippet (<= 160 chars) copied VERBATIM from the text.
"""


class LLMProvider(Protocol):
    model: str

    def complete(self, system: str, user: str) -> tuple[str, int, int]:
        """Return (text, input_tokens, output_tokens)."""


class AnthropicProvider:
    def __init__(self, api_key: str, model: str, timeout: float = 60):
        self.api_key, self.model, self.timeout = api_key, model, timeout

    def complete(self, system: str, user: str) -> tuple[str, int, int]:
        import requests

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": self.model, "max_tokens": 1500, "temperature": 0, "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=self.timeout,
        )
        r.raise_for_status()
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        return text, d["usage"]["input_tokens"], d["usage"]["output_tokens"]


class OpenAIProvider:
    def __init__(self, api_key: str, model: str, timeout: float = 60):
        self.api_key, self.model, self.timeout = api_key, model, timeout

    def complete(self, system: str, user: str) -> tuple[str, int, int]:
        import requests

        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            timeout=self.timeout,
        )
        r.raise_for_status()
        d = r.json()
        return d["choices"][0]["message"]["content"], d["usage"]["prompt_tokens"], d["usage"]["completion_tokens"]


class MockProvider:
    """Offline, deterministic, rule-based stand-in so the pipeline runs end-to-end without an API key.
    It is NOT an LLM and its accuracy is not representative. Cost is reported as $0."""

    model = "mock-heuristic-v1"
    SKILLS = ["Python", "SQL", "Spark", "PySpark", "Scala", "Java", "Go", "Kafka", "Flink", "Airflow", "dbt",
              "Snowflake", "BigQuery", "Redshift", "Databricks", "AWS", "GCP", "Azure", "Kubernetes", "Docker",
              "Terraform", "PyTorch", "TensorFlow", "Ray", "Postgres", "Trino", "Iceberg", "Delta Lake", "Rust"]

    def complete(self, system: str, user: str) -> tuple[str, int, int]:
        from .normalizer import is_target_title, seniority_from_title

        text = user.split("<<<TEXT\n", 1)[-1].rsplit("\nTEXT>>>", 1)[0]
        lines = text.splitlines()
        titles: list[tuple[str, str]] = []
        m = re.search(r"^Title: (.+)$", text, re.M)
        if m:
            titles.append((m.group(1).strip(), m.group(0)))
        else:  # HN: role names in the header line or in bullet lines
            pat = re.compile(r"((?:Senior |Sr\.? |Staff |Principal |Lead |Junior )?(?:Data|Machine Learning|ML|"
                             r"Analytics|Data Platform|ML Platform|AI|Platform) (?:Platform |Infrastructure )?Engineer\w*"
                             r"(?:,? (?:Data|ML) (?:Infrastructure|Platform))?)", re.I)
            for ln in lines[:12]:
                for mm in pat.finditer(ln):
                    t = mm.group(1).strip()
                    if t.lower() not in {x[0].lower() for x in titles}:
                        titles.append((t, t))
        head = lines[0] if lines else ""
        roles = []
        for title, tev in titles:
            # salary/scope: for multi-role comments prefer the line that names this role
            own = next((ln for ln in lines if title in ln and "$" in ln), None)
            scope = own if (own and len(titles) > 1) else (text if len(titles) == 1 else head)
            if not is_target_title(title, text):
                continue
            ev = {"title": tev}
            role = {"title": title, "evidence": ev, "skills": []}
            if "|" in head and not m:
                role["company"] = head.split("|")[0].strip()
                ev["company"] = role["company"]
            s = seniority_from_title(title)
            if s:
                role["seniority"], ev["seniority"] = s, tev
            loc = re.search(r"(?:Location: )?\b(San Francisco|New York|Seattle|Austin|Boston|Chicago|Denver|"
                            r"Los Angeles|Palo Alto|Mountain View|Washington)\b(?:, ?([A-Z]{2}))?", text)
            if loc:
                role["city"], ev["city"] = loc.group(1), loc.group(0)
                role["country"], ev["country"] = "US", loc.group(0)
            elif re.search(r"\b(US|USA|United States)\b", text):
                mm = re.search(r"[^\n]{0,30}\b(US|USA|United States)\b[^\n]{0,10}", text)
                role["country"], ev["country"] = "US", mm.group(0)
            rm = re.search(r"[^\n.]{0,40}\b(remote|hybrid|onsite|on-site|in-office)\b[^\n.]{0,30}", text, re.I)
            if rm:
                w = rm.group(1).lower()
                role["remote_policy"] = {"on-site": "onsite", "in-office": "onsite"}.get(w, w)
                ev["remote_policy"] = rm.group(0).strip()
            sal = re.search(r"\$\s?(\d{2,3})(,\d{3}|[kK])\s*(?:-|–|to)\s*\$?\s?(\d{2,3})(,\d{3}|[kK])"
                            r"(?:[^\n$]{0,25}?(per year|annually|a year|/yr|/year|per hour|/hr|hourly))?", scope)
            if sal:
                def amt(n, suf):
                    return int(n + suf[1:]) if suf.startswith(",") else int(n) * 1000
                lo, hi = amt(sal.group(1), sal.group(2)), amt(sal.group(3), sal.group(4))
                per = sal.group(5)
                role.update(salary_min=lo, salary_max=hi, currency="USD",
                            salary_period=("hour" if per and "h" in per else "year" if per else None))
                ev.update(salary_min=sal.group(0), salary_max=sal.group(0), currency=sal.group(0))
            if re.search(r"\b(full[- ]time)\b", text, re.I):
                mm = re.search(r"[^\n]{0,20}\bfull[- ]time\b[^\n]{0,10}", text, re.I)
                role["employment_type"], ev["employment_type"] = "full_time", mm.group(0)
            elif re.search(r"\bcontract(or)?\b", text, re.I):
                mm = re.search(r"[^\n]{0,20}\bcontract(or)?\b[^\n]{0,10}", text, re.I)
                role["employment_type"], ev["employment_type"] = "contract", mm.group(0)
            vm = re.search(r"[^\n.]{0,30}\b(visa sponsorship|sponsor visas?|sponsorship)\b[^\n.]{0,30}", text, re.I)
            if vm:
                neg = re.search(r"\b(no|not|unable|cannot|can't|won't|without)\b", vm.group(0), re.I)
                role["visa_sponsorship"], ev["visa_sponsorship"] = (not neg), vm.group(0).strip()
            for sk in self.SKILLS:
                if re.search(rf"(?<![\w]){re.escape(sk)}(?![\w])", text):
                    role["skills"].append(sk)
            if role["skills"]:
                ev["skills"] = ", ".join(role["skills"][:5])
            roles.append(role)
        out = json.dumps({"roles": roles})
        return out, len(user) // 4, len(out) // 4


def make_provider(settings) -> LLMProvider:
    if settings.llm_provider == "anthropic":
        if not settings.llm_api_key:
            raise RuntimeError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
        return AnthropicProvider(settings.llm_api_key, settings.llm_model)
    if settings.llm_provider == "openai":
        if not settings.llm_api_key:
            raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set")
        return OpenAIProvider(settings.llm_api_key, settings.llm_model)
    return MockProvider()


# ------------------------------------------------------------------ snippets

KEY_LINE = re.compile(
    r"(\$|€|£|salary|compensation|pay range|base pay|per year|annual|per hour|/hr|visa|sponsor|remote|hybrid|"
    r"on-?site|in[- ]office|office|location|full[- ]time|part[- ]time|contract|intern|python|sql|spark|kafka|"
    r"airflow|dbt|experience with|requirements|qualifications|you have|you'll|tech stack|stack)", re.I)


def build_snippet(raw: RawPosting, max_chars: int) -> str:
    """Keep the head of the posting plus only the lines that carry extractable facts."""
    text = raw.text
    if len(text) <= max_chars:
        return text
    head_budget = min(1500, max_chars // 3)
    head, rest = text[:head_budget], text[head_budget:]
    picked, used = [], len(head)
    for ln in rest.splitlines():
        if KEY_LINE.search(ln):
            ln = ln[:400]
            if used + len(ln) + 1 > max_chars:
                break
            picked.append(ln)
            used += len(ln) + 1
    return head + "\n...\n" + "\n".join(picked)


def build_user_prompt(raw: RawPosting, snippet: str) -> str:
    meta = [f"Source: {raw.source}"]
    if raw.company_hint and raw.source != "hackernews":
        meta.append(f"Company (from job board): {raw.company_hint}")
    return "\n".join(meta) + "\n<<<TEXT\n" + snippet + "\nTEXT>>>"


# ------------------------------------------------------------------ validation

def _parse(text: str) -> ExtractionResult:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in response")
    data = json.loads(t[start:end + 1])
    if isinstance(data, list):
        data = {"roles": data}
    return ExtractionResult.model_validate(data)


def _number_in(snippet: str, value: float) -> bool:
    digits = re.sub(r"[^\d.k]", "", snippet.lower().replace(",", ""))
    v = int(value) if float(value).is_integer() else value
    forms = {str(v), f"{v / 1000:g}k"} if isinstance(v, int) else {str(v)}
    return any(f in digits for f in forms) or any(f in snippet.replace(",", "") for f in forms)


GUARDED = ["company", "seniority", "city", "state", "country", "remote_policy", "salary_min", "salary_max",
           "currency", "salary_period", "employment_type", "visa_sponsorship", "posted_date"]


def evidence_guard(role: ExtractedRole, source_text: str) -> tuple[ExtractedRole, list[str]]:
    """Null every field whose evidence is missing or not found verbatim in the source text."""
    hay = squash(source_text)
    dropped = []
    ev = {k: v for k, v in (role.evidence or {}).items() if isinstance(v, str) and v.strip()}
    for f in GUARDED:
        val = getattr(role, f)
        if val is None:
            ev.pop(f, None)
            continue
        snip = ev.get(f)
        ok = bool(snip) and squash(snip) in hay
        if ok and f in ("salary_min", "salary_max"):
            ok = _number_in(snip, val)
        if not ok:
            setattr(role, f, None)
            ev.pop(f, None)
            dropped.append(f)
    if role.salary_min is None and role.salary_max is None:
        role.currency, role.salary_period = None, None
        ev.pop("currency", None)
    kept = [s for s in role.skills if re.search(rf"(?<![\w]){re.escape(s.lower())}(?![\w])", source_text.lower())]
    if len(kept) < len(role.skills):
        dropped.append("skills(partial)")
    role.skills = kept
    if not kept:
        ev.pop("skills", None)
    if ev.get("title") and squash(ev["title"]) not in hay:
        ev.pop("title")
    role.evidence = ev
    return role, dropped


# ------------------------------------------------------------------ extractor


class BudgetExceeded(Exception):
    pass


@dataclass
class Extractor:
    provider: LLMProvider
    conn: sqlite3.Connection
    max_input_chars: int = 6000
    price_in: float = 1.0
    price_out: float = 5.0
    budget_usd: float = 5.0
    run_id: str | None = None
    max_attempts: int = 2
    spent_usd: float = 0.0
    stats: dict = field(default_factory=lambda: {"llm_calls": 0, "cache_hits": 0, "failures": 0,
                                                 "fields_dropped_by_guard": 0})

    def _cost(self, tin: int, tout: int) -> float:
        if isinstance(self.provider, MockProvider):
            return 0.0
        return tin / 1e6 * self.price_in + tout / 1e6 * self.price_out

    def _log_call(self, source_key, tin, tout, cost, ok, err=None):
        self.conn.execute(
            "INSERT INTO llm_calls(run_id,source_key,model,input_tokens,output_tokens,cost_usd,ok,error,called_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (self.run_id, source_key, self.provider.model, tin, tout, cost, int(ok), err, utcnow()))

    def extract(self, raw: RawPosting) -> list[tuple[ExtractedRole, list[str]]]:
        snippet = build_snippet(raw, self.max_input_chars)
        user = build_user_prompt(raw, snippet)
        key = hashlib.sha256(f"{PROMPT_VERSION}|{self.provider.model}|{user}".encode()).hexdigest()
        row = self.conn.execute("SELECT output_json FROM extraction_cache WHERE cache_key=?", (key,)).fetchone()
        source_key = f"{raw.source}:{raw.board}:{raw.source_job_id}"
        if row:
            self.stats["cache_hits"] += 1
            result = ExtractionResult.model_validate_json(row["output_json"])
        else:
            if self.spent_usd >= self.budget_usd:
                raise BudgetExceeded(f"LLM budget ${self.budget_usd:.2f} reached")
            result = self._call(user, source_key)
            if result is None:
                return []
            self.conn.execute("INSERT OR REPLACE INTO extraction_cache VALUES (?,?,?,?)",
                              (key, self.provider.model, result.model_dump_json(), utcnow()))
            self.conn.commit()
        out = []
        for role in result.roles:
            role = role.model_copy(deep=True)
            role, dropped = evidence_guard(role, snippet)
            self.stats["fields_dropped_by_guard"] += len(dropped)
            out.append((role, dropped))
        return out

    def _call(self, user: str, source_key: str) -> ExtractionResult | None:
        prompt, last_err = user, None
        for attempt in range(1, self.max_attempts + 1):
            try:
                text, tin, tout = self.provider.complete(SYSTEM_PROMPT, prompt)
            except Exception as e:  # network / 5xx / 429 from the provider
                last_err = f"provider_error: {e!r}"
                self._log_call(source_key, 0, 0, 0.0, False, last_err)
                time.sleep(min(30, 2 ** attempt)) if not isinstance(self.provider, MockProvider) else None
                continue
            cost = self._cost(tin, tout)
            self.spent_usd += cost
            self.stats["llm_calls"] += 1
            try:
                res = _parse(text)
                self._log_call(source_key, tin, tout, cost, True)
                return res
            except (ValueError, ValidationError, json.JSONDecodeError) as e:
                last_err = f"invalid_output: {str(e)[:300]}"
                self._log_call(source_key, tin, tout, cost, False, last_err)
                prompt = (user + "\n\nYour previous answer was invalid: " + str(e)[:500] +
                          "\nReturn ONLY the JSON object described in the instructions.")
        self.stats["failures"] += 1
        log(lg, logging.ERROR, "extraction failed", source_key=source_key, error=last_err)
        return None
