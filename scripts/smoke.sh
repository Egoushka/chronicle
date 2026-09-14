#!/usr/bin/env bash
# Apply migrations to a throwaway database and exercise every SQL function.
# Requires PostgreSQL 16 + pgvector >= 0.7 (halfvec was added in 0.7.0).
set -euo pipefail
DB="${1:-chronicle_smoke}"
psql -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $DB" postgres
psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE $DB" postgres
# Globbed so a new migration is exercised by CI the day it lands. Enumerating
# 001 and 002 meant 003 would have been invisible here — and CI runs only this
# script, so an unapplied migration would have looked green.
for m in migrations/*.sql; do
    psql -v ON_ERROR_STOP=1 -q -d "$DB" -f "$m"
done
psql -v ON_ERROR_STOP=1 -q -d "$DB" -f scripts/smoke.sql
echo "smoke OK"
