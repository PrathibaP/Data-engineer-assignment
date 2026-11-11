"""Source A: Greenhouse public job board API (JSON, HTML-escaped descriptions)."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from pipeline.logger import get_logger, log
from pipeline.models import RawPosting
from pipeline.transform.normalize import html_to_text, is_target_title, looks_us, normalize_company
from pipeline.ingest.http import PoliteClient
from pipeline.ingest import SourceResult

lg = get_logger("greenhouse")
URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def _date(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_board(token: str, payload: dict) -> SourceResult:
    res = SourceResult("greenhouse", token, ok=True, complete=True)
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return SourceResult("greenhouse", token, ok=False, complete=False, errors=["unexpected payload: no 'jobs' list"])
    res.total_seen = len(jobs)
    for job in jobs:
        try:
            title = (job.get("title") or "").strip()
            loc = (job.get("location") or {}).get("name") or ""
            offices = "; ".join(o.get("name", "") for o in job.get("offices") or [] if o)
            text = html_to_text(job.get("content") or "")
            if not is_target_title(title, text):
                continue
            loc_all = "; ".join(x for x in (loc, offices) if x)
            if looks_us(loc_all) is False:
                continue
            res.postings.append(RawPosting(
                source="greenhouse", board=token, source_job_id=str(job["id"]),
                url=job.get("absolute_url") or f"https://boards.greenhouse.io/{token}/jobs/{job['id']}",
                company_hint=normalize_company(job.get("company_name") or token),
                title_hint=title, location_hint=loc_all or None,
                text=f"Title: {title}\nLocation: {loc_all}\n\n{text}",
                posted_date_hint=_date(job.get("first_published")),  # updated_at is NOT a posted date
                structured={"updated_at": job.get("updated_at"), "requisition_id": job.get("requisition_id")},
            ))
        except Exception as e:  # one malformed job must not drop the board
            res.errors.append(f"job {job.get('id')}: {e!r}")
            log(lg, logging.WARNING, "skipping malformed job", board=token, job_id=job.get("id"), error=repr(e))
    return res


def scrape(client: PoliteClient, token: str) -> SourceResult:
    r = client.get(URL.format(token=token), expect="json")
    if not r.ok:
        return SourceResult("greenhouse", token, ok=False, complete=False, errors=[r.error or "fetch failed"])
    try:
        payload = r.json()
    except json.JSONDecodeError as e:
        return SourceResult("greenhouse", token, ok=False, complete=False, errors=[f"invalid json: {e}"])
    res = parse_board(token, payload)
    log(lg, logging.INFO, "board parsed", board=token, total=res.total_seen, kept=len(res.postings))
    return res
