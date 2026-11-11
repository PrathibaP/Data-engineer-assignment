"""Source C: Hacker News "Ask HN: Who is hiring?" — parsed from HTML (no Firebase API).

1. Discover the current month's thread from https://news.ycombinator.com/submitted?id=whoishiring
   (or use HN_THREAD_ID if robots.txt or the page blocks discovery).
2. Fetch item?id=<thread>, follow the "More" (a.morelink) pagination.
3. Keep top-level comments only (replies are discussion, not postings), skip deleted/flagged ones.
4. Pre-filter comments that mention a data/ML role and are not clearly non-US-only.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from pipeline.logger import get_logger, log
from pipeline.models import RawPosting
from pipeline.transform.normalize import html_to_text, looks_us
from pipeline.ingest.http import PoliteClient
from pipeline.ingest import SourceResult

lg = get_logger("hackernews")
BASE = "https://news.ycombinator.com/"
SUBMITTED = BASE + "submitted?id=whoishiring"
THREAD_RE = re.compile(r"Ask HN: Who is hiring\?\s*\((\w+)\s+(\d{4})\)", re.I)
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}
ROLE_HINT = re.compile(
    r"\b(data engineer|data engineering|data platform|data infrastructure|analytics engineer|machine learning|"
    r"ml engineer|mle|ml ops|mlops|ml platform|ml infra|ai engineer|platform engineer|etl|data pipelines?)\b", re.I)
MAX_PAGES = 25


def find_thread(html_text: str, today: date) -> tuple[str | None, str | None, str | None]:
    """Return (thread_id, title, next_page_url). Prefers the thread for today's month."""
    soup = BeautifulSoup(html_text, "lxml")
    candidates = []
    for a in soup.select("span.titleline > a, a.storylink"):
        m = THREAD_RE.search(a.get_text(" ", strip=True))
        if not m:
            continue
        tid = re.search(r"id=(\d+)", a.get("href", ""))
        if tid:
            candidates.append((MONTHS.get(m.group(1).lower(), 0), int(m.group(2)), tid.group(1), a.get_text(strip=True)))
    more = soup.select_one("a.morelink")
    next_url = urljoin(BASE, more["href"]) if more else None
    for month, year, tid, title in candidates:
        if month == today.month and year == today.year:
            return tid, title, next_url
    return None, (candidates[0][3] if candidates else None), next_url


def parse_item_page(html_text: str) -> tuple[list[dict], str | None]:
    """Parse one thread page -> (top-level comments, next page url)."""
    soup = BeautifulSoup(html_text, "lxml")
    out = []
    for tr in soup.select("tr.athing.comtr"):
        ind = tr.select_one("td.ind")
        indent = 0
        if ind is not None:
            if ind.has_attr("indent"):
                indent = int(ind["indent"] or 0)
            else:
                img = ind.find("img")
                indent = int(img.get("width", 0)) // 40 if img else 0
        if indent != 0:
            continue
        body = tr.select_one("div.commtext")
        if body is None:  # [deleted] / [flagged] / [dead]
            continue
        for r in body.select("div.reply"):
            r.decompose()
        links = [a.get("href") for a in body.find_all("a") if a.get("href", "").startswith("http")]
        user = tr.select_one("a.hnuser")
        age = tr.select_one("span.age")
        posted = None
        if age is not None and age.get("title"):
            try:
                posted = datetime.fromisoformat(age["title"].split()[0]).date()
            except ValueError:
                posted = None
        out.append({"id": tr.get("id"), "user": user.get_text() if user else None, "posted": posted,
                    "text": html_to_text(str(body)), "links": links})
    more = soup.select_one("a.morelink")
    return out, (urljoin(BASE, more["href"]) if more else None)


def _company_guess(first_line: str) -> str | None:
    head = re.split(r"\s[|•\-–—]\s|\|", first_line, maxsplit=1)[0].strip()
    head = re.sub(r"\(.*?\)", "", head).strip()
    return head if 0 < len(head) <= 60 else None


def prefilter(comment: dict) -> bool:
    text = comment["text"]
    if not ROLE_HINT.search(text):
        return False
    head = "\n".join(text.splitlines()[:2])[:400]
    return looks_us(head) is not False


def scrape(client: PoliteClient, today: date, thread_id: str | None = None) -> SourceResult:
    title = None
    if not thread_id:
        url, pages = SUBMITTED, 0
        while url and pages < 3 and not thread_id:
            r = client.get(url, expect="html")
            pages += 1
            if not r.ok:
                msg = f"thread discovery failed ({r.error}); set HN_THREAD_ID to the thread's item id"
                return SourceResult("hackernews", "?", ok=False, complete=False, errors=[msg])
            thread_id, title, url = find_thread(r.text, today)
        if not thread_id:
            return SourceResult("hackernews", "?", ok=False, complete=False,
                                errors=[f"no 'Who is hiring?' thread for {today:%B %Y} (latest seen: {title})"])
    res = SourceResult("hackernews", thread_id, ok=True, complete=True, note=title)
    url, seen, page = BASE + f"item?id={thread_id}", set(), 0
    while url and url not in seen and page < MAX_PAGES:
        seen.add(url)
        page += 1
        r = client.get(url, expect="html")
        if not r.ok:
            res.complete = False  # partial thread: keep what we have, but don't infer closures
            res.errors.append(f"page {page} failed: {r.error}")
            break
        comments, url = parse_item_page(r.text)
        res.total_seen += len(comments)
        for c in comments:
            if not prefilter(c):
                continue
            first = c["text"].splitlines()[0] if c["text"] else ""
            res.postings.append(RawPosting(
                source="hackernews", board=thread_id, source_job_id=c["id"],
                url=BASE + f"item?id={c['id']}", company_hint=_company_guess(first),
                text=c["text"], posted_date_hint=c["posted"], outbound_urls=c["links"],
                structured={"hn_user": c["user"]},
            ))
    if page >= MAX_PAGES and url:
        res.complete = False
        res.errors.append("page limit reached")
    log(lg, logging.INFO, "thread parsed", thread=thread_id, pages=page, comments=res.total_seen,
        kept=len(res.postings), complete=res.complete)
    return res
