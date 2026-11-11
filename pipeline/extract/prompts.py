"""Prompt text and input trimming: only targeted snippets reach the LLM, never whole pages."""
from __future__ import annotations

import re

from pipeline.models import RawPosting

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


