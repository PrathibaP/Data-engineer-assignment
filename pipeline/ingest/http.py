"""Polite HTTP client shared by every source.

* robots.txt checked per domain (RFC 9309 semantics: 4xx -> allowed, 5xx/unreachable -> disallowed),
  and its Crawl-delay is honoured if it is stricter than our own limit.
* At most one request per `min_interval_s` per domain (default 1.0s => 1 rps), robots fetch included.
* Retries with exponential backoff + jitter on network errors, 429, 5xx and unexpected Content-Type;
  Retry-After is honoured. Other 4xx fail fast.
* Every successful body is cached on disk (content-addressed) and indexed in `raw_responses`.
* Failures are returned as values (FetchResult.ok == False) and logged to `fetch_failures`; never raised.

The network layer is a pluggable Transport so tests and offline runs use recorded fixtures.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import sqlite3
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlparse

from pipeline.logger import get_logger, log

lg = get_logger("http")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    text: str


class Transport(Protocol):
    def send(self, url: str, headers: dict[str, str], timeout: float) -> Response: ...


class RequestsTransport:
    def __init__(self) -> None:
        import requests

        self._s = requests.Session()

    def send(self, url: str, headers: dict[str, str], timeout: float) -> Response:
        r = self._s.get(url, headers=headers, timeout=timeout)
        return Response(r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.text)


class FixtureTransport:
    """Serves responses from a directory with a manifest.json: {url: {file, status, content_type}}.

    Unknown URLs return 404 (so robots.txt is 'allowed' unless a fixture provides one).
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.manifest: dict = json.loads((self.root / "manifest.json").read_text())
        self.requests: list[str] = []

    def send(self, url: str, headers: dict[str, str], timeout: float) -> Response:
        self.requests.append(url)
        entry = self.manifest.get(url)
        if entry is None:
            return Response(404, {"content-type": "text/plain"}, "not found")
        if entry.get("raise"):
            raise ConnectionError(entry["raise"])
        body = (self.root / entry["file"]).read_text() if entry.get("file") else ""
        return Response(entry.get("status", 200), {"content-type": entry.get("content_type", "text/html")}, body)


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    content_type: str | None = None
    text: str = ""
    content_hash: str | None = None
    error: str | None = None
    attempts: int = 0
    from_cache: bool = False

    def json(self):
        return json.loads(self.text)


@dataclass
class PoliteClient:
    transport: Transport
    conn: sqlite3.Connection
    raw_dir: Path
    user_agent: str
    min_interval_s: float = 1.0
    timeout_s: float = 20.0
    max_retries: int = 4
    run_id: str | None = None
    offline: bool = False  # serve only from raw cache, never touch the network
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    _last_hit: dict[str, float] = field(default_factory=dict)
    _robots: dict[str, urllib.robotparser.RobotFileParser | None] = field(default_factory=dict)
    _robots_allow_all: dict[str, bool] = field(default_factory=dict)
    _crawl_delay: dict[str, float] = field(default_factory=dict)
    _robots_reason: dict[str, str] = field(default_factory=dict)

    # ---------- rate limiting ----------
    def _wait_turn(self, domain: str) -> None:
        interval = max(self.min_interval_s, self._crawl_delay.get(domain, 0.0))
        last = self._last_hit.get(domain)
        if last is not None:
            delta = self.clock() - last
            if delta < interval:
                self.sleep(interval - delta)
        self._last_hit[domain] = self.clock()

    # ---------- robots ----------
    def _load_robots(self, scheme: str, domain: str) -> None:
        if domain in self._robots or domain in self._robots_allow_all:
            return
        url = f"{scheme}://{domain}/robots.txt"
        resp, err = None, None
        for attempt in range(1, self.max_retries + 1):  # transient errors retried like any request
            self._wait_turn(domain)
            try:
                resp = self.transport.send(url, {"User-Agent": self.user_agent}, self.timeout_s)
                if resp.status < 500:
                    break
                err = f"http_{resp.status}"
            except Exception as e:
                resp, err = None, repr(e)
            if attempt < self.max_retries:
                self.sleep(min(60.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5))
        if resp is None or resp.status >= 500:  # RFC 9309: unreachable -> assume complete disallow
            log(lg, logging.WARNING, "robots.txt unreachable, skipping domain", domain=domain, error=err)
            self._robots_allow_all[domain] = False
            self._robots_reason[domain] = f"robots_txt_unreachable: {err}"[:300]
            return
        if 400 <= resp.status < 500:  # RFC 9309: unavailable -> allowed
            self._robots_allow_all[domain] = True
            return
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(resp.text.splitlines())
        self._robots[domain] = rp
        delay = rp.crawl_delay(self.user_agent) or rp.crawl_delay("*")
        if delay:
            self._crawl_delay[domain] = float(delay)
            log(lg, logging.INFO, "honouring robots crawl-delay", domain=domain, delay_s=delay)

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        self._load_robots(p.scheme, p.netloc)
        if p.netloc in self._robots_allow_all:
            return self._robots_allow_all[p.netloc]
        rp = self._robots[p.netloc]
        return rp.can_fetch(self.user_agent, url)

    # ---------- cache ----------
    def _store(self, url: str, resp: Response) -> str:
        h = hashlib.sha256(resp.text.encode()).hexdigest()
        ext = "json" if "json" in resp.headers.get("content-type", "") else "html"
        path = self.raw_dir / f"{h[:2]}/{h}.{ext}"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(resp.text)
        now = utcnow()
        self.conn.execute(
            """INSERT INTO raw_responses(url,status_code,content_type,content_hash,body_path,first_fetched,last_fetched)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(url,content_hash) DO UPDATE SET last_fetched=excluded.last_fetched,
               fetch_count=fetch_count+1""",
            (url, resp.status, resp.headers.get("content-type"), h, str(path), now, now),
        )
        self.conn.commit()
        return h

    def _from_cache(self, url: str) -> FetchResult:
        row = self.conn.execute(
            "SELECT * FROM raw_responses WHERE url=? ORDER BY last_fetched DESC, id DESC LIMIT 1", (url,)
        ).fetchone()
        if not row or not Path(row["body_path"]).exists():
            return self._fail(url, "not_in_cache", None, 0)
        return FetchResult(url, True, row["status_code"], row["content_type"], Path(row["body_path"]).read_text(),
                           row["content_hash"], from_cache=True)

    def _fail(self, url: str, reason: str, status: int | None, attempts: int) -> FetchResult:
        log(lg, logging.WARNING, "fetch failed", url=url, reason=reason, status=status, attempts=attempts)
        self.conn.execute(
            "INSERT INTO fetch_failures(run_id,url,reason,status_code,attempts,failed_at) VALUES (?,?,?,?,?,?)",
            (self.run_id, url, reason[:500], status, attempts, utcnow()),
        )
        self.conn.commit()
        return FetchResult(url, False, status, error=reason, attempts=attempts)

    # ---------- main entry ----------
    def get(self, url: str, expect: str | None = None) -> FetchResult:
        """Fetch url. expect='json'|'html' validates the Content-Type (mismatch is retried, then fails)."""
        if self.offline:
            return self._from_cache(url)
        if not self.allowed(url):
            reason = self._robots_reason.get(urlparse(url).netloc, "disallowed_by_robots_txt")
            return self._fail(url, reason, None, 0)
        domain = urlparse(url).netloc
        headers = {"User-Agent": self.user_agent,
                   "Accept": "application/json" if expect == "json" else "text/html,*/*"}
        last_reason, last_status = "unknown", None
        for attempt in range(1, self.max_retries + 1):
            self._wait_turn(domain)
            retry_after = None
            try:
                resp = self.transport.send(url, headers, self.timeout_s)
            except Exception as e:
                last_reason, last_status = f"network_error: {e!r}", None
            else:
                ctype = resp.headers.get("content-type", "").lower()
                if resp.status == 200 and (expect is None or expect in ctype):
                    h = self._store(url, resp)
                    log(lg, logging.INFO, "fetched", url=url, status=200, attempt=attempt, bytes=len(resp.text))
                    return FetchResult(url, True, 200, ctype, resp.text, h, attempts=attempt)
                last_status = resp.status
                if resp.status == 200:
                    last_reason = f"content_type_mismatch: expected {expect}, got {ctype or 'none'}"
                elif resp.status in RETRYABLE_STATUS:
                    last_reason = f"http_{resp.status}"
                    ra = resp.headers.get("retry-after")
                    retry_after = float(ra) if ra and ra.isdigit() else None
                else:
                    return self._fail(url, f"http_{resp.status}", resp.status, attempt)
            if attempt < self.max_retries:
                backoff = retry_after if retry_after is not None else min(60.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
                log(lg, logging.INFO, "retrying", url=url, reason=last_reason, attempt=attempt, backoff_s=round(backoff, 2))
                self.sleep(min(backoff, 60.0))
        return self._fail(url, last_reason, last_status, self.max_retries)
