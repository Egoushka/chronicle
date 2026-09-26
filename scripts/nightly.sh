#!/usr/bin/env bash
# Nightly chronicle run: ingest -> fit-gaps -> segment -> enrich -> embed.
# Runs ON THE BOX as root, from /etc/cron.d/chronicle (homelab-gitops
# host/cron.d/chronicle). Every stage is incremental and resumable, so a
# missed night costs nothing but latency and an OOM kill costs one batch.
#
# Secrets that belong to OTHER stacks are read in place, at run time, and
# handed to the container by NAME (`-e VAR` with no value), so they never
# land in chronicle/.env and never appear in the process list — the same rule
# scripts/doctor-homelab.sh follows. chronicle/.env holds only chronicle's
# own: DB_PASSWORD and its LiteLLM virtual key.
#
#   TIER=1 ./scripts/nightly.sh     # tier 1 only (telegram, wakapi, dawarich)
set -euo pipefail
cd "$(dirname "$0")/.."

NTFY=http://192.0.2.10:8080/homelab-alerts
fail() {
  curl -s -m 10 -H "Title: chronicle nightly" -d "FAILED: $1" "$NTFY" >/dev/null || true
  exit 1
}
trap 'fail "line $LINENO (exit $?) — docker logs is empty for run --rm; see /var/log/chronicle.log"' ERR

val() { grep -E "^$2=" "$1" 2>/dev/null | cut -d= -f2- || true; }

export DAWARICH_USER_ID=2
pw=$(val /srv/stacks/dawarich/.env DAWARICH_DB_PASSWORD)
[ -n "$pw" ] && export DAWARICH_DB_URL="postgresql://dawarich:$pw@dawarich_db:5432/dawarich"

# firefly's .env is shell-sourceable (doctor-homelab.sh sources it too).
if [ -f /srv/stacks/firefly/.env ]; then
  ff=$(set -a; . /srv/stacks/firefly/.env; echo "$DB_USERNAME:$DB_PASSWORD@firefly-db:3306/$DB_DATABASE")
  export FIREFLY_DB_URL="mysql://$ff"
fi

LASTFM_API_KEY=$(val /opt/homelab/.ingest-secrets LASTFM_API_KEY)
LASTFM_USER=$(val /opt/homelab/.ingest-secrets LASTFM_USER)
export LASTFM_API_KEY LASTFM_USER

echo "=== $(date -u +%FT%TZ) chronicle nightly, tier ${TIER:-2}"
docker compose --profile batch run --rm \
  -e DAWARICH_DB_URL -e DAWARICH_USER_ID -e FIREFLY_DB_URL \
  -e LASTFM_API_KEY -e LASTFM_USER \
  chronicle-worker python -m chronicle.worker all --tier "${TIER:-2}"
echo "=== $(date -u +%FT%TZ) done"
