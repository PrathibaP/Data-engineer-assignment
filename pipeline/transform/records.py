"""Turn one extracted role into a normalised source record (the unit the store/dedup layer works on).

Authoritative source metadata (ATS board company/title, Lever structured salary / workplace type /
commitment, source publish dates) overrides or fills LLM output; final US + role eligibility is decided here.
"""
from __future__ import annotations

from pipeline.ingest import lever
from pipeline.models import ExtractedRole, RawPosting
from pipeline.transform.normalize import (US_STATES, annualize, clean_title, company_key, is_target_title, looks_us,
                                          normalize_city, normalize_company, normalize_country, normalize_title,
                                          seniority_from_title, title_key)

LEVER_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}
LEVER_COMMITMENT = {"full-time": "full_time", "full time": "full_time", "intern": "intern", "internship": "intern",
                    "contract": "contract", "contractor": "contract"}


def to_record(raw: RawPosting, role: ExtractedRole, content_hash: str) -> tuple[dict | None, str]:
    """Merge LLM output with authoritative source metadata. Returns (record, reject_reason)."""
    ev = dict(role.evidence)
    is_ats = raw.source != "hackernews"
    if is_ats:
        company = normalize_company(raw.company_hint)
        ev["company"] = f"job board: {raw.board}"
        title = clean_title(raw.title_hint or role.title)
        ev["title"] = f"Title: {raw.title_hint}"
    else:
        company = normalize_company(role.company or raw.company_hint)
        if not role.company and raw.company_hint:
            ev["company"] = raw.text.splitlines()[0][:160]
        title = clean_title(role.title)
    if not company:
        return None, "no_company"
    seniority = role.seniority.value if role.seniority else seniority_from_title(title)
    if seniority and "seniority" not in ev:
        ev["seniority"] = ev.get("title", title)
    city = normalize_city(role.city)
    country = normalize_country(role.country)
    if not country and role.state and (role.state.upper() in US_STATES or role.state.title() in US_STATES.values()):
        country, ev["country"] = "US", ev.get("state", role.state)
    if not country and is_ats and looks_us(raw.location_hint):
        country, ev["country"] = "US", f"Location: {raw.location_hint}"
    remote = role.remote_policy.value if role.remote_policy else None
    wt = (raw.structured.get("workplaceType") or "").lower()
    if not remote and wt in LEVER_WORKPLACE:
        remote, ev["remote_policy"] = LEVER_WORKPLACE[wt], f"Workplace type: {wt}"
    emp = role.employment_type.value if role.employment_type else None
    cm = (raw.structured.get("commitment") or "").lower()
    if not emp and cm in LEVER_COMMITMENT:
        emp, ev["employment_type"] = LEVER_COMMITMENT[cm], f"Commitment: {raw.structured['commitment']}"
    period = role.salary_period.value if role.salary_period else None
    smin, smax, cur = annualize(role.salary_min, period), annualize(role.salary_max, period), role.currency
    sr = raw.structured.get("salaryRange") or {}
    if sr.get("min") is not None:  # Lever structured salary beats free text
        per = lever.INTERVALS.get(sr.get("interval"))
        smin, smax, cur = annualize(sr.get("min"), per), annualize(sr.get("max"), per), sr.get("currency") or cur
        ev["salary_min"] = ev["salary_max"] = ev["currency"] = f"salaryRange: {sr}"
    if (smin or smax) and not cur:
        smin = smax = None  # never emit a salary without a stated currency
    if (smin or smax) and period is None and not sr:
        ev["salary_period_note"] = "no period stated; >=10k treated as annual"
    posted = raw.posted_date_hint.isoformat() if raw.posted_date_hint else (
        role.posted_date.isoformat() if role.posted_date else None)
    if raw.posted_date_hint:
        ev["posted_date"] = f"source metadata: {raw.posted_date_hint.isoformat()}"
    if not is_target_title(title, raw.text):
        return None, "not_target_role"
    if country != "US":
        return None, "not_us" if country else "location_unknown"
    if is_ats:
        skey = f"{raw.source}:{raw.board}:{raw.source_job_id}"
    else:
        skey = f"hackernews:{raw.board}:{raw.source_job_id}:{normalize_title(title).replace(' ', '-')[:60]}"
    return {
        "source_key": skey, "source": raw.source, "board": raw.board, "source_job_id": raw.source_job_id,
        "url": raw.url, "outbound_urls": raw.outbound_urls, "content_hash": content_hash,
        "title_raw": raw.title_hint or role.title, "location_raw": raw.location_hint,
        "company": company, "company_key": company_key(company), "title": title, "title_key": title_key(title),
        "seniority": seniority, "city": city, "country": country, "remote_policy": remote,
        "salary_min": smin, "salary_max": smax, "currency": cur if (smin or smax) else None,
        "employment_type": emp, "skills": role.skills, "visa_sponsorship": role.visa_sponsorship,
        "posted_date": posted, "evidence": ev,
    }, ""
