"""Source B: Lever public postings API. Second schema normalised into the same RawPosting model.

The endpoint sometimes answers with an HTML page (status 200, text/html); the client validates the
Content-Type and retries, then records a failure instead of trying to json-parse HTML.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ..logger import get_logger, log
from ..models import RawPosting
from ..normalizer import html_to_text, is_target_title, looks_us, normalize_company
from ..scraper_base import PoliteClient
from . import SourceResult

lg = get_logger("lever")
URL = "https://api.lever.co/v0/postings/{site}?mode=json"
INTERVALS = {"per-year-salary": "year", "per-month-salary": "month", "per-week-salary": "week",
             "per-day-wage": "day", "per-hour-wage": "hour"}


def _text(p: dict) -> str:
    parts = [p.get("descriptionPlain") or html_to_text(p.get("description") or "")]
    for lst in p.get("lists") or []:
        parts.append(f"{lst.get('text', '')}:\n{html_to_text(lst.get('content') or '')}")
    parts.append(p.get("additionalPlain") or html_to_text(p.get("additional") or ""))
    if p.get("salaryDescriptionPlain"):
        parts.append(p["salaryDescriptionPlain"])
    return "\n\n".join(x for x in parts if x)


def parse_site(site: str, payload) -> SourceResult:
    if isinstance(payload, dict) and payload.get("ok") is False:
        return SourceResult("lever", site, ok=False, complete=False, errors=[f"api error: {payload.get('error')}"])
    if not isinstance(payload, list):
        return SourceResult("lever", site, ok=False, complete=False, errors=["unexpected payload type"])
    res = SourceResult("lever", site, ok=True, complete=True, total_seen=len(payload))
    for p in payload:
        try:
            title = (p.get("text") or "").strip()
            cats = p.get("categories") or {}
            locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
            loc_all = "; ".join(locs)
            if p.get("country"):
                loc_all = f"{loc_all} ({p['country']})" if loc_all else p["country"]
            text = _text(p)
            if not is_target_title(title, text):
                continue
            if looks_us(loc_all) is False:
                continue
            created = p.get("createdAt")
            posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).date() if isinstance(created, (int, float)) else None
            header = [f"Title: {title}", f"Location: {loc_all}"]
            if cats.get("commitment"):
                header.append(f"Commitment: {cats['commitment']}")
            if p.get("workplaceType") and p["workplaceType"] != "unspecified":
                header.append(f"Workplace type: {p['workplaceType']}")
            sal = p.get("salaryRange") or {}
            if sal.get("min") is not None:
                header.append(f"Salary range: {sal.get('currency', '')} {sal['min']} - {sal.get('max')} "
                              f"({INTERVALS.get(sal.get('interval'), sal.get('interval'))})")
            res.postings.append(RawPosting(
                source="lever", board=site, source_job_id=str(p["id"]),
                url=p.get("hostedUrl") or f"https://jobs.lever.co/{site}/{p['id']}",
                company_hint=normalize_company(site), title_hint=title, location_hint=loc_all or None,
                text="\n".join(header) + "\n\n" + text,
                posted_date_hint=posted,
                structured={"workplaceType": p.get("workplaceType"), "commitment": cats.get("commitment"),
                            "salaryRange": sal or None},
            ))
        except Exception as e:
            res.errors.append(f"posting {p.get('id')}: {e!r}")
            log(lg, logging.WARNING, "skipping malformed posting", site=site, error=repr(e))
    return res


def scrape(client: PoliteClient, site: str) -> SourceResult:
    r = client.get(URL.format(site=site), expect="json")
    if not r.ok:
        return SourceResult("lever", site, ok=False, complete=False, errors=[r.error or "fetch failed"])
    try:
        payload = r.json()
    except json.JSONDecodeError as e:
        return SourceResult("lever", site, ok=False, complete=False, errors=[f"invalid json: {e}"])
    res = parse_site(site, payload)
    log(lg, logging.INFO, "site parsed", site=site, total=res.total_seen, kept=len(res.postings))
    return res
