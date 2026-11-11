"""Cross-source duplicate matching (pure functions, no DB) — easy to unit-test.

Decision order for an incoming source posting:
 1. Same source_key seen before                       -> same posting (method=source_key, score 1.0)
 2. HN comment links to an ATS URL / job id we hold   -> same posting (method=url_link, score 1.0)
 3. Blocking: candidates must share normalised company; two *different* job ids on the same board are
    never merged (a board lists each requisition once, so same title in two cities = two jobs).
 4. Weighted score = 0.55 title + 0.15 seniority + 0.15 location + 0.15 skills overlap, with hard caps
    for conflicting seniority (<= 0.60) or conflicting cities (<= 0.50).
    >= AUTO_MERGE -> merge;  REVIEW..AUTO_MERGE -> keep separate + write to dedup_review;  else new.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from pipeline.transform.normalize import normalize_title, title_core

AUTO_MERGE = 0.85
REVIEW = 0.65


@dataclass
class Candidate:
    source_key: str
    source: str
    board: str
    source_job_id: str
    company_key: str
    title: str
    seniority: str | None = None
    city: str | None = None
    country: str | None = None
    remote_policy: str | None = None
    skills: list[str] = field(default_factory=list)
    url: str = ""
    outbound_urls: list[str] = field(default_factory=list)


@dataclass
class Match:
    posting_id: str | None
    score: float
    method: str  # source_key | url_link | fuzzy | new
    reasons: list[str] = field(default_factory=list)


def _qualifier(title: str) -> str:
    parts = re.split(r"\s[-,|(/]\s?|,", (title or "").replace("–", "-").replace("—", "-"), maxsplit=1)
    return normalize_title(parts[1]) if len(parts) > 1 else ""


def title_similarity(a: str, b: str) -> float:
    ca, cb = title_core(a), title_core(b)
    if ca and ca == cb:
        qa, qb = _qualifier(a), _qualifier(b)
        if qa and qb and qa != qb and SequenceMatcher(None, qa, qb).ratio() < 0.8:
            return 0.70  # "Data Engineer, Ads" vs "Data Engineer, Payments": same family, different team
        return 1.0 if qa == qb else 0.90
    return max(SequenceMatcher(None, ca, cb).ratio(), SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()) * 0.9


def links_to(c: Candidate, existing: Candidate) -> bool:
    """HN comments often link straight to the ATS posting; that is the strongest possible signal."""
    def canon(u: str) -> str:  # drop scheme, www, query (?gh_src=...), fragment, trailing slash
        u = re.sub(r"^https?://(www\.)?", "", u.lower())
        return re.split(r"[?#]", u, maxsplit=1)[0].rstrip("/")

    for u in c.outbound_urls:
        ul = u.lower()
        if existing.url and canon(existing.url) == canon(u):
            return True
        if existing.source_job_id and len(existing.source_job_id) >= 6 and existing.source_job_id.lower() in ul \
                and (existing.board.lower() in ul or existing.source in ul):
            return True
    return False


def score_pair(c: Candidate, e: Candidate) -> tuple[float, list[str]]:
    if not c.company_key or c.company_key != e.company_key:
        return 0.0, ["different company"]
    if c.source == e.source and c.board == e.board and c.source_job_id != e.source_job_id:
        return 0.0, ["distinct job ids on the same board"]
    reasons = []
    t = title_similarity(c.title, e.title)
    reasons.append(f"title={t:.2f}")
    cap = 1.0
    if c.seniority and e.seniority:
        s = 1.0 if c.seniority == e.seniority else 0.0
        if s == 0.0:
            cap = min(cap, 0.60)
            reasons.append("seniority conflict")
    else:
        s = 0.5
    cc, ec = (c.city or "").lower(), (e.city or "").lower()
    if cc and ec:
        if cc == ec:
            loc = 1.0
        elif "remote" in (c.remote_policy or "") + (e.remote_policy or ""):
            loc = 0.6
        else:
            loc = 0.0
            cap = min(cap, 0.50)
            reasons.append("city conflict")
    else:
        loc = 0.6
    if c.skills and e.skills:
        a, b = {x.lower() for x in c.skills}, {x.lower() for x in e.skills}
        sk = len(a & b) / len(a | b)
    else:
        sk = 0.5
    score = min(cap, 0.55 * t + 0.15 * s + 0.15 * loc + 0.15 * sk)
    reasons.append(f"seniority={s:.1f} location={loc:.1f} skills={sk:.2f}")
    return round(score, 3), reasons


def best_match(c: Candidate, existing: dict[str, list[Candidate]]) -> Match:
    """existing: posting_id -> the source candidates already attached to it."""
    for pid, members in existing.items():
        if any(m.source_key == c.source_key for m in members):
            return Match(pid, 1.0, "source_key")
    for pid, members in existing.items():
        if any(links_to(c, m) or links_to(m, c) for m in members) and \
                all(m.company_key == c.company_key for m in members):
            return Match(pid, 1.0, "url_link", ["outbound link to ATS posting"])
    best = Match(None, 0.0, "new")
    for pid, members in existing.items():
        # a posting can be matched only if NO member blocks it (same-board different-id)
        scores = [score_pair(c, m) for m in members]
        if any(sc == 0.0 for sc, _ in scores):
            continue
        sc, why = max(scores, key=lambda x: x[0])
        if sc > best.score:
            best = Match(pid, sc, "fuzzy", why)
    return best
