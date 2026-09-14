#!/usr/bin/env bash
# First deploy of the chronicle stack. RUN THIS ON THE BOX, as root.
#
#   ssh homelab
#   cd /srv/stacks/chronicle && ./scripts/first-deploy.sh
#
# It is idempotent: every step checks before acting, so re-running after a
# failure resumes rather than restarts.
#
# WHY A SCRIPT AND NOT A RUNBOOK: the ordering constraints below are not
# obvious and each one cost a debugging cycle somewhere in this repo. Encoding
# them means they cannot be skipped in the wrong order at 1am.
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Secrets.
#
# Read out of the stacks that already own them, on the box, so no credential
# is ever copied through a laptop or a terminal transcript.
# ---------------------------------------------------------------------------
if [ ! -f .env ]; then
  say "writing .env"
  [ -f /srv/stacks/dawarich/.env ]      || die "dawarich stack not found"
  [ -f /srv/stacks/telegram-sync/.env ] || die "telegram-sync stack not found"
  umask 077
  {
    echo "DB_PASSWORD=$(openssl rand -hex 24)"
    echo "LITELLM_BASE_URL=http://litellm:4000/v1"
    grep -E '^LITELLM_API_KEY=' /srv/stacks/telegram-sync/.env
    echo "EMBED_MODEL=BAAI/bge-m3"
    echo "BATCH_SIZE=500"
    echo
    echo "TELEGRAM_DB_URL=/srv/telegram/telegram.db"
    echo "WAKAPI_DB_PATH=/srv/wakapi/wakapi.db"
    echo "WAKAPI_USER=Yehor"
    echo "DAWARICH_DB_URL=postgresql://dawarich:$(grep -E '^DAWARICH_DB_PASSWORD=' \
        /srv/stacks/dawarich/.env | cut -d= -f2-)@dawarich_db:5432/dawarich"
    echo "DAWARICH_USER_ID=2"
  } > .env
  chmod 600 .env
else
  say ".env exists, keeping it"
fi

# ---------------------------------------------------------------------------
# 2. Database first, alone.
#
# migrations/ is mounted at /docker-entrypoint-initdb.d, which Postgres runs
# ONLY on an empty data directory — so this must succeed the first time. If it
# does not, `docker compose down -v` and start over; a half-migrated volume
# will not re-run them.
# ---------------------------------------------------------------------------
say "starting chronicle-db"
docker compose up -d chronicle-db
for _ in $(seq 1 30); do
  docker compose exec -T chronicle-db pg_isready -U chronicle >/dev/null 2>&1 && break
  sleep 2
done
docker compose exec -T chronicle-db pg_isready -U chronicle >/dev/null 2>&1 \
  || die "chronicle-db never became ready — docker compose logs chronicle-db"

say "verifying the schema actually migrated"
tables=$(docker compose exec -T chronicle-db psql -U chronicle -d chronicle -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
[ "$tables" -gt 5 ] || die "only $tables tables — migrations did not run. \
docker compose down -v and retry; initdb scripts run on an EMPTY volume only."
echo "    $tables tables"

vec=$(docker compose exec -T chronicle-db psql -U chronicle -d chronicle -tAc \
  "SELECT extversion FROM pg_extension WHERE extname='vector'")
[ -n "$vec" ] || die "the vector extension is not installed"
echo "    pgvector $vec"
# halfvec does not exist below 0.7 and every embedding column uses it.
case "$vec" in 0.[0-6]*) die "pgvector $vec is too old — halfvec needs >= 0.7" ;; esac

# ---------------------------------------------------------------------------
# 3. Doctor BEFORE ingest. Always. A wrong adapter writes wrong rows for as
#    long as it runs, and five of the five DB-backed adapters were wrong the
#    first time this was pointed at the real databases.
# ---------------------------------------------------------------------------
say "building the worker image (this pulls torch; it takes a while)"
docker compose --profile batch build chronicle-worker

say "doctor --tier 1"
docker compose --profile batch run --rm chronicle-worker \
  python -m chronicle.worker doctor --tier 1 \
  || die "doctor failed — fix the adapter before ingesting, not after"

# ---------------------------------------------------------------------------
# 4. Ingest, then segment. Both are resumable; an OOM kill costs one batch.
# ---------------------------------------------------------------------------
say "ingest --tier 1  (682k telegram events; long)"
docker compose --profile batch run --rm chronicle-worker \
  python -m chronicle.worker ingest --tier 1

say "fit-gaps"
docker compose --profile batch run --rm chronicle-worker \
  python -m chronicle.worker fit-gaps

say "segment"
docker compose --profile batch run --rm chronicle-worker \
  python -m chronicle.worker segment

# ---------------------------------------------------------------------------
# 5. STOP. Read segments before embedding them.
#
# Segmentation is the highest-value stage and everything downstream inherits
# it. Checking it now costs ten minutes; finding it wrong after a multi-hour
# embed costs the embed.
# ---------------------------------------------------------------------------
say "100 random segments — READ THESE before continuing"
docker compose exec -T chronicle-db psql -U chronicle -d chronicle -c \
  "SELECT thread_key, started_at::date, event_count,
          left(raw_text, 160) AS text
   FROM segment ORDER BY random() LIMIT 100"

cat <<'EOF'

  Stop here and read the sample above.

  Looking for: segments that run together conversations that have nothing to
  do with each other, or that cut mid-exchange. If either is common, the gap
  fit is wrong — fix segmentation before embedding. Everything downstream
  inherits this.

  When the sample looks right:
    docker compose --profile batch run --rm chronicle-worker \
      python -m chronicle.worker embed
    docker compose up -d chronicle-api chronicle-mcp

EOF
