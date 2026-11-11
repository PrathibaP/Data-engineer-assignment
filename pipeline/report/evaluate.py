"""Hand-labelled evaluation: field-level extraction accuracy + LLM cost per posting.

Workflow
  1. python -m pipeline label-template   -> data/evaluation/labels_template.csv (20 rows, >=10 from HN)
  2. Copy it to data/evaluation/labels.csv; open each URL and fill the truth_* columns by hand.
     Empty cell = the posting does not state it (truth is null). '?' = cannot judge -> excluded.
     Fill truth_* from the posting ONLY, without looking at pred_* (blind labelling).
  3. python -m pipeline evaluate         -> evaluation_results.csv, evaluation_report.json / .md

Outcomes per field: correct | correct_null (both null) | incorrect | missing (truth set, pred null)
| spurious (pred set, truth null = hallucination) | not_evaluable.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pipeline.transform.normalize import company_key, normalize_title

FIELDS = ["company", "title", "seniority", "city", "country", "remote_policy", "salary_min", "salary_max",
          "currency", "employment_type", "skills", "visa_sponsorship", "posted_date"]
REPORT_GROUPS = {
    "company": ["company"], "title": ["title"], "seniority": ["seniority"], "location": ["city", "country"],
    "remote_policy": ["remote_policy"], "salary": ["salary_min", "salary_max", "currency"],
    "employment_type": ["employment_type"], "skills": ["skills"], "visa_sponsorship": ["visa_sponsorship"],
    "posted_date": ["posted_date"],
}


def write_label_template(conn: sqlite3.Connection, eval_dir: Path, n: int = 20, min_hn: int = 10) -> Path:
    rows = conn.execute("SELECT source_key, source, url, extracted_json FROM source_postings").fetchall()
    rows = sorted(rows, key=lambda r: hashlib.md5(r["source_key"].encode()).hexdigest())  # deterministic sample
    hn = [r for r in rows if r["source"] == "hackernews"][:max(min_hn, n - len([r for r in rows if r["source"] != "hackernews"]))]
    ats = [r for r in rows if r["source"] != "hackernews"][: n - len(hn)]
    sample = (hn + ats)[:n]
    path = Path(eval_dir) / "labels_template.csv"
    cols = ["source_key", "source", "url"] + [f"pred_{f}" for f in FIELDS] + [f"truth_{f}" for f in FIELDS] + ["notes"]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in sample:
            rec = json.loads(r["extracted_json"])
            row = {"source_key": r["source_key"], "source": r["source"], "url": r["url"]}
            for f in FIELDS:
                v = rec.get(f)
                row[f"pred_{f}"] = "; ".join(v) if isinstance(v, list) else ("" if v is None else v)
                row[f"truth_{f}"] = ""
            w.writerow(row)
    return path


def _norm(field: str, v: str):
    v = (v or "").strip()
    if v == "" or v.lower() in {"null", "none", "n/a"}:
        return None
    if field in ("salary_min", "salary_max"):
        return float(v.replace(",", "").replace("$", ""))
    if field == "visa_sponsorship":
        return v.lower() in {"true", "yes", "1", "y"}
    if field == "skills":
        return {s.strip().lower() for s in v.replace(",", ";").split(";") if s.strip()}
    if field == "company":
        return company_key(v)
    if field == "title":
        return normalize_title(v)
    if field == "city":
        return v.lower()
    return v.lower()


def _compare(field: str, pred, truth) -> str:
    if field == "skills":
        pred, truth = pred or set(), truth or set()
        if not pred and not truth:
            return "correct_null"
        if not truth:
            return "spurious"
        if not pred:
            return "missing"
        f1 = 2 * len(pred & truth) / (len(pred) + len(truth))
        return "correct" if f1 >= 0.8 else "incorrect"
    if pred is None and truth is None:
        return "correct_null"
    if truth is None:
        return "spurious"
    if pred is None:
        return "missing"
    if field in ("salary_min", "salary_max"):
        return "correct" if abs(pred - truth) <= 0.01 * max(truth, 1) else "incorrect"
    return "correct" if pred == truth else "incorrect"


def evaluate(conn: sqlite3.Connection, eval_dir: Path) -> dict:
    eval_dir = Path(eval_dir)
    labels_path = eval_dir / "labels.csv"
    if not labels_path.exists():
        raise SystemExit(f"{labels_path} not found: run label-template, copy it to labels.csv and fill truth_* columns")
    with labels_path.open() as fh:
        labels = list(csv.DictReader(fh))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    detail, per_field = [], {f: {} for f in FIELDS}
    skill_f1 = []
    for lab in labels:
        conn.execute("INSERT OR REPLACE INTO eval_labels VALUES (?,?,?)",
                     (lab["source_key"], json.dumps({f: lab.get(f"truth_{f}") for f in FIELDS}), now))
        row = conn.execute("SELECT extracted_json FROM source_postings WHERE source_key=?", (lab["source_key"],)).fetchone()
        pred_rec = json.loads(row["extracted_json"]) if row else {}
        for f in FIELDS:
            raw_truth = (lab.get(f"truth_{f}") or "").strip()
            if raw_truth == "?":
                outcome, p, t = "not_evaluable", None, None
            else:
                pv = pred_rec.get(f)
                p = _norm(f, "; ".join(pv) if isinstance(pv, list) else ("" if pv is None else str(pv)))
                t = _norm(f, raw_truth)
                outcome = _compare(f, p, t)
                if f == "skills" and p and t:
                    skill_f1.append(2 * len(p & t) / (len(p) + len(t)))
            per_field[f][outcome] = per_field[f].get(outcome, 0) + 1
            detail.append({"source_key": lab["source_key"], "source": lab.get("source"), "field": f,
                           "predicted": pred_rec.get(f), "truth": raw_truth, "outcome": outcome})
    conn.commit()

    def summarize(counts: dict) -> dict:
        evaluable = sum(v for k, v in counts.items() if k != "not_evaluable")
        good = counts.get("correct", 0) + counts.get("correct_null", 0)
        stated = sum(counts.get(k, 0) for k in ("correct", "incorrect", "missing"))
        return {**counts, "evaluable": evaluable,
                "accuracy": round(good / evaluable, 3) if evaluable else None,
                "accuracy_when_stated": round(counts.get("correct", 0) / stated, 3) if stated else None}

    fields = {f: summarize(c) for f, c in per_field.items()}
    groups = {}
    for g, fs in REPORT_GROUPS.items():
        merged: dict = {}
        for f in fs:
            for k, v in per_field[f].items():
                merged[k] = merged.get(k, 0) + v
        groups[g] = summarize(merged)
    total = {}
    for c in per_field.values():
        for k, v in c.items():
            total[k] = total.get(k, 0) + v
    cost = conn.execute("SELECT COALESCE(SUM(cost_usd),0) c, COUNT(DISTINCT source_key) n, COUNT(*) calls, "
                        "COALESCE(SUM(input_tokens),0) ti, COALESCE(SUM(output_tokens),0) tout, "
                        "GROUP_CONCAT(DISTINCT model) models FROM llm_calls").fetchone()
    summary = {
        "n_labelled": len(labels),
        "n_hackernews": sum(1 for x in labels if x.get("source") == "hackernews"),
        "overall": summarize(total),
        "by_group": groups,
        "skills_mean_f1": round(sum(skill_f1) / len(skill_f1), 3) if skill_f1 else None,
        "llm": {"models": cost["models"], "total_cost_usd": round(cost["c"], 6), "calls": cost["calls"],
                "postings_extracted": cost["n"], "input_tokens": cost["ti"], "output_tokens": cost["tout"],
                "cost_per_posting_usd": round(cost["c"] / cost["n"], 6) if cost["n"] else None},
        "not_stated_in_source": {f: per_field[f].get("correct_null", 0) + per_field[f].get("spurious", 0) for f in FIELDS},
    }
    if cost["models"] and "mock" in cost["models"]:
        summary["warning"] = "Predictions come from the MOCK heuristic provider, not an LLM. Not a real result."
    with (eval_dir / "evaluation_results.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(detail[0].keys()) if detail else ["source_key"])
        w.writeheader()
        for d in detail:
            w.writerow({k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in d.items()})
    report = {"summary": summary, "fields": fields}
    (eval_dir / "evaluation_report.json").write_text(json.dumps(report, indent=2))
    (eval_dir / "evaluation_report.md").write_text(_markdown(summary, fields))
    return report


def _markdown(summary: dict, fields: dict) -> str:
    lines = ["# Extraction evaluation", ""]
    if summary.get("warning"):
        lines += [f"> **{summary['warning']}**", ""]
    lines += [f"Labelled postings: {summary['n_labelled']} ({summary['n_hackernews']} from Hacker News)", "",
              "| Field | Correct | Correct null | Incorrect | Missing | Spurious | Not evaluable | Accuracy | Acc. when stated |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for g, s in {**summary["by_group"], "OVERALL": summary["overall"]}.items():
        lines.append(f"| {g} | {s.get('correct', 0)} | {s.get('correct_null', 0)} | {s.get('incorrect', 0)} | "
                     f"{s.get('missing', 0)} | {s.get('spurious', 0)} | {s.get('not_evaluable', 0)} | "
                     f"{s['accuracy']} | {s['accuracy_when_stated']} |")
    llm = summary["llm"]
    lines += ["", f"Skills mean F1: {summary['skills_mean_f1']}", "",
              f"LLM: model(s) {llm['models']}, {llm['calls']} calls, {llm['postings_extracted']} postings, "
              f"total ${llm['total_cost_usd']}, **${llm['cost_per_posting_usd']} per posting** "
              f"({llm['input_tokens']} in / {llm['output_tokens']} out tokens)."]
    return "\n".join(lines) + "\n"
