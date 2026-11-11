"""Source adapters. Each returns a SourceResult; none of them raise on network or parse failures."""
from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.models import RawPosting


@dataclass
class SourceResult:
    source: str
    board: str
    ok: bool  # fetched and parsed
    complete: bool  # full listing obtained -> safe to use for closed-posting detection
    postings: list[RawPosting] = field(default_factory=list)
    total_seen: int = 0  # postings before role/location pre-filter
    errors: list[str] = field(default_factory=list)
    note: str | None = None
