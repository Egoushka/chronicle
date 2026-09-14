.PHONY: test lint migrate migrate-rename smoke fmt help doctor doctor-homelab

VENV  := .venv
TOOLS := $(VENV)/bin/pytest

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

# pytest, ruff, and numpy — which segment.py imports at module scope, so the
# suite cannot even be collected without it. Deliberately NOT `pip install -e
# .`: that pulls FlagEmbedding and sentence-transformers, gigabytes of torch,
# for 68 tests that touch neither a model nor a database.
$(TOOLS):
	python3 -m venv $(VENV)
	$(VENV)/bin/pip -q install pytest ruff numpy

test: $(TOOLS)  ## run the unit suite (no DB, no models needed)
	PYTHONPATH=. $(VENV)/bin/python -m pytest tests/ -q

lint: $(TOOLS)  ## ruff
	$(VENV)/bin/ruff check chronicle/ tests/

fmt: $(TOOLS)  ## ruff format
	$(VENV)/bin/ruff format chronicle/ tests/

# Globbed, not enumerated. The previous version named 001 and 002 explicitly,
# which meant a new migration file was silently ignored here AND in
# scripts/smoke.sh AND in CI — it would only ever have run via the compose
# initdb mount, i.e. on a fresh volume, i.e. never on the deployed box.
MIGRATIONS := $(sort $(wildcard migrations/*.sql))

migrate:   ## apply migrations to $CHRONICLE_DB_URL
	@test -n "$$CHRONICLE_DB_URL" || { echo "set CHRONICLE_DB_URL"; exit 1; }
	@for m in $(MIGRATIONS); do \
	  echo "-> $$m"; \
	  psql "$$CHRONICLE_DB_URL" -v ON_ERROR_STOP=1 -q -f "$$m" || exit 1; \
	done

# 003 drops hybrid_search and stratified_search (PostgreSQL cannot rename an
# OUT parameter with CREATE OR REPLACE), so 002 has to be re-applied after it.
# This is the ONLY correct order for upgrading an already-populated database.
migrate-rename:  ## upgrade a LIVE db from `episode` to `segment` (003 then 002)
	@test -n "$$CHRONICLE_DB_URL" || { echo "set CHRONICLE_DB_URL"; exit 1; }
	psql "$$CHRONICLE_DB_URL" -v ON_ERROR_STOP=1 -f migrations/003_rename_episode_to_segment.sql
	psql "$$CHRONICLE_DB_URL" -v ON_ERROR_STOP=1 -f migrations/002_retrieval.sql
	@psql "$$CHRONICLE_DB_URL" -v ON_ERROR_STOP=1 -Atc \
	  "SELECT 1 FROM pg_proc WHERE proname='hybrid_search'" | grep -q 1 \
	  || { echo "FAILED: hybrid_search missing after rename"; exit 1; }
	@echo "rename complete; hybrid_search restored"

smoke:     ## migrations + function smoke test against a throwaway DB
	./scripts/smoke.sh

doctor:    ## validate configured sources BEFORE ingesting (run this first)
	python3 -m chronicle.doctor --tier $${TIER:-1}

doctor-homelab: ## same, but ON the box — the sources are files and stack-private DBs
	./scripts/doctor-homelab.sh --tier $${TIER:-1}

ingest:    ## sources -> event -> episode -> embedding
	python3 -m chronicle.worker all --tier $${TIER:-1}

eval-init: ## write the question template you must fill in by hand
	python3 -m chronicle.evaluate init

eval:      ## chronicle vs ripgrep on your own questions
	python3 -m chronicle.evaluate compare
