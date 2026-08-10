#!/usr/bin/env bash
# Run `chronicle.doctor` ON THE HOMELAB BOX against the real sources.
#
# Doctor has to run where the sources are: telegram-sync and wakapi are SQLite
# files on disk, and every source database is stack-private with no published
# host port. So this ships the package to a scratch dir and runs it in a
# throwaway container attached to those stacks' networks.
#
# Strictly read-only. The SQLite files are opened with a `mode=ro` URI, and
# every password is read from its own stack's .env at run time into a
# mode-600 env-file — never a `-e` flag, which would put it in the box's
# process list for the lifetime of the run.
#
#   ./scripts/doctor-homelab.sh              # tier 1
#   ./scripts/doctor-homelab.sh --tier 2     # + firefly, forgejo
#
# Teardown when you are done — /tmp on that box is tmpfs, i.e. RAM, and the box
# runs 61.4 GB of committed mem_limit on 32 GB:
#   ssh homelab 'rm -rf /tmp/chronicle-doctor'
set -euo pipefail

HOST="${CHRONICLE_DOCTOR_HOST:-homelab}"
DIR=/tmp/chronicle-doctor

tar czf - chronicle | ssh "$HOST" "mkdir -p $DIR && tar xzf - -C $DIR"

# psycopg (postgres) and pymysql (firefly is MariaDB) are the only non-stdlib
# imports doctor needs. Vendored once; the box has no pip and no ensurepip, so
# this happens inside the container.
ssh "$HOST" "test -d $DIR/vendor || docker run --rm -v $DIR:/app -w /app \
  python:3.12-slim pip -q install --target=/app/vendor 'psycopg[binary]>=3.2' pymysql"

# Everything below runs on the box. The env-file is assembled there from each
# stack's own .env and removed on the way out; no secret is ever echoed back.
ssh "$HOST" "set -eu
umask 077
ENVF=\$(mktemp $DIR/env.XXXXXX)
trap 'rm -f \$ENVF' EXIT

DAWARICH_PW=\$(grep -E '^DAWARICH_DB_PASSWORD=' /srv/stacks/dawarich/.env | cut -d= -f2-)
FORGEJO_PW=\$(grep -E '^FORGEJO_DB_PASSWORD=' /srv/stacks/forgejo/.env | cut -d= -f2-)
. /srv/stacks/firefly/.env   # DB_USERNAME / DB_PASSWORD / DB_DATABASE

cat >\$ENVF <<EOF
PYTHONPATH=/app/vendor
TELEGRAM_DB_URL=/srv/telegram/telegram.db
WAKAPI_DB_PATH=/srv/wakapi/wakapi.db
WAKAPI_USER=${WAKAPI_USER:-Yehor}
DAWARICH_DB_URL=postgresql://dawarich:\$DAWARICH_PW@dawarich_db:5432/dawarich
DAWARICH_USER_ID=${DAWARICH_USER_ID:-2}
FIREFLY_DB_URL=mysql://\$DB_USERNAME:\$DB_PASSWORD@firefly-db:3306/\$DB_DATABASE
FORGEJO_DB_URL=postgresql://forgejo:\$FORGEJO_PW@forgejo-db:5432/forgejo
FORGEJO_EMAIL=${FORGEJO_EMAIL:-sam@example.com}
EOF

# One --network per stack whose database we read. Tier-1 runs simply never
# construct the tier-2 adapters, so the extra attachments are inert.
docker run --rm \
  --network dawarich_default \
  --network firefly_default \
  --network forgejo_default \
  -v /srv/stacks/telegram-sync/data:/srv/telegram \
  -v /var/lib/docker/volumes/wakapi_wakapi_data/_data:/srv/wakapi:ro \
  -v $DIR:/app -w /app \
  --env-file \$ENVF \
  python:3.12-slim python -m chronicle.doctor ${*:---tier 1}"

# The telegram mount is deliberately NOT `:ro`. telegram.db is in WAL mode, and
# WAL creates a -shm file for locking even on a read-only connection, so a `:ro`
# bind mount fails outright with "unable to open database file". wakapi.db is
# journal_mode=delete, so `:ro` is fine there.
