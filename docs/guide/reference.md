---
title: "Reference"
description: "Every MCP tool, HTTP route, worker command, make target, environment variable, Compose service and database object, with defaults."
order: 6
section: "Reference"
---

## MCP tools

The MCP server is `python -m chronicle.mcp_server` ([mcp_server.py](../../chronicle/mcp_server.py)), built from [Dockerfile.mcp](../../Dockerfile.mcp) on the MCP Python SDK 2.x (`mcp>=2.2,<3`). It serves the SSE transport, `/sse` and `/messages/`, on port 8031: bound to `0.0.0.0` inside its container and published on `127.0.0.1:8031`. Nothing authenticates.

Each tool forwards its arguments to the api at `CHRONICLE_API_URL`, waits up to 120 seconds, and returns the api's JSON body as text. An error status from the api becomes an error from the tool.

| Tool | Parameters, with defaults | Forwards to |
|---|---|---|
| `recall` | `query`, `date_from`, `date_to`, `source`, `limit` (20) | `POST /recall` |
| `first_mention` | `term` | `POST /first-mention` |
| `evolution` | `topic`, `bin_width` (`"3 months"`) | `POST /evolution` |
| `tally` | `question` | `POST /tally` |
| `timeline` | `date_from`, `date_to`, `sources` | `POST /timeline` |
| `open_commitments` | `older_than_days` (90) | `POST /commitments` |
| `ground` | `claim`, `limit` (10) | `POST /ground` |

> [!WARNING]
> The `tally` tool cannot succeed. It sends only `question`, `/tally` answers 400 without `sql`, and nothing in chronicle writes SQL from a question ([api.py](../../chronicle/api.py)). Call `POST /tally` with your own SQL instead.

The descriptions an assistant reads for these tools promise more than the code does. `recall` says "hybrid + rerank"; no rerank step runs. `evolution` says it summarises each period with about 30 LLM calls; `/evolution` makes no model call and returns ranked segments per period. `first_mention` says it handles cross-script spellings; it lemmatises the term and does not transliterate. `tally` says it returns the generated SQL; nothing generates SQL.

[test_mcp_server.py](../../tests/test_mcp_server.py) pins the tool names and parameters, and checks that `pyproject.toml` and `Dockerfile.mcp` pin the same SDK range. The tool-surface test skips unless `httpx` and the SDK are installed.

## HTTP API

The api is `uvicorn chronicle.api:app` on port 8030 ([api.py](../../chronicle/api.py)), published on `127.0.0.1:8030`. Nothing authenticates. Bodies are JSON, and dates are ISO 8601 (`2024-01-31` or `2024-01-31T09:00:00Z`).

| Route | Body | Answers |
|---|---|---|
| `GET /` | none | the search page, [ui.html](../../chronicle/ui.html) |
| `GET /health` | none | `ok` and `version`; 503 if the database is down |
| `GET /stats` | none | `sources` and `segments` |
| `POST /recall` | `query`, `date_from`, `date_to`, `source`, `limit` | `intent`, `routed_because`, `window_from_query`, `results` |
| `POST /first-mention` | `term` | `term`, `patterns_tried`, `candidates`, `note` |
| `POST /evolution` | `topic`, `bin_width` | `topic`, `bins`, `bin_count` |
| `POST /tally` | `question`, `sql` | `question`, `sql`, `row_count`, `rows` |
| `POST /timeline` | `date_from`, `date_to`, `sources` | `from`, `to`, `events` |
| `POST /commitments` | `older_than_days` | `open` |
| `POST /ground` | `claim`, `limit` | `claim`, `evidence`, `note` |

- **`/stats`** lists each source with `density`, `enabled`, `last_ingested_at`, `last_error` and its `events` count, and counts segments: `total`, `embedded`, `enriched`.
- **`/recall`**: `limit` defaults to 20, and `source` keeps segments from one source. Each result has `segment_id`, `score` (the fusion score), `date` (the segment's start), `thread`, `text` (the first 2,000 characters of `raw_text`), `evidence` (`source:source_id` ids) and `summary` (`null` unless enriched). `window_from_query` is `[from, to]` when a year named in the question was applied ([How it works](how-it-works.md#retrieval)).
- **`/first-mention`**: `patterns_tried` holds the term in lower case and its lemma. `candidates` holds up to 20 events, oldest first, each with `source`, `source_id`, `date`, `text` and `thread`.
- **`/evolution`**: `bin_width` is any PostgreSQL interval. `bins` maps each bin's start date to up to 10 segments in rank order, each with `segment_id`, `date`, `text` (the first 800 characters) and `evidence`.
- **`/tally`** answers 400 without `sql`, or with the database's error when the SQL fails, and 503 when the `chronicle_tally` role is missing (migration 004). `rows` holds the first 200 rows, every value as a string; `row_count` counts all of them.
- **`/timeline`** needs both dates. `sources` keeps segments from those sources. It returns every substantive segment that starts in the window, oldest first, with no limit, each with `date`, `sources`, `thread`, `text` (the summary, or the first 300 characters) and `evidence`.
- **`/commitments`**: `older_than_days` defaults to 90. Each entry has `id`, `text`, `with`, `stated_at` and `age_days`. The view behind it already drops commitments younger than 90 days, so a smaller value returns the same list. `with` comes from the `person` table, which nothing fills yet.
- **`/ground`**: `limit` defaults to 10. `evidence` is in time order, each with `segment_id`, `date`, `text` (the first 1,500 characters) and `source_event_ids`.

## Commands

The worker is `python -m chronicle.worker <command>` ([worker.py](../../chronicle/worker.py)). On a Compose deployment, run it as `docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker <command>`.

| Command | What it does |
|---|---|
| `doctor` | runs `chronicle.doctor` at `--tier` |
| `ingest` | reads every configured source up to `--tier` into `event` |
| `fit-gaps` | fits a gap for every narrative thread of 100 or more events |
| `segment` | turns events no segment cites into segments |
| `resegment` | rebuilds the `--thread` threads at `--max-messages` |
| `enrich` | enriches up to `ENRICH_LIMIT` segments |
| `embed` | encodes segments without an embedding, refreshes `lexeme_df` |
| `all` | `ingest`, `fit-gaps`, `segment`, `enrich`, `embed`, in order |

| Option | Default | For |
|---|---|---|
| `--tier` | 1; one of 1 to 4 | `doctor`, `ingest`, `all` |
| `--max-messages` | `SEGMENT_MAX_MESSAGES`, else 30 | `segment`, `resegment`, `all` |
| `--thread` | none; repeat it for more threads | `resegment`, which requires it |

`ingest` exits 0 even when a source fails; the error is in `source.last_error`. `enrich` exits 1 when every call in a batch failed, and `all` then still runs `embed` and exits 1.

The other modules:

| Command | What it does |
|---|---|
| `python -m chronicle.doctor` | validates sources read-only; `--tier N`, `--json` |
| `python -m chronicle.purge` | counts what the filters now exclude; `--apply`, `--source` |
| `python -m chronicle.redact` | counts stored secrets; `--apply` rewrites them |
| `python -m chronicle.evaluate` | `init`, `threads`, `grep`, `chronicle`, `compare` |
| `python -m chronicle.mcp_server` | the MCP server |
| `uvicorn chronicle.api:app` | the api |

`chronicle.doctor` exits 1 on a failure or a source conflict. `chronicle.purge` and `chronicle.redact` change nothing without `--apply`. `chronicle.evaluate` takes `--dump` (default `eval/dump.tsv`) and `--api` (default `http://localhost:8030`).

## Make targets

The targets run on the machine where you call `make`, with its own `python3`, not in a container; `doctor-homelab` and `eval-homelab` work over SSH on the host that runs the stack ([Makefile](../../Makefile)).

| Target | What it runs |
|---|---|
| `make test` | the unit suite, in a `.venv` with pytest, ruff and numpy |
| `make lint`, `make fmt` | `ruff check`, `ruff format` |
| `make smoke` | migrations and SQL functions on a throwaway database |
| `make migrate` | every migration, in order, against `$CHRONICLE_DB_URL` |
| `make migrate-rename` | 003 then 002, for a database from before the rename |
| `make doctor` | `chronicle.doctor --tier ${TIER:-1}` |
| `make doctor-homelab` | `scripts/doctor-homelab.sh`, over SSH |
| `make ingest` | `chronicle.worker all --tier ${TIER:-1}` |
| `make purge-excluded` | `chronicle.purge`; `APPLY=1` deletes, `SOURCE=` limits |
| `make redact-secrets` | `chronicle.redact`; `APPLY=1` rewrites |
| `make eval-init` | writes the question template |
| `make eval` | `chronicle.evaluate compare` |
| `make eval-threads` | prints the thread keys the gold evidence lives in |
| `make resegment` | `worker resegment` for `THREADS`, at `CAP` (default 30) |
| `make eval-homelab` | `scripts/eval-homelab.sh`, over SSH |
| `make help` | lists the documented targets |

`make smoke` runs [smoke.sh](../../scripts/smoke.sh): it needs `psql` and a PostgreSQL superuser in `PGHOST`, `PGUSER` and `PGPASSWORD`, applies every migration to a throwaway database, runs [smoke.sql](../../scripts/smoke.sql), and then the five integration tests (`purge-itest`, `tally-itest`, `worker-itest`, `resegment-itest`, `redact-itest`) when `psycopg` and `httpx` import.

## Environment variables

Compose passes `.env` to the api, the MCP server and the worker (`env_file`), and reads it to fill in `compose.yaml`.

**The stack.**

| Variable | Default | Read by |
|---|---|---|
| `DB_PASSWORD` | required | compose.yaml, which builds `CHRONICLE_DB_URL` from it |
| `CHRONICLE_DB_URL` | set by compose.yaml | worker, api, purge, redact |
| `BATCH_SIZE` | 500 | worker: events per ingest batch, segments per embed batch |
| `SEGMENT_MAX_MESSAGES` | 30 | worker: events per narrative segment |
| `EMBED_MODEL` | `BAAI/bge-m3` | the encoder |
| `EMBED_BACKEND` | `flag`; compose.yaml sets `onnx` on the api | the encoder |
| `OMP_NUM_THREADS`, `MKL_NUM_THREADS` | 8 in compose.yaml | PyTorch; ONNX Runtime reads the first |
| `CHRONICLE_API_URL` | `http://chronicle-api:8030` | the MCP server |
| `CHRONICLE_EVAL` | `eval/questions.json` | the evaluation harness |

compose.yaml also sets `EMBED_DIM: "1024"` on the api. The code does not read it: the dimension is the constant `EMBED_DIM` in [embed.py](../../chronicle/embed.py). The image sets `HF_HOME` and `SENTENCE_TRANSFORMERS_HOME` to `/models`, the `chronicle_models` volume ([Dockerfile](../../Dockerfile)).

**Enrichment** ([enrich.py](../../chronicle/enrich.py), [worker.py](../../chronicle/worker.py)).

| Variable | Default | Meaning |
|---|---|---|
| `ENRICH_LIMIT` | 0 in compose.yaml; 2000 if unset | segments enriched per run |
| `ENRICH_MODEL` | `gemini-3.5-flash-lite` in compose.yaml | the model name sent to the endpoint |
| `ENRICH_URL` | `http://litellm:4000/v1` in compose.yaml | the endpoint; else `LITELLM_BASE_URL` |
| `LITELLM_API_KEY` | empty | the endpoint's key; without it `enrich` skips |
| `LITELLM_BASE_URL` | `http://litellm:4000/v1` in `.env.example` | the fallback endpoint |
| `ENRICH_CONCURRENCY` | 8 | calls in parallel |
| `CHRONICLE_OWNER` | `the owner` | the owner's name in prompts and facts |
| `CHRONICLE_OWNER_ALIASES` | empty; `me` always counts | sender names that mean the owner |

`.env.example` sets `ENRICH_MODEL=qwen3:8b-q4_K_M`, which overrides the compose.yaml default once copied ([Deploy and operate](operate.md#try-enrichment)).

**Sources** ([doctor.py](../../chronicle/doctor.py)). A source is built only when its required variables are all set.

| Variable | Source | Value |
|---|---|---|
| `TELEGRAM_DB_URL` | telegram | path to the SQLite file, inside the worker |
| `TELEGRAM_EXCLUDE_BOT_CHATS` | telegram | default 1; `0`, `false` or `no` lets bots in |
| `TELEGRAM_EXCLUDE_CHAT_IDS` | telegram | chat ids, separated by commas or spaces |
| `WAKAPI_DB_PATH` or `WAKAPI_DB_URL` | wakapi | path to the SQLite file |
| `WAKAPI_USER` | wakapi | `users.id`, the login |
| `DAWARICH_DB_URL` | dawarich | a PostgreSQL DSN |
| `DAWARICH_USER_ID` | dawarich | `users.id`, a number |
| `FIREFLY_DB_URL` | firefly | `mysql://user:pass@host:3306/db` |
| `LASTFM_API_KEY`, `LASTFM_USER` | lastfm | a read key and the username |
| `FORGEJO_DB_URL` | forgejo | a PostgreSQL DSN |
| `FORGEJO_EMAIL` | forgejo | optional: keep one author's actions |
| `IMMICH_DB_URL` | immich | a PostgreSQL DSN |
| `IMMICH_OWNER_ID` | immich | optional: keep one owner's assets |
| `PAPERLESS_DB_URL` | paperless | a PostgreSQL DSN |
| `KARAKEEP_DB_PATH` | karakeep | path to the SQLite file |
| `MINIFLUX_DB_URL` | miniflux | a PostgreSQL DSN |
| `OWNTRACKS_STORE` | owntracks | the recorder's store directory |
| `NYTKA_DB_URL` | nytka | a PostgreSQL DSN for a read-only role |
| `NYTKA_EXCLUDE_CONVERSATIONS` | nytka | conversation ids to leave out, separated by commas or spaces |

**Scripts.**

| Variable | Read by | Meaning |
|---|---|---|
| `NTFY_URL` | nightly.sh | ntfy topic URL for failure alerts |
| `TIER` | nightly.sh (2); `make doctor`, `make ingest` (1) | the highest tier |
| `INGEST_SECRETS_FILE` | nightly.sh, doctor-homelab.sh | a shared `KEY=value` file with the Last.fm key |
| `CHRONICLE_DOCTOR_HOST` | doctor-homelab.sh, eval-candidates.sh, eval-homelab | the SSH host that runs the stack |
| `CHRONICLE_STACKS` | the same three | the stacks directory on that host |
| `PGHOST`, `PGUSER`, `PGPASSWORD` | smoke.sh, the integration tests | a PostgreSQL superuser |

## Compose services

| Service | Built from | Port | Memory | Restart |
|---|---|---|---|---|
| `chronicle-db` | `pgvector/pgvector:pg16`, pinned by digest | none | 2g | unless stopped |
| `chronicle-api` | `Dockerfile` | `127.0.0.1:8030` | 2560m | unless stopped |
| `chronicle-mcp` | `Dockerfile.mcp` | `127.0.0.1:8031` | 256m | unless stopped |
| `chronicle-worker` | `Dockerfile` | none | 8g | never; `batch` profile |

From [compose.yaml](../../compose.yaml):

- Swap equals the memory limit for every service, so a service at its limit is killed rather than swapped. Every service runs with `no-new-privileges`.
- Health checks: the api answers `/health` (start period 180 s); the MCP server's `/sse` returns status 200; the database answers `pg_isready`. The api and the MCP server carry the label `autoheal=true`.
- The database runs with data checksums, `shared_buffers=512MB`, `work_mem=32MB` and `maintenance_work_mem=256MB`, and mounts `migrations/` at `/docker-entrypoint-initdb.d`.
- Volumes: `chronicle_pg` holds the database, `chronicle_models` the model cache at `/models`.
- The network `default` uses the subnet `10.211.71.0/24`. The worker reaches its sources through `compose.override.yaml`.

## Database

Tables, from [001_core.sql](../../migrations/001_core.sql) and [002_retrieval.sql](../../migrations/002_retrieval.sql):

| Table | Holds |
|---|---|
| `source` | per source: density, tier, resume point, last error |
| `event` | every event, partitioned by year |
| `thread_config` | the fitted gap per thread |
| `segment` | segments: both texts, embedding, enrichment, versions |
| `lexeme_df`, `lexeme_df_meta` | term statistics for the lexical search |
| `fact`, `fact_predicate` | bi-temporal facts, and the closed predicate list |
| `commitment` | promises that enrichment found |
| `entity`, `entity_mention` | named entities, and the segments that mention them |
| `person` | people by Telegram id; nothing writes it yet |
| `life_event` | life events; nothing writes it yet |
| `projection_dep` | the events each fact and commitment came from |
| `erasure_log` | one row per purge or redaction run |

| Function | What it does |
|---|---|
| `hybrid_search` | dense and lexical ranking, fused by RRF |
| `first_mention` | the earliest events matching any pattern |
| `stratified_search` | the nearest segments in each time bin |
| `resolve_fact_conflicts` | closes superseded single-valued facts |
| `refresh_lexeme_df` | rebuilds the term statistics |
| `fit_thread_gaps` | a SQL fallback gap fit; the worker does not call it |

The views are `v_daily`, `v_monthly_topics`, `v_current_facts`, `v_forgotten_commitments`, `v_on_this_day` and `v_promotable_facts`. Only `/commitments` reads one of them. The `chronicle_tally` role can select from every table and view, so `/tally` can reach them all.

| Migration | What it does |
|---|---|
| `001_core.sql` | extensions, the `ru_unaccent` search configuration, the tables |
| `002_retrieval.sql` | the search functions, fact resolution, the views |
| `003_rename_episode_to_segment.sql` | renames `episode` to `segment` on an old database |
| `004_tally_role.sql` | the SELECT-only `chronicle_tally` role |
| `005_incremental.sql` | the index incremental runs need, the 16 predicates |
