"""Extraction stage: snippet -> (cache | LLM) -> validate -> evidence guard -> roles.

Cost controls: deterministic pre-filter upstream, snippet trimming, an extraction cache keyed by
(prompt version, model, input hash), per-call token/cost logging, one repair retry, a hard run budget.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field

from pydantic import ValidationError

from pipeline.extract.guard import _parse, evidence_guard
from pipeline.extract.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_snippet, build_user_prompt
from pipeline.extract.providers import LLMProvider, MockProvider
from pipeline.ingest.http import utcnow
from pipeline.logger import get_logger, log
from pipeline.models import ExtractedRole, ExtractionResult, RawPosting

lg = get_logger("extractor")


class BudgetExceeded(Exception):
    pass


@dataclass
class Extractor:
    provider: LLMProvider
    conn: sqlite3.Connection
    max_input_chars: int = 6000
    price_in: float = 1.0
    price_out: float = 5.0
    budget_usd: float = 5.0
    run_id: str | None = None
    max_attempts: int = 2
    spent_usd: float = 0.0
    stats: dict = field(default_factory=lambda: {"llm_calls": 0, "cache_hits": 0, "failures": 0,
                                                 "fields_dropped_by_guard": 0})

    def _cost(self, tin: int, tout: int) -> float:
        if isinstance(self.provider, MockProvider):
            return 0.0
        return tin / 1e6 * self.price_in + tout / 1e6 * self.price_out

    def _log_call(self, source_key, tin, tout, cost, ok, err=None):
        self.conn.execute(
            "INSERT INTO llm_calls(run_id,source_key,model,input_tokens,output_tokens,cost_usd,ok,error,called_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (self.run_id, source_key, self.provider.model, tin, tout, cost, int(ok), err, utcnow()))

    def extract(self, raw: RawPosting) -> list[tuple[ExtractedRole, list[str]]]:
        snippet = build_snippet(raw, self.max_input_chars)
        user = build_user_prompt(raw, snippet)
        key = hashlib.sha256(f"{PROMPT_VERSION}|{self.provider.model}|{user}".encode()).hexdigest()
        row = self.conn.execute("SELECT output_json FROM extraction_cache WHERE cache_key=?", (key,)).fetchone()
        source_key = f"{raw.source}:{raw.board}:{raw.source_job_id}"
        if row:
            self.stats["cache_hits"] += 1
            result = ExtractionResult.model_validate_json(row["output_json"])
        else:
            if self.spent_usd >= self.budget_usd:
                raise BudgetExceeded(f"LLM budget ${self.budget_usd:.2f} reached")
            result = self._call(user, source_key)
            if result is None:
                return []
            self.conn.execute("INSERT OR REPLACE INTO extraction_cache VALUES (?,?,?,?)",
                              (key, self.provider.model, result.model_dump_json(), utcnow()))
            self.conn.commit()
        out = []
        for role in result.roles:
            role = role.model_copy(deep=True)
            role, dropped = evidence_guard(role, snippet)
            self.stats["fields_dropped_by_guard"] += len(dropped)
            out.append((role, dropped))
        return out

    def _call(self, user: str, source_key: str) -> ExtractionResult | None:
        prompt, last_err = user, None
        for attempt in range(1, self.max_attempts + 1):
            try:
                text, tin, tout = self.provider.complete(SYSTEM_PROMPT, prompt)
            except Exception as e:  # network / 5xx / 429 from the provider
                last_err = f"provider_error: {e!r}"
                self._log_call(source_key, 0, 0, 0.0, False, last_err)
                time.sleep(min(30, 2 ** attempt)) if not isinstance(self.provider, MockProvider) else None
                continue
            cost = self._cost(tin, tout)
            self.spent_usd += cost
            self.stats["llm_calls"] += 1
            try:
                res = _parse(text)
                self._log_call(source_key, tin, tout, cost, True)
                return res
            except (ValueError, ValidationError, json.JSONDecodeError) as e:
                last_err = f"invalid_output: {str(e)[:300]}"
                self._log_call(source_key, tin, tout, cost, False, last_err)
                prompt = (user + "\n\nYour previous answer was invalid: " + str(e)[:500] +
                          "\nReturn ONLY the JSON object described in the instructions.")
        self.stats["failures"] += 1
        log(lg, logging.ERROR, "extraction failed", source_key=source_key, error=last_err)
        return None
