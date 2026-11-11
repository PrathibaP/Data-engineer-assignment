"""Command line entry point.

    python -m pipeline run [--offline] [--fixtures DIR] [--today YYYY-MM-DD]
    python -m pipeline export | label-template | evaluate
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from pipeline.config import load_settings
from pipeline.logger import setup_logging
from pipeline.orchestrator import run
from pipeline.report import export as exporter
from pipeline.store.db import connect


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
    from pipeline.report import evaluate as evaluator

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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli())
