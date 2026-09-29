---
title: "Deploy and operate"
description: "Deploy beside your source stacks, schedule the nightly run, upgrade to a tag, erase or redact what is stored, rebuild segments, and roll back."
order: 3
section: "Guides"
---

Chronicle is one Docker Compose stack: `chronicle-db`, `chronicle-api`, `chronicle-mcp`, and a `chronicle-worker` that runs on demand under the `batch` profile ([DEPLOY.md](../DEPLOY.md)). It reads its sources and never writes to them. This page assumes the layout [compose.sources.example.yaml](../../compose.sources.example.yaml) describes: each source runs as its own Compose stack in a sibling directory of the chronicle checkout.

## First deploy

Before the first start ([DEPLOY.md](../DEPLOY.md#before-first-deploy)):

- `chronicle-db` is pinned by digest to a `pgvector/pgvector:pg16` build. Keep pgvector at 0.7 or newer if you change it: every embedding column is `halfvec`, which 0.6 does not have.
- The stack's network uses the subnet `10.211.71.0/24`. Change it in [compose.yaml](../../compose.yaml) if your host already uses that range.
- Clone into a directory named `chronicle`. Compose names the project after the directory, and [eval-homelab.sh](../../scripts/eval-homelab.sh) expects the network `chronicle_default`.
- Write `.env` and `compose.override.yaml` as in the [Quickstart](quickstart.md#configure) and [Connect a source](sources.md).

On a host in the sibling layout, [first-deploy.sh](../../scripts/first-deploy.sh) does the first deploy in order. Run it on the host, as root, from the checkout:

```bash title="First deploy in the sibling layout"
WAKAPI_USER=<login> DAWARICH_USER_ID=<id> ./scripts/first-deploy.sh
```

It refuses to start without `compose.override.yaml`. If `.env` is missing, it writes one with a random `DB_PASSWORD` and dawarich's database password read from `../dawarich/.env`; `WAKAPI_USER` and `DAWARICH_USER_ID` are needed only then. It starts `chronicle-db` alone, checks that the migrations created the tables and that pgvector is 0.7 or newer, builds the worker, runs `doctor --tier 1`, and stops on a failure. Then it ingests tier 1, fits the gaps, segments, prints 100 random segments, and stops so you can read them. Every step checks before acting, so a re-run resumes.

When the sample reads right, embed and start the services:

```bash title="Embed, then serve"
docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker embed
docker compose up -d chronicle-api chronicle-mcp
```

> [!IMPORTANT]
> Nothing authenticates. [compose.yaml](../../compose.yaml) publishes ports 8030 and 8031 on `127.0.0.1` and keeps every service on the stack's own network. Keep it that way: the database is the most sensitive data on the host ([SECURITY.md](../../SECURITY.md)).

## Schedule the nightly run

[nightly.sh](../../scripts/nightly.sh) is what cron runs on the reference host. It runs `python -m chronicle.worker all --tier 2` in a throwaway worker container: `ingest`, `fit-gaps`, `segment`, `enrich`, `embed`. Every stage is incremental and safe to re-run, which [worker-itest.py](../../scripts/worker-itest.py) checks by running the pipeline five times against a real database.

```text title="An example root crontab line"
0 3 * * * <stacks>/chronicle/scripts/nightly.sh >> /var/log/chronicle.log 2>&1
```

- **Tier.** `TIER=1 ./scripts/nightly.sh` limits the run to tier 1. The default is 2.
- **Other stacks' secrets.** The script reads dawarich's database password from `../dawarich/.env`, Firefly's credentials from `../firefly/.env`, and the Last.fm key from the file `INGEST_SECRETS_FILE` names. It passes them to the container by name, so they never land in chronicle's `.env` or in the process list. A source whose settings are missing is skipped.
- **Alerts.** Set `NTFY_URL` in the environment or in `.env` to an ntfy topic URL, and a failed night posts `FAILED: line N ...` there. Unset, a failure is only in the log.
- **Exit code.** A source that fails during `ingest` does not fail the run: its error goes to `source.last_error`, which `/stats` shows, and the other sources carry on. A failed `enrich` still lets `embed` run, and the run then exits 1 ([worker.py](../../chronicle/worker.py)). No alert fires for a source that simply goes quiet; that is roadmap goal 8.

The worker is `restart: "no"` and exits when its batch is done, so it holds memory only while it runs. An out-of-memory kill costs one batch, not the run ([compose.yaml](../../compose.yaml)).

## Upgrade

A deployment runs a tag, never a working tree ([CLAUDE.md](../../CLAUDE.md#versions-and-releases)). Read the release's section in [CHANGELOG.md](../../CHANGELOG.md) first: a release that needs more than pulling and restarting, such as a migration, a new setting or a backfill, says so under **Upgrade**.

```bash title="Move to a release"
git fetch --tags
git checkout v0.3.0
docker compose --profile batch build
docker compose up -d chronicle-api chronicle-mcp
curl -s http://127.0.0.1:8030/health
```

`/health` reports the version the api runs.

PostgreSQL runs the migrations from its init directory only on an empty volume, so an upgrade that adds a migration needs it applied by hand. The database container mounts [migrations/](../../migrations) at `/docker-entrypoint-initdb.d`, so you can apply one file there:

```bash title="Apply one migration to a running database"
docker compose exec -T chronicle-db psql -U chronicle -d chronicle -v ON_ERROR_STOP=1 \
  -f /docker-entrypoint-initdb.d/005_incremental.sql
```

Files 002 to 005 are written to be applied again. 001 is not: it creates its tables without `IF NOT EXISTS`. `make migrate` applies every file in order to `$CHRONICLE_DB_URL` and stops at the first error, so point it only at an empty database. A database from before the rename of `episode` to `segment` upgrades with `make migrate-rename`, which runs 003 and then 002.

## Erase what a filter now excludes

Source filters act when events are fetched. Tightening one, such as excluding another chat, keeps those rows out of the next ingest and leaves everything the old rule indexed searchable. [purge.py](../../chronicle/purge.py) deletes that difference. Run it in the worker, the only container that can read the sources:

```bash title="Count, then delete"
docker compose --profile batch run --rm chronicle-worker python -m chronicle.purge
docker compose --profile batch run --rm chronicle-worker python -m chronicle.purge --apply
```

Without `--apply` it only counts, per source: events, segments, embedded segments, entity mentions, facts, commitments, and commitments resolved by a doomed segment. `--source telegram` limits it to one source. The rules come from each adapter's `excluded_thread_keys()`, and today only `telegram` excludes anything: bot chats, the service account and `TELEGRAM_EXCLUDE_CHAT_IDS`.

`--apply` works in one transaction. It reopens commitments that a doomed segment resolved, prunes the doomed event ids from `life_event`, deletes the `projection_dep` rows, deletes the segments (their entity mentions, facts and commitments go with them), deletes the events, writes an `erasure_log` row with the rule and the counts, and then checks that nothing is left, rolling back if anything is. [purge-itest.py](../../scripts/purge-itest.py) runs this against a real database.

> [!CAUTION]
> `--apply` deletes events, segments, embeddings and facts for good. Chronicle has no undo; the source keeps its own copy. With no source configured, purge finds nothing and exits 0, which looks the same as a clean archive: check that the dry run lists your source first.

## Redact secrets already stored

Ingest redacts secrets before a row is written, for every source ([redact.py](../../chronicle/redact.py)). The backfill applies the same rules to what was stored before:

```bash title="Count, then rewrite"
docker compose --profile batch run --rm chronicle-worker python -m chronicle.redact
docker compose --profile batch run --rm chronicle-worker python -m chronicle.redact --apply
docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker embed
```

The dry run prints how many events hold a secret, per source and per kind, and never prints a value. `--apply` rewrites those events and every segment that cites them in one transaction, clears the segments' embeddings, marks them unenriched, and writes an `erasure_log` row; `embed` then re-encodes them. Facts already extracted from a rewritten segment stay until it is enriched again. [redact-itest.py](../../scripts/redact-itest.py) checks that a second `--apply` finds nothing.

The rules cover documented formats (private keys; AWS, Google and Stripe keys; GitHub, GitLab, Slack and Telegram bot tokens; `sk-` API keys; JWTs), a password inside a URL, a value labelled as a password, secret, token or API key in English, Russian or Ukrainian, and a long value alone on its line when the text names a secret elsewhere. A match becomes `[REDACTED:<kind>]`.

> [!IMPORTANT]
> Rotate every credential the backfill finds. Redaction removes the value from chronicle, not from wherever else it was pasted ([CHANGELOG 0.2.0](../../CHANGELOG.md#020---2026-09-29)).

## Rebuild threads at a new segment cap

A narrative segment holds at most 30 events by default, and that cap has not been measured ([worker.py](../../chronicle/worker.py)). Roadmap goal 5 is to sweep it. `segment` never revisits a finished segment, so a new cap applies only to new events; `resegment` rebuilds named threads from their events:

```bash title="Rebuild two threads at a cap of 15, then re-embed them"
docker compose --profile batch run --rm chronicle-worker \
  python -m chronicle.worker resegment --max-messages 15 --thread telegram:123 --thread telegram:456
docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker embed
```

`make -s eval-threads` prints the thread keys that your evaluation questions cite, which is the scope a sweep needs ([Measure it against grep](evaluate.md)). Each thread is rebuilt in its own transaction: its segments and the facts and commitments derived from them are dropped, commitments they resolved reopen, and events stay untouched. The rebuilt segments have no embedding until `embed` runs. Each segment records its cap in `segmenter_version` (`seg-2026.07-timegap-v1/m15`), and `SEGMENT_MAX_MESSAGES` sets the default for `segment` and `resegment`. [resegment-itest.py](../../scripts/resegment-itest.py) covers the rebuild.

## Try enrichment

Enrichment asks an LLM for a summary, topics, facts and commitments per segment ([enrich.py](../../chronicle/enrich.py)). It is off by default: compose.yaml sets `ENRICH_LIMIT` to 0 unless `.env` says otherwise. The one batch run so far lowered the evaluation score, and roadmap goal 7 is to keep it or delete it by an A/B test ([ROADMAP.md](../../ROADMAP.md#stage-0--worth-using-over-grep-for-its-owner-v0x)).

To run the A/B, measure first, then set in `.env`:

| Variable | Set it to |
|---|---|
| `ENRICH_LIMIT` | segments to enrich per run |
| `ENRICH_MODEL` | a model your endpoint serves |
| `ENRICH_URL` | OpenAI-compatible base URL; default `http://litellm:4000/v1` |
| `LITELLM_API_KEY` | the key for that endpoint |

Then run `all` (it enriches before it embeds, and enriched segments are re-encoded) and measure again.

> [!WARNING]
> Enrichment sends each chosen segment's text (its first 8,000 characters), chat title and date to that endpoint. It is the one path by which archive text leaves the host. It picks only narrative segments that pass the substantive filter, newest first.

`.env.example` sets `ENRICH_MODEL=qwen3:8b-q4_K_M` and calls it a local model; compose.yaml defaults to `gemini-3.5-flash-lite` through LiteLLM and says that local model never existed. A copied `.env` overrides the compose default, so set the variable yourself. A batch in which every call fails stops `enrich` with exit 1: a wrong key, a renamed model, an outage. Setting `ENRICH_LIMIT` back to 0 stops new enrichment; segments already enriched keep their topics and facts in their embedded text until their text changes or their thread is rebuilt.

## Roll back

Chronicle is read-only against every source, so nothing it did needs undoing there. `docker compose down` and removing the `chronicle_pg` volume return the host to its state before chronicle ([DEPLOY.md](../DEPLOY.md#rollback)). Compose prefixes volume names with the project name; `docker volume ls` shows the full name.

> [!CAUTION]
> Removing `chronicle_pg` deletes the whole archive: events, segments, embeddings and facts. The first embed of the reference archive took about 9.8 hours.
