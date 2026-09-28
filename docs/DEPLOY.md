# Deploying chronicle

Chronicle is one Docker Compose stack: `chronicle-db` (PostgreSQL 16 +
pgvector), `chronicle-api`, `chronicle-mcp`, and a `chronicle-worker` that runs
on demand under the `batch` profile. It reads its sources and never writes to
them.

## Before first deploy

1. **Pin the `chronicle-db` digest** in `compose.yaml` if you want a different
   build of `pgvector/pgvector:pg16` than the one pinned:
   ```bash
   docker pull pgvector/pgvector:pg16
   docker inspect --format='{{index .RepoDigests 0}}' pgvector/pgvector:pg16
   ```
   pgvector must be **0.7 or newer**: `halfvec` does not exist in 0.6.

2. **Check the subnet.** `compose.yaml` uses `10.211.71.0/24`; change it if
   that range is taken on your host.

3. **Configure.** `cp .env.example .env` and fill it in: `DB_PASSWORD`, the
   owner (`CHRONICLE_OWNER`, `CHRONICLE_OWNER_ALIASES`), then one block per
   source you run. `.env` is gitignored; never commit it.

4. **Wire the sources.** `cp compose.sources.example.yaml compose.override.yaml`
   and keep only the stacks you run. Compose merges the override automatically;
   without it the worker sees no source at all.

## Install

```bash
ssh <host>
cd <stacks>                           # the directory that holds your source stacks
git clone https://github.com/Egoushka/chronicle.git chronicle
cd chronicle                          # then steps 3 and 4 above
docker compose up -d chronicle-db chronicle-api chronicle-mcp
```

The migrations run from `/docker-entrypoint-initdb.d`, which PostgreSQL only
executes on an **empty** data directory. If the first start fails,
`docker compose down -v` and start again; a half-migrated volume will not
re-run them. `scripts/first-deploy.sh` does the whole first deploy in order,
for hosts laid out as `compose.sources.example.yaml` describes.

If your deploy tooling polls another repo, chronicle will not update with it:
pull it explicitly, and decide that deliberately — a stack nothing pulls
silently stops receiving updates.

## Backfill order

Sequenced so each step is useful on its own and nothing is wasted if you stop.
`W` below is `docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker`.

**1. Doctor first, always.** `$W doctor --tier 1` validates every configured
source read-only, in seconds. Every DB-backed adapter in this repo was wrong
somewhere the first time it met a real database; doctor is how that shows up
before the worker writes wrong rows.

**2. Ingest tier 1, then fit gaps and segment.**
```bash
$W ingest --tier 1
$W fit-gaps
$W segment
```
Then **read 100 random segments before trusting anything downstream.** If
segmentation is wrong, everything inherits the damage, and it is much cheaper
to find that out now than after the embed.

**3. Embed.** On the reference host, 51,044 segments took ~9.8 h on a
contended 16-core CPU. It is resumable: an interrupted run continues.

**4. Measure against grep.** Write 30–50 real questions (`make eval-init`),
then `make eval` scores ripgrep and chronicle on them. Letta hit 74% on LoCoMo
with nothing but a filesystem and `grep`, beating Mem0's 68.5%. If chronicle
does not clearly win, fix retrieval before adding features.

**5. Schedule it.** `$W all --tier 2` runs every stage incrementally and is
safe to re-run; `scripts/nightly.sh` wraps it for cron.

**6. Enrich only for a measured A/B.** It is off by default
(`ENRICH_LIMIT=0`): the first batch on the reference deployment lowered the
eval from 55.0% to 52.3%. See `chronicle/enrich.py`.

## Rollback

Chronicle is read-only against every source. Nothing it does can corrupt
them. `docker compose down` plus dropping the `chronicle_pg` volume returns
the host to its prior state.
