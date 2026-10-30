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
notify() { [ -z "$NTFY" ] || curl -s -m 10 -H "Title: chronicle nightly" -d "$1" "$NTFY" >/dev/null || true; }
fail() {
  notify "FAILED: $1"
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

# Nytka (tier 4, so only with TIER=4): a read-only role whose password belongs to
# the nytka stack, read in place like the others. Without it the source is skipped.
pw=$(val ../nytka/.env CHRONICLE_RO_PASSWORD)
if [ -n "$pw" ]; then
  export NYTKA_DB_URL="postgresql://chronicle_ro:$pw@nytka-postgres:5432/nytka"
  args+=(-e NYTKA_DB_URL)
fi

# The history ledger (github source): a read-only token for Egoushka/workspace's
# `ledger` release, in homelab-gitops' chronicle-ingest stack (secrets only, no
# containers). Without it the source is skipped.
tok=$(val ../chronicle-ingest/.env LEDGER_GITHUB_TOKEN)
if [ -n "$tok" ]; then
  export LEDGER_GITHUB_TOKEN=$tok
  args+=(-e LEDGER_GITHUB_TOKEN)
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

# A retired source stack takes its network with it, and Compose then refuses to
# start the worker at all: from 2026-10-14 to 10-29 "network dawarich_default
# declared as external, but could not be found" stopped every source, not just
# dawarich. Run without such networks instead, so a missing stack costs only its
# own source, and alert so compose.override.yaml gets cleaned up.
compose=(docker compose)
keep=() missing=()
while read -r key name external; do
  if [ "$external" = True ] && ! docker network inspect "$name" >/dev/null 2>&1; then
    missing+=("$name")
  else
    keep+=("$key")
  fi
done < <(docker compose --profile batch config --format json | python3 -c '
import json, sys
c = json.load(sys.stdin)
for k in c["services"]["chronicle-worker"].get("networks") or {}:
    n = (c.get("networks") or {}).get(k) or {}
    print(k, n.get("name", k), bool(n.get("external")))
')
if [ ${#missing[@]} -gt 0 ]; then
  echo "WARN: running without missing source network(s): ${missing[*]}"
  notify "WARN: running without missing source network(s): ${missing[*]}. Remove them from compose.override.yaml."
  ov=$(mktemp)
  trap 'rm -f "$ov"' EXIT
  # -f turns off the automatic compose.override.yaml, so name it again.
  printf 'services:\n  chronicle-worker:\n    networks: !override [%s]\n' "$(IFS=,; echo "${keep[*]}")" >"$ov"
  compose+=(-f compose.yaml)
  [ ! -f compose.override.yaml ] || compose+=(-f compose.override.yaml)
  compose+=(-f "$ov")
fi

# ${args[@]+...}: an empty array is "unbound" under set -u before bash 4.4.
"${compose[@]}" --profile batch run --rm ${args[@]+"${args[@]}"} \
  chronicle-worker python -m chronicle.worker all --tier "${TIER:-2}"
echo "=== $(date -u +%FT%TZ) done"
