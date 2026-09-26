#!/usr/bin/env bash
# `make eval` ON THE BOX: chronicle vs ripgrep on eval/questions.json.
#
# The dump is the whole archive as text, so it never leaves the box: written
# by COPY inside chronicle-db, read by a throwaway container on
# chronicle_default (which also reaches chronicle-api:8030), then deleted.
# No credentials involved — COPY runs as the db container itself.
set -euo pipefail
cd "$(dirname "$0")/.."
D=$(mktemp -d /var/tmp/chronicle-eval.XXXXXX)
trap "rm -rf $D" EXIT
docker exec chronicle-db psql -U chronicle -d chronicle -Atc \
  "COPY (SELECT source||':'||source_id, ts, replace(replace(text, E'\\n', ' '), E'\\t', ' ')
         FROM event ORDER BY ts) TO STDOUT" > "$D/dump.tsv"
docker run --rm --network chronicle_default -v "$PWD":/app:ro -v "$D":/d -w /app \
  -e CHRONICLE_EVAL=/app/eval/questions.json python:3.12-slim sh -c "
    apt-get -qq update >/dev/null && apt-get -qq install -y ripgrep >/dev/null &&
    pip -q install --root-user-action=ignore httpx >/dev/null &&
    python -m chronicle.evaluate compare --dump /d/dump.tsv --api http://chronicle-api:8030"
