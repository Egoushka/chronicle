.PHONY: test lint migrate smoke fmt help

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

test:      ## run the unit suite (no DB, no models needed)
	PYTHONPATH=. python3 -m pytest tests/ -q

lint:      ## ruff
	ruff check chronicle/ tests/

fmt:       ## ruff format
	ruff format chronicle/ tests/

migrate:   ## apply migrations to $CHRONICLE_DB_URL
	@test -n "$$CHRONICLE_DB_URL" || { echo "set CHRONICLE_DB_URL"; exit 1; }
	psql "$$CHRONICLE_DB_URL" -v ON_ERROR_STOP=1 -f migrations/001_core.sql
	psql "$$CHRONICLE_DB_URL" -v ON_ERROR_STOP=1 -f migrations/002_retrieval.sql

smoke:     ## migrations + function smoke test against a throwaway DB
	./scripts/smoke.sh
