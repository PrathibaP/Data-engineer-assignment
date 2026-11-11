"""Pipeline orchestration + CLI.

    python -m pipeline run [--offline] [--fixtures DIR]   scrape -> extract -> dedup -> store -> export
    python -m pipeline export                              re-export CSVs from the database
    python -m pipeline label-template                      write data/evaluation/labels_template.csv (20 postings)
    python -m pipeline evaluate                            score extraction against data/evaluation/labels.csv

Stages are separate modules; this file only wires them together.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from . import exporter
from .change_tracker import Store
from .config import Settings, load_settings
from .database import connect
from .extractor import BudgetExceeded, Extractor, make_provider
from .logger import get_logger, log, setup_logging
from .models import ExtractedRole, RawPosting
from .normalizer import (US_STATES, annualize, clean_title, company_key, is_target_title, looks_us,
                         normalize_city, normalize_company, normalize_country, normalize_title,
                         seniority_from_title, title_key)
from .scraper_base import FixtureTransport, PoliteClient, RequestsTransport
from .sources import SourceResult, greenhouse, hackernews, lever

lg = get_logger("main")

LEVER_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}
LEVER_COMMITMENT = {"full-time": "full_time", "full time": "full_time", "intern": "intern", "internship": "intern",
                    "contract": "contract", "contractor": "contract"}


# ------------------------------------------------------------------ normalisation of one extracted role

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


# ------------------------------------------------------------------ scraping with substitution

def _scrape_boards(client, module, names, fallbacks, kind) -> tuple[list[SourceResult], list[str]]:
    results, notes, pool = [], [], [n for n in fallbacks if n not in names]
    for name in names:
        res = module.scrape(client, name)
        while res.ok and res.total_seen == 0 and pool:
            sub = pool.pop(0)
            notes.append(f"{kind} '{name}' returned 0 jobs; substituted '{sub}'")
            log(lg, logging.WARNING, "empty board, substituting", kind=kind, board=name, substitute=sub)
            results.append(res)  # keep the empty (complete) result so stale rows for it can close
            res = module.scrape(client, sub)
        results.append(res)
    return results, notes


# ------------------------------------------------------------------ run

def run(settings: Settings, today: date | None = None, offline: bool = False, now: str | None = None) -> dict:
    setup_logging(settings.log_dir)
    today = today or datetime.now(timezone.utc).date()
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_id = f"run_{now.replace(':', '').replace('-', '')[:15]}_{uuid.uuid4().hex[:6]}"
    conn = connect(settings.db_path)
    conn.execute("INSERT INTO runs(run_id, started_at) VALUES (?,?)", (run_id, now))
    conn.commit()
    transport = FixtureTransport(settings.fixtures_dir) if settings.fixtures_dir else RequestsTransport()
    client = PoliteClient(transport, conn, settings.raw_dir, settings.user_agent, settings.min_interval_s,
                          settings.timeout_s, settings.max_retries, run_id=run_id, offline=offline)
    provider = make_provider(settings)
    if settings.llm_provider == "mock":
        log(lg, logging.WARNING, "LLM_PROVIDER=mock: heuristic extractor, results are NOT real LLM output")
    extractor = Extractor(provider, conn, settings.llm_max_input_chars, settings.llm_price_in_per_mtok,
                          settings.llm_price_out_per_mtok, settings.llm_budget_usd, run_id)
    store = Store(conn, run_id, now)
    stats: dict = {"run_id": run_id, "started_at": now, "provider": settings.llm_provider, "model": provider.model,
                   "sources": [], "rejected": {}, "notes": []}

    results, notes = _scrape_boards(client, greenhouse, settings.greenhouse_tokens,
                                    settings.greenhouse_fallback_tokens, "greenhouse")
    r2, n2 = _scrape_boards(client, lever, settings.lever_sites, settings.lever_fallback_sites, "lever")
    results += r2
    stats["notes"] += notes + n2
    results.append(hackernews.scrape(client, today, settings.hn_thread_id))  # HN last: merges into ATS records

    budget_hit = False
    for res in results:
        kept = 0
        for raw in res.postings:
            chash = hashlib.sha256(raw.text.encode()).hexdigest()
            try:
                if budget_hit:
                    raise BudgetExceeded("budget already exhausted")
                roles = extractor.extract(raw)
                if not roles and raw.source != "hackernews":
                    roles = [(ExtractedRole(title=raw.title_hint or ""), [])]
                for role, _dropped in roles:
                    rec, why = to_record(raw, role, chash)
                    if rec is None:
                        stats["rejected"][why] = stats["rejected"].get(why, 0) + 1
                        continue
                    store.upsert(rec)
                    kept += 1
                if not roles:
                    stats["rejected"]["no_roles_extracted"] = stats["rejected"].get("no_roles_extracted", 0) + 1
            except BudgetExceeded as e:
                budget_hit = True
                res.complete = False
                log(lg, logging.ERROR, "LLM budget reached; remaining postings skipped", error=str(e))
                _keep_alive(conn, raw, run_id)
            except Exception as e:  # one bad posting never kills the run
                log(lg, logging.ERROR, "posting failed", source=raw.source, job=raw.source_job_id, error=repr(e))
                stats["rejected"]["error"] = stats["rejected"].get("error", 0) + 1
                _keep_alive(conn, raw, run_id)
            conn.commit()
        closed = store.close_missing(res.source, res.board) if (res.ok and res.complete) else 0
        stats["sources"].append({"source": res.source, "board": res.board, "ok": res.ok, "complete": res.complete,
                                 "seen": res.total_seen, "prefiltered": len(res.postings), "stored_roles": kept,
                                 "inactivated": closed, "errors": res.errors[:5], "note": res.note})
        conn.commit()

    cost = conn.execute("SELECT COALESCE(SUM(cost_usd),0) c, COUNT(*) n, COALESCE(SUM(input_tokens),0) ti, "
                        "COALESCE(SUM(output_tokens),0) tout FROM llm_calls WHERE run_id=?", (run_id,)).fetchone()
    stats.update(store.stats)
    stats.update(extractor.stats)
    stats["llm_cost_usd"] = round(cost["c"], 6)
    stats["llm_input_tokens"], stats["llm_output_tokens"] = cost["ti"], cost["tout"]
    stats["failed_urls"] = [dict(r) for r in conn.execute(
        "SELECT url, reason, status_code, attempts FROM fetch_failures WHERE run_id=?", (run_id,))]
    stats["postings_total"] = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
    stats["postings_open"] = conn.execute("SELECT COUNT(*) FROM postings WHERE status='open'").fetchone()[0]
    stats["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = "ok" if all(s["ok"] for s in stats["sources"]) else "partial"
    conn.execute("UPDATE runs SET finished_at=?, status=?, stats_json=? WHERE run_id=?",
                 (stats["finished_at"], status, json.dumps(stats, default=str), run_id))
    conn.commit()
    exporter.export_all(conn, settings.export_dir, stats)
    log(lg, logging.INFO, "run finished", run_id=run_id, status=status, new=stats["new_postings"],
        merged=stats["merged_sources"], changes=stats["changes"], cost_usd=stats["llm_cost_usd"])
    conn.close()
    return stats


def _keep_alive(conn, raw: RawPosting, run_id: str) -> None:
    """A posting we saw but could not process must not be closed by the closure step."""
    conn.execute("UPDATE source_postings SET last_run_id=? WHERE source=? AND board=? AND source_job_id=?",
                 (run_id, raw.source, raw.board, raw.source_job_id))


# ------------------------------------------------------------------ CLI

def cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="scrape, extract, dedup, store, export")
    r.add_argument("--offline", action="store_true", help="replay the raw response cache, no network")
    r.add_argument("--fixtures", type=Path, help="serve HTTP from a fixture directory (manifest.json)")
    r.add_argument("--today", type=date.fromisoformat, help="override run date (selects the HN month)")
    sub.add_parser("export")
    lt = sub.add_parser("label-template")
    lt.add_argument("--n", type=int, default=20)
    lt.add_argument("--min-hn", type=int, default=10)
    sub.add_parser("evaluate")
    args = ap.parse_args(argv)
    cmd = args.cmd or "run"
    settings = load_settings()
    setup_logging(settings.log_dir)
    if cmd == "run":
        if getattr(args, "fixtures", None):
            settings.fixtures_dir = args.fixtures
        stats = run(settings, today=getattr(args, "today", None), offline=getattr(args, "offline", False))
        print(json.dumps({k: stats[k] for k in ("run_id", "postings_total", "postings_open", "new_postings",
                                                 "merged_sources", "review_queued", "changes", "closed",
                                                 "llm_calls", "cache_hits", "llm_cost_usd")}, indent=2))
        return 0
    from . import evaluator

    conn = connect(settings.db_path)
    if cmd == "export":
        exporter.export_all(conn, settings.export_dir, None)
    elif cmd == "label-template":
        p = evaluator.write_label_template(conn, settings.eval_dir, args.n, args.min_hn)
        print(f"wrote {p}")
    elif cmd == "evaluate":
        report = evaluator.evaluate(conn, settings.eval_dir)
        print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(cli())
