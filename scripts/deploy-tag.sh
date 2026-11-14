#!/usr/bin/env bash
# Deploy a release tag to the box, the box being a reality `docs/guide/operate.md`'s
# "Upgrade" section does not describe: `<stacks>/chronicle` there is RSYNCED, not a
# git checkout (no `.git`, so `git fetch`/`git checkout vX.Y.Z` has nothing to run
# against), and it has drifted from main before — six modules out of date, a whole
# refactor (51ed713) never deployed — because nothing but a person's memory kept it
# in sync. #40.
#
#   ./scripts/deploy-tag.sh v0.7.6
#
# Run from THIS machine, inside a clone of this repo checked out exactly at the tag
# being deployed (the script insists on that below), never on the box itself — the
# box has no git history to check out from and nothing to rsync FROM.
#
# What travels: everything in the tag's tree except four paths that are the box's
# own state, never main's, and that first-deploy.sh / this box's operator put there
# on purpose:
#   .env                    box-specific secrets and settings
#   compose.override.yaml   this box's source wiring (gitignored, compose.sources.example.yaml is the template)
#   eval/questions.json     this box's private eval set (gitignored)
#   models/                 a downloaded embedding model cache
# The ghcr image (built by .github/workflows/image.yml on the same tag) carries
# chronicle/ and migrations/ (Dockerfile:25-26); compose.yaml, the scripts/ directory
# this very file lives in, and the migrations/ directory a fresh `chronicle-db`
# mounts at init time all run OUTSIDE that image, on the box itself — rsync is how
# THOSE get the tag.
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

TAG="${1:?usage: deploy-tag.sh vX.Y.Z}"
case "$TAG" in
  v[0-9]*.[0-9]*.[0-9]*) ;;
  *) die "tag must look like vX.Y.Z, got '$TAG'" ;;
esac
# .github/workflows/image.yml strips the leading 'v' for the image tag
# (GITHUB_REF_NAME#v), and chronicle/__init__.py's __version__ — what /health
# reports — never had one either.
VERSION="${TAG#v}"

HOST="${CHRONICLE_DOCTOR_HOST:?set CHRONICLE_DOCTOR_HOST to the ssh host of the box}"
STACKS="${CHRONICLE_STACKS:?set CHRONICLE_STACKS to the stacks directory on the box}"
DEST="$STACKS/chronicle"

# ---------------------------------------------------------------------------
# 1. The tree rsynced below must be EXACTLY the tag's tree, not whatever
#    happens to be checked out locally with uncommitted changes on top.
# ---------------------------------------------------------------------------
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || git fetch --tags -q
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null \
  || die "no tag $TAG (git fetch --tags found none either)"

want=$(git rev-parse "refs/tags/$TAG^{commit}")
have=$(git rev-parse HEAD)
[ "$have" = "$want" ] \
  || die "HEAD ($have) is not $TAG ($want) — git checkout $TAG first, so the tree rsynced is exactly the tag's"
[ -z "$(git status --porcelain)" ] \
  || die "working tree is dirty at $TAG — commit or stash before deploying it"

# ---------------------------------------------------------------------------
# 2. Sync the host-side tree, protecting the box's own four paths both from
#    being overwritten and from --delete (a file the tag removes elsewhere
#    still needs removing, e.g. a retired script).
# ---------------------------------------------------------------------------
say "rsync $TAG -> $HOST:$DEST"
rsync -az --delete \
  --exclude .git/ \
  --filter='protect .env' --filter='protect compose.override.yaml' \
  --filter='protect eval/questions.json' --filter='protect models/' \
  ./ "$HOST:$DEST/"

# ---------------------------------------------------------------------------
# 3. A tag can add a setting the old .env predates — 51ed713 did, moving the
#    owner's name from code into CHRONICLE_OWNER / CHRONICLE_OWNER_ALIASES,
#    and a box whose .env never got it would just silently start saying "the
#    owner" instead of failing. Diff the key NAMES (never values) against the
#    freshly-synced .env.example, which is not one of the four protected
#    paths and so is already this tag's.
# ---------------------------------------------------------------------------
say "checking .env against this tag's .env.example"
new_keys=$(ssh "$HOST" "cd $DEST && \
  comm -23 <(grep -ohE '^[A-Z][A-Z0-9_]*' .env.example | sort -u) \
           <(grep -ohE '^[A-Z][A-Z0-9_]*' .env | sort -u)")
if [ -n "$new_keys" ]; then
  echo "  new in .env.example, missing from .env on the box:"
  echo "$new_keys" | sed 's/^/    /'
  echo "  check whether $TAG needs them set before it behaves as intended."
fi

# ---------------------------------------------------------------------------
# 4. A release that needs a migration, a backfill or any other manual step
#    says so under CHANGELOG.md's heading for it (CLAUDE.md "Versions and
#    releases"). Surface that note now, before the restart, instead of after.
# ---------------------------------------------------------------------------
note=$(awk -v h="## [$VERSION]" '
  substr($0, 1, length(h)) == h { on = 1; next }
  on && /^## \[/ { exit }
  on && /^### Upgrade/ { up = 1; next }
  up && /^## / { exit }
  up { print }
' CHANGELOG.md)
if [ -n "$note" ]; then
  say "CHANGELOG.md says $VERSION needs manual Upgrade steps — this script does not run them"
  echo "$note"
fi

# ---------------------------------------------------------------------------
# 5. Pull the tag's image and restart. CHRONICLE_VERSION in .env is what
#    compose.yaml's image: line reads (compose.yaml, chronicle-api); unset
#    or missing it defaults to :local, i.e. a build from source, so this is
#    an update-in-place of one line, never a rewrite of the (protected) file.
# ---------------------------------------------------------------------------
say "setting CHRONICLE_VERSION=$VERSION on the box"
ssh "$HOST" "cd $DEST && \
  if grep -q '^CHRONICLE_VERSION=' .env; then \
    sed -i \"s/^CHRONICLE_VERSION=.*/CHRONICLE_VERSION=$VERSION/\" .env; \
  else \
    printf '\nCHRONICLE_VERSION=%s\n' '$VERSION' >> .env; \
  fi"

say "pulling and restarting chronicle-api, chronicle-mcp"
ssh "$HOST" "cd $DEST && docker compose pull chronicle-api chronicle-mcp"
ssh "$HOST" "cd $DEST && docker compose up -d chronicle-api chronicle-mcp"

# ---------------------------------------------------------------------------
# 6. /health only proves the container restarted and can reach the database —
#    it says nothing about migrations. Poll instead of a single curl: the
#    container needs a moment after `up -d` before uvicorn answers, and a
#    stale PREVIOUS version answering here (a restart that reused a cached
#    image, or that failed and rolled the container back) must fail loud, not
#    read as "it's up, good enough".
# ---------------------------------------------------------------------------
say "waiting for /health to report $VERSION"
got=""
for _ in $(seq 1 30); do
  resp=$(ssh "$HOST" "curl -fsS http://localhost:8030/health" 2>/dev/null) || resp=""
  got=$(printf '%s' "$resp" | grep -oE '"version":[[:space:]]*"[^"]*"' | grep -oE '[^":[:space:]]+"?$' | tr -d '"')
  [ -n "$got" ] && break
  sleep 2
done
[ -n "$got" ] || die "/health never answered — docker compose logs chronicle-api on $HOST"
[ "$got" = "$VERSION" ] \
  || die "/health reports '$got', not '$VERSION' — restart did not take; docker compose ps / logs on $HOST"
echo "    /health: $resp"
say "deployed $TAG ($VERSION) and verified"
