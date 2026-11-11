"""Pipeline orchestration: five explicit stages over one RunContext.

    1. ingest      fetch every board/thread politely -> SourceResult (RawPostings, ok/complete flags)
    2. extract     RawPosting -> cached LLM -> evidence-checked roles
    3. resolve     role -> normalised record -> dedup match -> idempotent upsert + change history
    4. reconcile   close postings missing from boards fetched *completely* this run
    5. report      run stats, cost, exports

Each stage only talks to the next through plain data (SourceResult, RawPosting, record dicts), so any
stage can be tested or replaced on its own. Failures are contained per board (ingest) and per posting
(extract/resolve); nothing a single source does can abort the run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from pipeline.config import Settings
from pipeline.extract import BudgetExceeded, Extractor, make_provider
from pipeline.ingest import SourceResult, greenhouse, hackernews, lever
from pipeline.ingest.http import FixtureTransport, PoliteClient, RequestsTransport
from pipeline.logger import get_logger, log, setup_logging
from pipeline.models import ExtractedRole, RawPosting
from pipeline.report import export as exporter
from pipeline.store.db import connect
from pipeline.store.repository import Store
from pipeline.transform.records import to_record

lg = get_logger("orchestrator")


@dataclass
class RunContext:
    settings: Settings
    conn: sqlite3.Connection
    run_id: str
    now: str
    today: date
    client: PoliteClient
    extractor: Extractor
    store: Store
    stats: dict = field(default_factory=dict)
    budget_hit: bool = False

    def reject(self, reason: str) -> None:
        self.stats["rejected"][reason] = self.stats["rejected"].get(reason, 0) + 1


# ------------------------------------------------------------------ setup

def _context(settings: Settings, today: date | None, offline: bool, now: str | None) -> RunContext:
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
    stats = {"run_id": run_id, "started_at": now, "provider": settings.llm_provider, "model": provider.model,
             "sources": [], "rejected": {}, "notes": []}
    return RunContext(settings, conn, run_id, now, today, client, extractor, Store(conn, run_id, now), stats)


# ------------------------------------------------------------------ 1. ingest

def _scrape_boards(ctx: RunContext, module, names: list[str], fallbacks: list[str], kind: str) -> list[SourceResult]:
    """Scrape each board; an empty board is replaced by the next unused fallback (and noted)."""
    results, pool = [], [n for n in fallbacks if n not in names]
    for name in names:
        res = module.scrape(ctx.client, name)
        while res.ok and res.total_seen == 0 and pool:
            sub = pool.pop(0)
            ctx.stats["notes"].append(f"{kind} '{name}' returned 0 jobs; substituted '{sub}'")
            log(lg, logging.WARNING, "empty board, substituting", kind=kind, board=name, substitute=sub)
            results.append(res)  # keep the empty (complete) result so stale rows for it can close
            res = module.scrape(ctx.client, sub)
        results.append(res)
    return results


def ingest(ctx: RunContext) -> list[SourceResult]:
    s = ctx.settings
    results = _scrape_boards(ctx, greenhouse, s.greenhouse_tokens, s.greenhouse_fallback_tokens, "greenhouse")
    results += _scrape_boards(ctx, lever, s.lever_sites, s.lever_fallback_sites, "lever")
    results.append(hackernews.scrape(ctx.client, ctx.today, s.hn_thread_id))  # last: HN merges into ATS records
    return results


# ------------------------------------------------------------------ 2. extract

def extract(ctx: RunContext, raw: RawPosting) -> list[ExtractedRole]:
    if ctx.budget_hit:
        raise BudgetExceeded("budget already exhausted")
    roles = [r for r, _dropped in ctx.extractor.extract(raw)]
    if not roles and raw.source != "hackernews":  # an ATS posting is always one role, even if the LLM balked
        roles = [ExtractedRole(title=raw.title_hint or "")]
    return roles


# ------------------------------------------------------------------ 3. resolve

def resolve(ctx: RunContext, raw: RawPosting, roles: list[ExtractedRole]) -> int:
    content_hash = hashlib.sha256(raw.text.encode()).hexdigest()
    stored = 0
    for role in roles:
        rec, why = to_record(raw, role, content_hash)
        if rec is None:
            ctx.reject(why)
            continue
        ctx.store.upsert(rec)  # dedup match + idempotent upsert + change history
        stored += 1
    if not roles:
        ctx.reject("no_roles_extracted")
    return stored


def _keep_alive(ctx: RunContext, raw: RawPosting) -> None:
    """A posting we saw but could not process must not be closed by reconcile."""
    ctx.conn.execute("UPDATE source_postings SET last_run_id=? WHERE source=? AND board=? AND source_job_id=?",
                     (ctx.run_id, raw.source, raw.board, raw.source_job_id))


def process_source(ctx: RunContext, res: SourceResult) -> int:
    stored = 0
    for raw in res.postings:
        try:
            stored += resolve(ctx, raw, extract(ctx, raw))
        except BudgetExceeded as e:
            ctx.budget_hit, res.complete = True, False
            log(lg, logging.ERROR, "LLM budget reached; remaining postings skipped", error=str(e))
            _keep_alive(ctx, raw)
        except Exception as e:  # one bad posting never kills the run
            log(lg, logging.ERROR, "posting failed", source=raw.source, job=raw.source_job_id, error=repr(e))
            ctx.reject("error")
            _keep_alive(ctx, raw)
        ctx.conn.commit()
    return stored


# ------------------------------------------------------------------ 4. reconcile

def reconcile(ctx: RunContext, res: SourceResult) -> int:
    """Closure is only safe when the board was fetched and parsed completely in this run."""
    return ctx.store.close_missing(res.source, res.board) if (res.ok and res.complete) else 0


# ------------------------------------------------------------------ 5. report

def report(ctx: RunContext) -> dict:
    conn, stats = ctx.conn, ctx.stats
    cost = conn.execute("SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(input_tokens),0) ti, "
                        "COALESCE(SUM(output_tokens),0) tout FROM llm_calls WHERE run_id=?", (ctx.run_id,)).fetchone()
    stats.update(ctx.store.stats)
    stats.update(ctx.extractor.stats)
    stats["llm_cost_usd"] = round(cost["c"], 6)
    stats["llm_input_tokens"], stats["llm_output_tokens"] = cost["ti"], cost["tout"]
    stats["failed_urls"] = [dict(r) for r in conn.execute(
        "SELECT url, reason, status_code, attempts FROM fetch_failures WHERE run_id=?", (ctx.run_id,))]
    stats["postings_total"] = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
    stats["postings_open"] = conn.execute("SELECT COUNT(*) FROM postings WHERE status='open'").fetchone()[0]
    stats["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = "ok" if all(s["ok"] for s in stats["sources"]) else "partial"
    conn.execute("UPDATE runs SET finished_at=?, status=?, stats_json=? WHERE run_id=?",
                 (stats["finished_at"], status, json.dumps(stats, default=str), ctx.run_id))
    conn.commit()
    exporter.export_all(conn, ctx.settings.export_dir, stats)
    log(lg, logging.INFO, "run finished", run_id=ctx.run_id, status=status, new=stats["new_postings"],
        merged=stats["merged_sources"], changes=stats["changes"], cost_usd=stats["llm_cost_usd"])
    return stats


# ------------------------------------------------------------------ run

def run(settings: Settings, today: date | None = None, offline: bool = False, now: str | None = None) -> dict:
    ctx = _context(settings, today, offline, now)
    try:
        for res in ingest(ctx):
            stored = process_source(ctx, res)
            closed = reconcile(ctx, res)
            ctx.stats["sources"].append({
                "source": res.source, "board": res.board, "ok": res.ok, "complete": res.complete,
                "seen": res.total_seen, "prefiltered": len(res.postings), "stored_roles": stored,
                "inactivated": closed, "errors": res.errors[:5], "note": res.note})
            ctx.conn.commit()
        return report(ctx)
    finally:
        ctx.conn.close()
