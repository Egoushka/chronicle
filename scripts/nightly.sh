#!/usr/bin/env bash
# Nightly chronicle run: ingest -> fit-gaps -> segment -> enrich -> embed.
# Runs ON THE HOST as root, from cron, in the sibling-stacks layout that
# compose.sources.example.yaml describes. Every stage is incremental and
# resumable, so a missed night costs nothing but latency and an OOM kill costs
# one batch.
#
# Secrets that belong to OTHER stacks are read in place, at run time, and
# handed to the container by NAME (`-e VAR` with no value), so they never
# land in chronicle/.env and never appear in the process list — the same rule
# scripts/doctor-homelab.sh follows. chronicle/.env holds chronicle's own
# secrets (DB_PASSWORD, its LiteLLM key) plus ids and paths, never another
# stack's secret.
#
#   TIER=1 ./scripts/nightly.sh     # tier 1 only (telegram, wakapi, dawarich)
set -euo pipefail
cd "$(dirname "$0")/.."

val() { grep -E "^$2=" "$1" 2>/dev/null | cut -d= -f2- || true; }

# Failure alerts go to the ntfy topic named by NTFY_URL, from the environment
# or from chronicle/.env. Unset, a failed night is only in the log.
NTFY=${NTFY_URL:-$(val .env NTFY_URL)}
[ -n "$NTFY" ] || echo "NTFY_URL is unset: a failure tonight will not alert"
fail() {
  [ -z "$NTFY" ] || curl -s -m 10 -H "Title: chronicle nightly" -d "FAILED: $1" "$NTFY" >/dev/null || true
  exit 1
}
trap 'fail "line $LINENO (exit $?) — docker logs is empty for run --rm; see /var/log/chronicle.log"' ERR

# The other stacks are this checkout's siblings. Per-user ids and the shared
# secrets file come from the environment or chronicle/.env, never from here;
# a source whose settings are missing is skipped, not failed.
args=()
DAWARICH_USER_ID=${DAWARICH_USER_ID:-$(val .env DAWARICH_USER_ID)}
pw=$(val ../dawarich/.env DAWARICH_DB_PASSWORD)
if [ -n "$pw" ] && [ -n "$DAWARICH_USER_ID" ]; then
  export DAWARICH_USER_ID DAWARICH_DB_URL="postgresql://dawarich:$pw@dawarich_db:5432/dawarich"
  args+=(-e DAWARICH_DB_URL -e DAWARICH_USER_ID)
fi

# firefly's .env is shell-sourceable (doctor-homelab.sh sources it too).
if [ -f ../firefly/.env ]; then
  ff=$(set -a; . ../firefly/.env; echo "$DB_USERNAME:$DB_PASSWORD@firefly-db:3306/$DB_DATABASE")
  export FIREFLY_DB_URL="mysql://$ff"
  args+=(-e FIREFLY_DB_URL)
fi

# Last.fm's key may live in a secrets file shared by several stacks. Without
# one, LASTFM_API_KEY / LASTFM_USER in chronicle/.env reach the worker as-is:
# passing an empty `-e` here would override them.
SECRETS=${INGEST_SECRETS_FILE:-$(val .env INGEST_SECRETS_FILE)}
if [ -n "$SECRETS" ]; then
  LASTFM_API_KEY=$(val "$SECRETS" LASTFM_API_KEY)
  LASTFM_USER=$(val "$SECRETS" LASTFM_USER)
  export LASTFM_API_KEY LASTFM_USER
  args+=(-e LASTFM_API_KEY -e LASTFM_USER)
fi

echo "=== $(date -u +%FT%TZ) chronicle nightly, tier ${TIER:-2}"
# ${args[@]+...}: an empty array is "unbound" under set -u before bash 4.4.
docker compose --profile batch run --rm ${args[@]+"${args[@]}"} \
  chronicle-worker python -m chronicle.worker all --tier "${TIER:-2}"
echo "=== $(date -u +%FT%TZ) done"
