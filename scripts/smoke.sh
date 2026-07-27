#!/usr/bin/env bash
# Apply migrations to a throwaway database and exercise every SQL function.
# Requires PostgreSQL 16 + pgvector >= 0.7 (halfvec was added in 0.7.0).
set -euo pipefail
DB="${1:-chronicle_smoke}"
psql -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $DB" postgres
psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE $DB" postgres
psql -v ON_ERROR_STOP=1 -q -d "$DB" -f migrations/001_core.sql
psql -v ON_ERROR_STOP=1 -q -d "$DB" -f migrations/002_retrieval.sql
psql -v ON_ERROR_STOP=1 -q -d "$DB" -f scripts/smoke.sql
echo "smoke OK"
