---
title: "Quickstart"
description: "Start the stack with Docker Compose, ingest one Telegram archive, embed its segments, and get a first answer from /recall."
order: 1
section: "Get started"
---

You start chronicle with Docker Compose, point the worker at one source, run each stage once, and ask the api a question. The steps use the Telegram source. The other tier-1 sources you can set up today, `wakapi` and `dawarich`, follow the same pattern ([Connect a source](sources.md)).

## Before you start

- **Docker with Compose v2.** Everything below runs through `docker compose` from the checkout.
- **Memory.** [compose.yaml](../../compose.yaml) limits the resident services to 2560m (api), 2g (database) and 256m (MCP server). The worker gets 8g while it runs and exits when its batch is done.
- **Network on the first run.** The image build installs PyTorch and the rest of [pyproject.toml](../../pyproject.toml). The first `embed` downloads BGE-M3, about 2.2 GB by the comment in compose.yaml, into the `chronicle_models` volume. The api's first query fetches the ONNX export of the same model, at a pinned revision, if the volume does not hold it yet ([embed.py](../../chronicle/embed.py)).
- **One source.** Here, the SQLite database of a Telegram sync service. The adapter reads its `messages`, `chats` and `chat_tags` tables; the columns it expects are in [telegram.py](../../chronicle/adapters/telegram.py), and the `telegram_db` fixture in [test_adapters.py](../../tests/test_adapters.py) builds a minimal database with that schema.

## Configure

```bash title="Clone and copy the settings"
git clone https://github.com/Egoushka/chronicle.git && cd chronicle
cp .env.example .env
```

Edit `.env`:

| Variable | Set it to |
|---|---|
| `DB_PASSWORD` | any strong password; Compose refuses to start without it |
| `TELEGRAM_DB_URL` | keep `/srv/telegram/telegram.db`, the path inside the worker |
| `CHRONICLE_OWNER` | optional: your name; only enrichment reads it |

Leave the other source blocks as they are. A source whose variables are incomplete is reported as not configured and skipped ([doctor.py](../../chronicle/doctor.py)).

Then tell Compose where the database lives on the host. Create `compose.override.yaml` next to `compose.yaml`; Compose merges it automatically, and git ignores it:

```yaml title="compose.override.yaml"
services:
  chronicle-worker:
    volumes:
      - /path/to/telegram-sync/data:/srv/telegram
```

Replace `/path/to/telegram-sync/data` with the directory that holds `telegram.db`. The mount is read-write on purpose. A SQLite file in WAL mode cannot be opened through a `:ro` mount, and the adapter's `mode=ro` URI is what keeps it read-only ([compose.sources.example.yaml](../../compose.sources.example.yaml)).

## Start the stack

```bash title="Start the database, the api and the MCP server"
docker compose up -d chronicle-db chronicle-api chronicle-mcp
curl -s http://127.0.0.1:8030/health
```

The first build takes a while. On its first start, PostgreSQL runs every file in [migrations/](../../migrations) from `/docker-entrypoint-initdb.d`, and it does so only while the data directory is empty. `/health` answers `{"ok":true,"version":"0.3.0"}` once the api reaches the database.

## Check the source

```bash title="Validate the source before ingesting"
docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker doctor --tier 1
```

`doctor` reads a sample of up to 25 recent events from each configured source, read-only, and checks it: real timestamps, ascending order, unique ids, non-empty text, a thread key per conversation ([What doctor checks](sources.md#what-doctor-checks)). You want a `✓` line for `telegram`. The other tier-1 sources show `·`, not configured, and the summary reads `1 ok · 0 warn · 0 fail · 3 not configured`.

`doctor` exits 1 on a failure or a source conflict. Fix the adapter's input before you ingest: a wrong adapter writes wrong rows for as long as it runs.

## Ingest and segment

```bash title="Ingest, fit the gaps, cut segments"
docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker ingest --tier 1
docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker fit-gaps
docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker segment
```

`ingest` writes one row of `event` per message, from personal chats with bot chats left out ([Connect a source](sources.md#tier-1-core)). `fit-gaps` measures, for each chat with at least 100 messages, the silence that separates two conversations; smaller chats use 30 minutes. `segment` cuts every chat into segments at its gap.

Read a sample before you embed. If segmentation is wrong, everything downstream inherits it ([DEPLOY.md](../DEPLOY.md#backfill-order)):

```bash title="Print 20 random segments"
docker compose exec chronicle-db psql -U chronicle -d chronicle -c \
  "SELECT thread_key, started_at::date, event_count, left(raw_text, 160) FROM segment ORDER BY random() LIMIT 20"
```

Look for segments that run unrelated conversations together, or cut one exchange in the middle ([first-deploy.sh](../../scripts/first-deploy.sh)).

## Embed

```bash title="Encode the segments"
docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker embed
```

`embed` encodes every segment that has no embedding, newest first, 500 at a time (`BATCH_SIZE`), on the CPU. It is resumable: an interrupted run continues where it stopped. On the reference host, 51,044 segments took about 9.8 hours on 16 contended cores ([DEPLOY.md](../DEPLOY.md#backfill-order)). At the end it refreshes the term statistics that the keyword half of the search reads.

> [!TIP]
> `python -m chronicle.worker all --tier 1` runs `ingest`, `fit-gaps`, `segment`, `enrich` and `embed` in that order. The nightly job runs the same command at tier 2; enrichment is off by default, so `enrich` does nothing.

## Ask a question

```bash title="Search the archive"
curl -s http://127.0.0.1:8030/recall \
  -H 'Content-Type: application/json' \
  -d '{"query": "what did we decide about the trip", "limit": 5}'
```

The first query after a start loads the query encoder, so it is slow; the api loads it lazily, not at boot ([embed.py](../../chronicle/embed.py)). The answer is JSON:

| Field | Meaning |
|---|---|
| `intent` | the kind of question `route()` recognised; a label only |
| `routed_because` | the pattern that matched, or `null` |
| `window_from_query` | the date range the question named, or `null` |
| `results` | segments, best first |

Each result has `segment_id`, `score`, `date`, `thread`, `text` (the segment's first 2,000 characters), `evidence` (its `source:source_id` event ids) and `summary` (`null` unless enrichment ran). The [HTTP API reference](reference.md#http-api) covers every route.

You can also open `http://127.0.0.1:8030/` in a browser. The api serves a small search page there ([ui.html](../../chronicle/ui.html)).

## Connect an assistant

The MCP server speaks the SSE transport at `http://127.0.0.1:8031/sse`. Point an MCP client that supports SSE at that URL. It gets seven tools: `recall`, `first_mention`, `evolution`, `tally`, `timeline`, `open_commitments` and `ground` ([MCP tools](reference.md#mcp-tools)).

Both ports are published on loopback only. From another machine, tunnel them over SSH:

```bash title="Reach the api and the MCP server from another machine"
ssh -N -L 8030:127.0.0.1:8030 -L 8031:127.0.0.1:8031 <host>
```

> [!IMPORTANT]
> Nothing authenticates. Never publish 8030 or 8031 beyond loopback, and never attach these services to a shared proxy network ([compose.yaml](../../compose.yaml), [SECURITY.md](../../SECURITY.md)).

## If something fails

| Symptom | What to do |
|---|---|
| `set DB_PASSWORD in chronicle/.env` | set `DB_PASSWORD` in `.env` |
| doctor: `unable to open database file` | fix the host path; never mount a WAL file `:ro` |
| doctor: `not configured` for telegram | set `TELEGRAM_DB_URL` in `.env` |
| `/recall` returns no results | run `embed`; only embedded segments are searched |
| the first `/recall` is slow | the encoder loads on first use; ask again |
| `/tally` answers 503 | migration 004 has not run |
| tables missing after the first start | start over with empty volumes, below |

The migrations run only on an empty data directory, so a first start that failed halfway leaves them unapplied for good. Remove the volumes and start again ([DEPLOY.md](../DEPLOY.md#install)):

```bash title="Start over with empty volumes"
docker compose down -v
docker compose up -d chronicle-db chronicle-api chronicle-mcp
```

> [!CAUTION]
> `docker compose down -v` deletes the `chronicle_pg` volume, with every event and segment in it, and the `chronicle_models` model cache. Use it only on a fresh install.

Next: [connect more sources](sources.md), or [schedule the nightly run](operate.md#schedule-the-nightly-run).
