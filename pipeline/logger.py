"""Structured JSON-lines logging to logs/pipeline.jsonl plus readable console output."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "ctx", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger("pipeline")
    root.setLevel(level)
    fh = logging.FileHandler(Path(log_dir) / "pipeline.jsonl")
    fh.setFormatter(JsonFormatter())
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(fh)
    root.addHandler(ch)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"pipeline.{name}")


def log(logger: logging.Logger, level: int, msg: str, **ctx) -> None:
    """Log with structured context fields, e.g. log(lg, INFO, "fetched", url=u, status=200)."""
    suffix = " ".join(f"{k}={v}" for k, v in ctx.items())
    logger.log(level, f"{msg} {suffix}".strip(), extra={"ctx": ctx})
