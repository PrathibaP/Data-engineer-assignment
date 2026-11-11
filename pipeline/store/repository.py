"""Idempotent upserts, canonical record assembly, change history and closed-posting detection.

* source_postings is keyed by a stable source_key, so re-seeing a posting updates last_seen instead of
  inserting. The canonical posting_id is derived from the first source_key that created it and is
  persisted, so it never changes across runs.
* The canonical record is recomputed from all of its source rows (ATS sources outrank HN for every field;
  HN only fills fields the ATS left null). Differences against the stored record become change_history
  rows; an unchanged re-run therefore writes nothing to change_history.
* A source row becomes inactive only when its board was fetched *completely* in this run and the row was
  not seen. A failed or partial fetch never closes anything. A canonical posting is closed when all of its
  source rows are inactive, and re-opened (with history) if one reappears.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict

from pipeline.store.db import dumps
from pipeline.resolve.dedup import AUTO_MERGE, REVIEW, Candidate, best_match

TRACKED = ["company", "title", "seniority", "city", "country", "remote_policy", "salary_min", "salary_max",
           "currency", "employment_type", "visa_sponsorship", "posted_date", "status"]
FIELDS = [f for f in TRACKED if f != "status"] + ["skills"]
SOURCE_RANK = {"greenhouse": 0, "lever": 0, "hackernews": 1}


def make_posting_id(source_key: str) -> str:
    return "pst_" + hashlib.sha1(source_key.encode()).hexdigest()[:16]


def _cand(rec: dict) -> Candidate:
    return Candidate(**{k: rec.get(k) for k in Candidate.__dataclass_fields__ if k in rec and rec.get(k) is not None})


class Store:
    def __init__(self, conn: sqlite3.Connection, run_id: str, now: str):
        self.conn, self.run_id, self.now = conn, run_id, now
        self.stats = {"new_postings": 0, "merged_sources": 0, "review_queued": 0, "updated_sources": 0,
                      "changes": 0, "closed": 0, "reopened": 0}

    # ---------------------------------------------------------------- matching
    def _existing(self, company_key: str) -> dict[str, list[Candidate]]:
        rows = self.conn.execute(
            "SELECT sp.posting_id, sp.extracted_json FROM source_postings sp JOIN postings p USING(posting_id) "
            "WHERE p.company_key=?", (company_key,)).fetchall()
        out: dict[str, list[Candidate]] = {}
        for r in rows:
            out.setdefault(r["posting_id"], []).append(_cand(json.loads(r["extracted_json"])))
        return out

    def upsert(self, rec: dict) -> tuple[str, str]:
        """rec = one normalised role as seen on one source. Returns (posting_id, action)."""
        row = self.conn.execute("SELECT posting_id, content_hash, extracted_json FROM source_postings WHERE source_key=?",
                                (rec["source_key"],)).fetchone()
        if row:
            pid = row["posting_id"]
            self.conn.execute(
                "UPDATE source_postings SET last_seen=?, last_run_id=?, active=1, content_hash=?, url=?, title_raw=?,"
                " location_raw=?, extracted_json=? WHERE source_key=?",
                (self.now, self.run_id, rec["content_hash"], rec["url"], rec.get("title_raw"), rec.get("location_raw"),
                 dumps(rec), rec["source_key"]))
            self.stats["updated_sources"] += 1
            self.recompute(pid)
            return pid, "seen"
        m = best_match(_cand(rec), self._existing(rec["company_key"]))
        if m.posting_id and (m.method in ("source_key", "url_link") or m.score >= AUTO_MERGE):
            pid, action = m.posting_id, "merged"
            self.stats["merged_sources"] += 1
        else:
            pid, action = make_posting_id(rec["source_key"]), "new"
            self._insert_posting(pid, rec)
            self.stats["new_postings"] += 1
            if m.posting_id and m.score >= REVIEW:
                self.conn.execute(
                    "INSERT OR IGNORE INTO dedup_review(source_key,candidate_posting_id,score,reasons,created_at)"
                    " VALUES (?,?,?,?,?)", (rec["source_key"], m.posting_id, m.score, "; ".join(m.reasons), self.now))
                self.stats["review_queued"] += 1
        self.conn.execute(
            "INSERT INTO source_postings(source_key,posting_id,source,board,source_job_id,url,title_raw,location_raw,"
            "content_hash,extracted_json,match_method,match_score,active,last_run_id,first_seen,last_seen)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
            (rec["source_key"], pid, rec["source"], rec["board"], rec["source_job_id"], rec["url"], rec.get("title_raw"),
             rec.get("location_raw"), rec["content_hash"], dumps(rec), m.method if action == "merged" else "new",
             m.score if m.posting_id else None, self.run_id, self.now, self.now))
        self.recompute(pid, initial=(action == "new"))
        return pid, action

    def _insert_posting(self, pid: str, rec: dict) -> None:
        self.conn.execute(
            "INSERT INTO postings(posting_id,company,company_key,title,title_key,status,first_seen,last_seen,updated_at)"
            " VALUES (?,?,?,?,?,'open',?,?,?)",
            (pid, rec["company"], rec["company_key"], rec["title"], rec["title_key"], self.now, self.now, self.now))

    # ---------------------------------------------------------------- canonical assembly
    def recompute(self, pid: str, initial: bool = False) -> list[tuple[str, object, object]]:
        srcs = self.conn.execute("SELECT * FROM source_postings WHERE posting_id=?", (pid,)).fetchall()
        srcs = sorted(srcs, key=lambda r: (SOURCE_RANK.get(r["source"], 9), r["first_seen"], r["source_key"]))
        recs = [(r, json.loads(r["extracted_json"])) for r in srcs]
        active = [x for x in recs if x[0]["active"]]
        pool = active or recs  # a closed posting keeps its last known values
        new, evidence = {}, {}
        for f in FIELDS:
            for r, rec in pool:
                v = rec.get(f)
                if v not in (None, [], ""):
                    new[f] = v
                    if rec.get("evidence", {}).get(f):
                        evidence[f] = (rec["evidence"][f], r["source_key"])
                    break
            else:
                new[f] = [] if f == "skills" else None
        new["status"] = "open" if active else "closed"
        cur = self.conn.execute("SELECT * FROM postings WHERE posting_id=?", (pid,)).fetchone()
        changes = []
        # enrichment of a posting created earlier in this same run is not a "change"
        if not initial and cur["first_seen"] != self.now:
            for f in TRACKED:
                old = cur[f]
                nv = new[f]
                if f == "visa_sponsorship" and nv is not None:
                    nv = int(bool(nv))
                if (old if old is not None else None) != (nv if nv is not None else None) and str(old) != str(nv):
                    changes.append((f, old, nv))
        from pipeline.transform.normalize import company_key, title_key

        self.conn.execute(
            "UPDATE postings SET company=?,company_key=?,title=?,title_key=?,seniority=?,city=?,country=?,remote_policy=?,"
            "salary_min=?,salary_max=?,currency=?,employment_type=?,skills_json=?,visa_sponsorship=?,posted_date=?,"
            "status=?,first_seen=?,last_seen=?,updated_at=CASE WHEN ? THEN ? ELSE updated_at END WHERE posting_id=?",
            (new["company"], company_key(new["company"]), new["title"], title_key(new["title"]), new["seniority"],
             new["city"], new["country"], new["remote_policy"], new["salary_min"], new["salary_max"], new["currency"],
             new["employment_type"], json.dumps(new["skills"]),
             None if new["visa_sponsorship"] is None else int(bool(new["visa_sponsorship"])),
             new["posted_date"], new["status"], min(r["first_seen"] for r, _ in recs),
             max(r["last_seen"] for r, _ in recs), int(bool(changes) or initial), self.now, pid))
        self.conn.execute("DELETE FROM evidence WHERE posting_id=?", (pid,))
        self.conn.executemany("INSERT INTO evidence(posting_id,field,snippet,source_key) VALUES (?,?,?,?)",
                              [(pid, f, s, sk) for f, (s, sk) in evidence.items()])
        for f, old, nv in changes:
            self.conn.execute(
                "INSERT INTO change_history(posting_id,run_id,field,old_value,new_value,changed_at) VALUES (?,?,?,?,?,?)",
                (pid, self.run_id, f, None if old is None else str(old), None if nv is None else str(nv), self.now))
            if f == "status":
                self.stats["closed" if nv == "closed" else "reopened"] += 1
        self.stats["changes"] += len(changes)
        return changes

    # ---------------------------------------------------------------- closure
    def close_missing(self, source: str, board: str) -> int:
        """Call ONLY for boards fetched completely this run."""
        rows = self.conn.execute(
            "SELECT DISTINCT posting_id FROM source_postings WHERE source=? AND board=? AND active=1 "
            "AND (last_run_id IS NULL OR last_run_id != ?)", (source, board, self.run_id)).fetchall()
        self.conn.execute(
            "UPDATE source_postings SET active=0 WHERE source=? AND board=? AND active=1 "
            "AND (last_run_id IS NULL OR last_run_id != ?)", (source, board, self.run_id))
        for r in rows:
            self.recompute(r["posting_id"])
        return len(rows)


def candidate_dict(c: Candidate) -> dict:
    return asdict(c)
