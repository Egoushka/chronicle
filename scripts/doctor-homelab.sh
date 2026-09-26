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
#   ./scripts/doctor-homelab.sh --tier 2     # + firefly, lastfm
#   ./scripts/doctor-homelab.sh --tier 3     # + immich, paperless, karakeep
#   ./scripts/doctor-homelab.sh --tier 4     # + miniflux (owntracks is
#       deliberately unset: it reads the same GPS as dawarich, and doctor fails
#       the pair as a conflict — its store holds no .rec history anyway)
#
# Teardown when you are done — /tmp on that box is tmpfs, i.e. RAM, and the box
# runs 61.4 GB of committed mem_limit on 32 GB:
#   ssh homelab 'rm -rf /tmp/chronicle-doctor'
set -euo pipefail

HOST="${CHRONICLE_DOCTOR_HOST:-homelab}"
DIR=/tmp/chronicle-doctor

tar czf - chronicle | ssh "$HOST" "mkdir -p $DIR && tar xzf - -C $DIR"

# psycopg (postgres), pymysql (firefly is MariaDB) and httpx (lastfm) are the
# only non-stdlib imports doctor needs. Vendored once; the box has no pip and
# no ensurepip, so this happens inside the container. Keyed on httpx, the
# newest of the three, so an older vendor dir is topped up rather than reused.
ssh "$HOST" "test -d $DIR/vendor/httpx || docker run --rm -v $DIR:/app -w /app \
  python:3.12-slim pip -q install --target=/app/vendor 'psycopg[binary]>=3.2' pymysql httpx"

# Everything below runs on the box. The env-file is assembled there from each
# stack's own .env and removed on the way out; no secret is ever echoed back.
#
# Every read tolerates a missing stack (`|| true`): a stack that is gone must
# SKIP its source, not abort the run. forgejo left the box on 2026-09-16 and
# its bare grep here, under `set -e`, failed EVERY tier — tier 1 included,
# which never reads it.
ssh "$HOST" "set -eu
umask 077
ENVF=\$(mktemp $DIR/env.XXXXXX)
trap 'rm -f \$ENVF' EXIT

DAWARICH_PW=\$(grep -E '^DAWARICH_DB_PASSWORD=' /srv/stacks/dawarich/.env | cut -d= -f2- || true)
IMMICH_USER=\$(grep -E '^DB_USERNAME=' /srv/stacks/immich/.env | cut -d= -f2- || true)
IMMICH_PW=\$(grep -E '^DB_PASSWORD=' /srv/stacks/immich/.env | cut -d= -f2- || true)
IMMICH_DB=\$(grep -E '^DB_DATABASE_NAME=' /srv/stacks/immich/.env | cut -d= -f2- || true)
PAPERLESS_PW=\$(grep -E '^POSTGRES_PASSWORD=' /srv/stacks/paperless/.env | cut -d= -f2- || true)
MINIFLUX_PW=\$(grep -E '^MF_DB_PASSWORD=' /srv/stacks/miniflux/.env | cut -d= -f2- || true)
LASTFM_KEY=\$(grep -E '^LASTFM_API_KEY=' /opt/homelab/.ingest-secrets | cut -d= -f2- || true)
LASTFM_U=\$(grep -E '^LASTFM_USER=' /opt/homelab/.ingest-secrets | cut -d= -f2- || true)
. /srv/stacks/firefly/.env   # DB_USERNAME / DB_PASSWORD / DB_DATABASE

cat >\$ENVF <<EOF
PYTHONPATH=/app/vendor
TELEGRAM_DB_URL=/srv/telegram/telegram.db
WAKAPI_DB_PATH=/srv/wakapi/wakapi.db
WAKAPI_USER=${WAKAPI_USER:-Yehor}
DAWARICH_DB_URL=postgresql://dawarich:\$DAWARICH_PW@dawarich_db:5432/dawarich
DAWARICH_USER_ID=${DAWARICH_USER_ID:-2}
FIREFLY_DB_URL=mysql://\$DB_USERNAME:\$DB_PASSWORD@firefly-db:3306/\$DB_DATABASE
LASTFM_API_KEY=\$LASTFM_KEY
LASTFM_USER=\$LASTFM_U
IMMICH_DB_URL=postgresql://\$IMMICH_USER:\$IMMICH_PW@immich_postgres:5432/\$IMMICH_DB
PAPERLESS_DB_URL=postgresql://paperless:\$PAPERLESS_PW@paperless-db:5432/paperless
KARAKEEP_DB_PATH=/srv/karakeep/db.db
MINIFLUX_DB_URL=postgresql://miniflux:\$MINIFLUX_PW@miniflux-db:5432/miniflux
EOF

# One --network per stack whose database we read. Lower-tier runs simply never
# construct the higher-tier adapters, so the extra attachments are inert.
docker run --rm \
  --network dawarich_default \
  --network firefly_default \
  --network immich_default \
  --network paperless_default \
  --network miniflux_default \
  -v /srv/stacks/telegram-sync/data:/srv/telegram \
  -v /var/lib/docker/volumes/wakapi_wakapi_data/_data:/srv/wakapi:ro \
  -v /var/lib/docker/volumes/karakeep_karakeep_data/_data:/srv/karakeep:ro \
  -v $DIR:/app -w /app \
  --env-file \$ENVF \
  python:3.12-slim python -m chronicle.doctor ${*:---tier 1}"

# The telegram mount is deliberately NOT `:ro`. telegram.db is in WAL mode, and
# WAL creates a -shm file for locking even on a read-only connection, so a `:ro`
# bind mount fails outright with "unable to open database file". wakapi.db and
# karakeep's db.db are journal_mode=delete (verified 2026-09-26), so `:ro` is
# fine there.
