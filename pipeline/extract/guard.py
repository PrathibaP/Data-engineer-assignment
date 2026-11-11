"""Output validation and the anti-hallucination evidence guard.

1. _parse: tolerate code fences / chatter, then strict Pydantic validation (enums, types).
2. evidence_guard: every non-null field needs a snippet found verbatim in the input; salary numbers must
   appear inside their snippet; each skill must occur in the text. Anything unsupported becomes null.
"""
from __future__ import annotations

import json
import re

from pipeline.models import ExtractedRole, ExtractionResult
from pipeline.transform.normalize import squash

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


