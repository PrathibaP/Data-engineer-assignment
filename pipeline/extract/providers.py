"""LLM provider adapters behind one small interface (complete -> text, input tokens, output tokens).

AnthropicProvider / OpenAIProvider call the HTTP APIs directly (no SDK dependency). MockProvider is an
offline, deterministic regex stand-in so the pipeline and tests run without a key; it is NOT an LLM.
"""
from __future__ import annotations

import json
import re
from typing import Protocol

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
        from pipeline.transform.normalize import is_target_title, seniority_from_title

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


