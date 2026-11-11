PY ?= python3

.PHONY: setup run offline demo test label evaluate export clean

setup:            ## create venv and install dependencies
	$(PY) -m venv .venv && .venv/bin/pip install -r requirements.txt && cp -n .env.example .env || true

run:              ## live run: scrape -> extract -> dedup -> store -> export
	$(PY) -m pipeline run

offline:          ## re-process from the raw response cache, no network
	$(PY) -m pipeline run --offline

demo:             ## offline demo on SYNTHETIC fixtures: v1, v1 again (idempotent), v2 (changes)
	rm -rf /tmp/jobs-demo
	DATA_DIR=/tmp/jobs-demo LLM_PROVIDER=mock $(PY) -m pipeline run --fixtures tests/fixtures/live_v1 --today 2026-11-11
	DATA_DIR=/tmp/jobs-demo LLM_PROVIDER=mock $(PY) -m pipeline run --fixtures tests/fixtures/live_v1 --today 2026-11-11
	DATA_DIR=/tmp/jobs-demo LLM_PROVIDER=mock $(PY) -m pipeline run --fixtures tests/fixtures/live_v2 --today 2026-11-12
	@echo "demo outputs in /tmp/jobs-demo/exports"

test:
	$(PY) -m pytest -q

label:            ## write data/evaluation/labels_template.csv (20 postings, >=10 HN)
	$(PY) -m pipeline label-template

evaluate:         ## score data/evaluation/labels.csv against the extractions
	$(PY) -m pipeline evaluate

export:
	$(PY) -m pipeline export

clean:
	rm -rf data/raw/?? data/*.db* logs/*.jsonl .pytest_cache
