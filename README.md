# chronicle

Self-hosted memory for one person's digital life. Chronicle ingests a chat
archive and the activity streams you already self-host — location, coding
time, spending, listening, photos — into one timeline, groups it into
conversation **segments** before indexing, and serves it to AI assistants over
[MCP](https://modelcontextprotocol.io).

It runs entirely on your host: PostgreSQL with pgvector, a local BGE-M3
encoder, an HTTP API and an MCP server. The one optional cloud call, an
enrichment pass, is off by default.

## Why segments, not messages

Chat archives are mostly filler. Measured on the reference deployment's
archive — 681,331 Telegram messages across 487 chats, seven and a half years:

| | |
|---|---|
| messages under 20 chars | **65.0%** |
| messages under 60 chars | **94.0%** |
| messages over 200 chars | **1.5%** |
| messages with `reply_to_id` | **7.9%** |

Embedding every message spends ~442,000 vectors on `ок`, `ага`, `+1`, `😂`.
They crowd every nearest-neighbour search and bury the 1.5% that say
something.

So chronicle aggregates first. Events group into segments by a time gap
fitted per conversation, with caps and reply edges as anchors: 685,401 events
became 51,044 segments (13.4×), averaging 13 events and 488 characters. The
index shrinks by an order of magnitude and retrieval gets better, which is what
SeCom (ICLR 2025) measured for conversational memory units:

```
segment-level   71.57   <- what chronicle indexes
turn-level      65.58
session-level   63.16
summaries       53.87-56.25   <- worst; chronicle never builds a summary pyramid
```

The research behind it is [docs/RESEARCH.md](docs/RESEARCH.md); the decisions
that followed are the ADRs in [docs/](docs).

## What it does

- **18 source adapters over five storage shapes** — PostgreSQL, MariaDB,
  SQLite, flat JSONL and HTTP APIs — all mapped onto one `Event`.
- **A source policy layer.** Every source declares a density — narrative,
  discrete, telemetry or ambient — and density decides how hard its adapter
  aggregates before anything is indexed: wakapi heartbeats become coding
  sessions, GPS points become stays and trips, scrobbles become listening
  sessions. Twenty sources without a policy is the same mistake as indexing
  every message, one level up.
- **Hybrid retrieval.** Exact cosine over `halfvec(1024)` BGE-M3 embeddings —
  no ANN: at this scale an exact scan is single-digit milliseconds and every
  date filter stays exact — plus a lexical branch weighted by corpus IDF,
  fused with reciprocal rank fusion. A question that names its own period
  ("in January 2026") gets that window applied.
- **Deterministic routing.** Lookup, first mention, change over time and
  counting are different operations with different tools. Routing is rules,
  logged and overridable; there is no trained router and no recency prior.
- **Cross-script entity resolution** for Russian, Ukrainian and Latin
  spellings of one name (Егор / Єгор / Yehor) — see
  [ADR-002](docs/ADR-002-entity-resolution.md).
- **Bi-temporal facts** whose conflicts resolve in code (`max(version)`),
  never by asking a model which fact is newer.
- **Erasure that follows dependencies.** Every projection cites its source
  events, so excluding a chat removes everything derived from it.
- **An evaluation harness** that scores chronicle against `ripgrep` on your own
  questions.

## Sources

| tier | sources | what they add |
|---|---|---|
| 1 · core | `telegram` `wakapi` `dawarich` `calendar` | the archive is worth having with only these |
| 2 · behaviour | `firefly` `lastfm` `forgejo` `jira` | what you *did*, as opposed to what you *said* |
| 3 · artifact | `immich` `paperless` `gmail` `notion` `karakeep` `github` `linkedin` `slack` | things you made, saved or were sent |
| 4 · ambient | `miniflux` `owntracks` | weak signal; first to go if precision drops |

The database-backed adapters read each app's own schema read-only: SQLite is
opened with a `mode=ro` URI, and every query is checked by `doctor` before a
single row is ingested. The Telegram adapter reads the SQLite database of a
Telegram sync service; the schema it expects is in
[`chronicle/adapters/telegram.py`](chronicle/adapters/telegram.py). The API
adapters (calendar, jira, gmail, notion, github, linkedin, slack) take an
injected page fetcher and still need wiring to a client; `lastfm` talks to the
REST API directly.

Behavioural signals are the reason for the extra sources. *"What was happening
in the months before things went wrong?"* Telegram tells you what you said;
location, coding time, spending and photos tell you what you did, and nobody
curates those.

## Quick start

```bash
git clone https://github.com/Egoushka/chronicle.git && cd chronicle
cp .env.example .env                                   # DB_PASSWORD, the owner, one block per source
cp compose.sources.example.yaml compose.override.yaml  # how the worker reaches your sources; trim it
docker compose up -d chronicle-db chronicle-api chronicle-mcp

docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker doctor --tier 1
docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker all --tier 1
```

`doctor` is not optional. It checks driver reachability, timestamps that are
really timestamps (SQLite returns TEXT), ordering, duplicate ids, unaggregated
telemetry, empty narrative text, degenerate thread keys, and import time
masquerading as event time — in about ten seconds, read-only. Every
database-backed adapter here was wrong somewhere the first time it met a real
database, and each would have surfaced hours into a backfill.

`all` runs ingest → fit-gaps → segment → enrich (off by default) → embed.
Every stage is incremental and safe to re-run, so the same command is the
nightly job; `scripts/nightly.sh` wraps it for cron. The full sequence,
including what to check between stages, is [docs/DEPLOY.md](docs/DEPLOY.md).

## Connect an assistant

The MCP server listens on `http://127.0.0.1:8031/sse` (SSE transport).

| tool | answers |
|---|---|
| `recall` | open questions about what was said or happened — hybrid search over segments |
| `first_mention` | when something first came up — an argmin over time, not a similarity search |
| `evolution` | how a view changed — retrieves per time bin so early periods are not crowded out |
| `tally` | counting — runs generated SQL as a SELECT-only role and returns the SQL with the result |
| `timeline` | what was happening in a period, across every source |
| `open_commitments` | promises with no evidence of being kept |
| `ground` | the conversations behind a claim from another memory, including ones that contradict it |

The same operations are HTTP endpoints on `127.0.0.1:8030` (`/recall`,
`/first-mention`, `/evolution`, `/tally`, `/timeline`, `/commitments`,
`/ground`, `/stats`, `/health`). **Nothing authenticates.** Both ports are
published on loopback only; reach them over an SSH tunnel or from another
container on `chronicle_default`, never from a public network.

## Measure it

```bash
make eval-init    # writes the question template; fill in 30-50 real questions
make eval         # scores ripgrep and chronicle on the same questions
```

Write the questions from memory, not by browsing the archive: a question
written after reading the answer is one you already know is findable.

On the reference deployment (37 questions, 2026-09-26), chronicle scores
**63.5%** against ripgrep's **62.8%** — level with grep, not yet clearly ahead,
and grep's keywords were written by someone who had seen the answers. The
remaining misses are vocabulary mismatch: the answer never uses the question's
words.

## Architecture

```
sources ──► adapters ──► event (immutable, partitioned by year)
                            │
                            ▼
                     SEGMENTATION            per-thread fitted time gap
                     681k ──► ~51k           + caps + reply-edge anchors
                            │
                            ▼
                     segment                 raw_text (returned)
                            │                embed_text (indexed)
                            ▼
                     PostgreSQL              halfvec(1024) exact scan
                     one system              tsvector + trgm + B-tree
                            │
                            ▼
                     api + MCP ──► your assistant
```

`raw_text` is what a hit returns; `embed_text` is what gets indexed. Calendar
rollups are views, not tables, because summarising a summary is how archives
rot.

## Running it

- **PostgreSQL 16 or newer with pgvector ≥ 0.7** — `halfvec` does not exist in
  0.6. The pinned `pgvector/pgvector:0.8.6-pg18` image is fine; a distro package
  may not be.
- **Memory.** api 2.5 GB, db 2 GB, resident. The worker needs 8 GB **while it
  runs** and exits when done, so it is scheduled, not resident.
- **Throughput.** On the reference host (16 contended CPU cores) the first
  embed of 51k segments took ~9.8 h; a warm `recall` takes ~0.4 s.

## Pairing with a curated memory

Chronicle is an evidence layer: everything, sourced, uncurated. A curated
memory such as [Hindsight](https://github.com/vectorize-io/hindsight) is the
opposite: few facts, chosen deliberately, no evidence trail. They compose
through two narrow flows. `ground` attaches the real conversations behind a
curated claim on every recall. Promotion goes the other way rarely:
`v_promotable_facts` requires support across ≥3 segments **and** ≥2 threads at
confidence ≥0.7, because piping every extracted fact into a curated store
drowns it.

## Development

```bash
make test     # 110 unit tests; no database, no models
make smoke    # every migration and SQL function, plus three integration tests, against real PostgreSQL
make lint
```

Before committing, enable the hooks: `git config core.hooksPath .githooks`,
then copy `.private-terms.example` to `.private-terms` and list what must never
appear here. The hooks block private terms in files and commit messages, run
gitleaks, and require a GitHub noreply address; CI checks the same.

## Status

Working: segmentation, gap fitting, cross-script entity resolution, intent
routing, RRF fusion, bi-temporal facts, the source policy layer, 18 adapters,
doctor, the worker, the API, the MCP server, the evaluation harness and the
erasure path. Every database-backed adapter has run against its real
application's database on the reference deployment.

Not done: the API adapters need a client wired in; enrichment works but stays
off until an A/B shows it helps; retrieval is level with grep, not ahead of it.

What comes next and in what order: [ROADMAP.md](ROADMAP.md). What changed in
each release: [CHANGELOG.md](CHANGELOG.md).

## Security and privacy

A chat archive holds other people's words as well as yours, and chronicle's
database is the most sensitive data on the host. See [SECURITY.md](SECURITY.md)
for what to report and how. You are responsible for following the privacy laws
where you run it.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Not affiliated with
or endorsed by Telegram or any service it reads.
