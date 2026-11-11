"""SQLite storage: schema, connection helper and small query helpers.

Tables
------
runs              one row per pipeline execution (counts, status, timings)
raw_responses     every HTTP body fetched (dedup by url + content hash); body stored on disk in data/raw
fetch_failures    URL-level failures with reason, never fatal to a run
source_postings   one row per posting *as seen on one source* (the source-to-posting mapping)
postings          canonical, deduplicated records (the target schema)
evidence          field -> source snippet for each canonical posting
change_history    field-level changes to canonical postings (append-only)
dedup_review      uncertain cross-source matches that were NOT merged, for human review
extraction_cache  LLM output keyed by hash(prompt version, model, input) -> no repeat LLM spend
llm_calls         token usage + cost per call
eval_labels       hand-labelled ground truth loaded from data/evaluation/labels.csv
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',
    stats_json    TEXT
);

CREATE TABLE IF NOT EXISTS raw_responses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL,
    status_code   INTEGER,
    content_type  TEXT,
    content_hash  TEXT NOT NULL,
    body_path     TEXT NOT NULL,
    first_fetched TEXT NOT NULL,
    last_fetched  TEXT NOT NULL,
    fetch_count   INTEGER NOT NULL DEFAULT 1,
    UNIQUE (url, content_hash)
);
CREATE INDEX IF NOT EXISTS ix_raw_url ON raw_responses(url, last_fetched);

CREATE TABLE IF NOT EXISTS fetch_failures (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT,
    url           TEXT NOT NULL,
    reason        TEXT NOT NULL,
    status_code   INTEGER,
    attempts      INTEGER,
    failed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS postings (
    posting_id        TEXT PRIMARY KEY,
    company           TEXT NOT NULL,
    company_key       TEXT NOT NULL,
    title             TEXT NOT NULL,
    title_key         TEXT NOT NULL,
    seniority         TEXT CHECK (seniority IN ('junior','mid','senior','staff_plus') OR seniority IS NULL),
    city              TEXT,
    country           TEXT,
    remote_policy     TEXT CHECK (remote_policy IN ('onsite','hybrid','remote') OR remote_policy IS NULL),
    salary_min        REAL,
    salary_max        REAL,
    currency          TEXT,
    employment_type   TEXT CHECK (employment_type IN ('full_time','contract','intern') OR employment_type IS NULL),
    skills_json       TEXT NOT NULL DEFAULT '[]',
    visa_sponsorship  INTEGER,
    posted_date       TEXT,
    status            TEXT NOT NULL CHECK (status IN ('open','closed')),
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_postings_company ON postings(company_key, title_key);

CREATE TABLE IF NOT EXISTS source_postings (
    source_key      TEXT PRIMARY KEY,           -- e.g. greenhouse:stripe:123, hn:4567:senior-data-engineer
    posting_id      TEXT NOT NULL REFERENCES postings(posting_id),
    source          TEXT NOT NULL,              -- greenhouse | lever | hackernews
    board           TEXT NOT NULL,              -- greenhouse token / lever site / hn thread id
    source_job_id   TEXT NOT NULL,
    url             TEXT NOT NULL,
    title_raw       TEXT,
    location_raw    TEXT,
    content_hash    TEXT NOT NULL,
    extracted_json  TEXT,
    match_method    TEXT NOT NULL,              -- new | source_key | url_link | fuzzy
    match_score     REAL,
    active          INTEGER NOT NULL DEFAULT 1,
    last_run_id     TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sp_posting ON source_postings(posting_id);
CREATE INDEX IF NOT EXISTS ix_sp_board ON source_postings(source, board, active);

CREATE TABLE IF NOT EXISTS evidence (
    posting_id   TEXT NOT NULL REFERENCES postings(posting_id),
    field        TEXT NOT NULL,
    snippet      TEXT NOT NULL,
    source_key   TEXT NOT NULL,
    PRIMARY KEY (posting_id, field)
);

CREATE TABLE IF NOT EXISTS change_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_id   TEXT NOT NULL REFERENCES postings(posting_id),
    run_id       TEXT NOT NULL,
    field        TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    changed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ch_posting ON change_history(posting_id, changed_at);

CREATE TABLE IF NOT EXISTS dedup_review (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key       TEXT NOT NULL,
    candidate_posting_id TEXT NOT NULL,
    score            REAL NOT NULL,
    reasons          TEXT NOT NULL,
    decision         TEXT NOT NULL DEFAULT 'pending',   -- pending | merge | keep_separate
    created_at       TEXT NOT NULL,
    UNIQUE (source_key, candidate_posting_id)
);

CREATE TABLE IF NOT EXISTS extraction_cache (
    cache_key     TEXT PRIMARY KEY,
    model         TEXT NOT NULL,
    output_json   TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT,
    source_key     TEXT,
    model          TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    cost_usd       REAL NOT NULL,
    ok             INTEGER NOT NULL,
    error          TEXT,
    called_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_labels (
    source_key    TEXT PRIMARY KEY,
    labels_json   TEXT NOT NULL,
    loaded_at     TEXT NOT NULL
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def dumps(v) -> str:
    return json.dumps(v, sort_keys=True, default=str)
