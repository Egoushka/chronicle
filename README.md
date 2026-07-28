# chronicle

Personal event store and retrieval brain. Ingests Telegram, wakapi, dawarich
(and whatever comes next) into one timeline, and serves it to `agent-runner`
over MCP so the assistant can actually look things up.

Deploys into [`homelab-gitops`](../homelab-gitops) as the `chronicle` stack.

---

## Why this exists

The Telegram archive is 681,331 messages across 487 chats and 457 senders,
2018-12-30 to now. The existing `telegram-sync` stack embeds **every message**
into Qdrant. Measured against the real corpus:

| | |
|---|---|
| messages under 20 chars | **65.0%** (442,954) |
| messages under 60 chars | **94.0%** |
| messages over 200 chars | **1.5%** |
| messages with `reply_to_id` | **7.9%** |

So ~442,000 of those vectors represent `ок`, `ага`, `+1`, `да`, `😂`. They are
not retrieval units — they crowd every neighbourhood they land in and bury the
1.5% of messages that carry a proposition.

**Chronicle's core move is aggregation before indexing.** Events are grouped
into *episodes* by a per-thread fitted time gap: 681k units become ~50k. The
index gets ~11× smaller and retrieval gets better at the same time.

Evidence, from SeCom (ICLR 2025), measuring retrieval quality by memory unit
on conversational data — at ~30 tokens/turn, where ours are 5–10:

```
segment-level   71.57   <- what this builds
turn-level      65.58
session-level   63.16
summaries       53.87-56.25   <- worst. Do not build a summary pyramid.
```

Full reasoning, benchmarks and citations: [`docs/RESEARCH.md`](docs/RESEARCH.md).

## It is not a Telegram tool

Telegram is the richest channel, not the only one. **18 adapters**, five
storage shapes, one `Event` shape downstream.

| tier | sources | why |
|---|---|---|
| **1 · core** | `telegram` `wakapi` `dawarich` `calendar` | the archive is worth having with only these |
| **2 · behaviour** | `firefly` `lastfm` `forgejo` `jira` | what you *did*, as opposed to what you *said* |
| **3 · artifact** | `immich` `paperless` `gmail` `notion` `karakeep` `github` `linkedin` `slack` | things you made, saved, or were sent |
| **4 · ambient** | `miniflux` `owntracks` | weak signal; on last, off first if precision drops |

Storage shapes, because the homelab is not uniform and assuming it was is how
the first version broke: **PostgreSQL** (telegram, immich, paperless,
miniflux, forgejo, dawarich) · **MariaDB** (firefly) · **SQLite** (wakapi,
karakeep) · **flat JSONL files** (owntracks) · **HTTP/MCP** (gmail, calendar,
notion, slack, jira, linkedin, lastfm, github).

### The policy layer is the point

"Use all possible channels" has a failure mode that looks exactly like the one
Chronicle was built to fix. Indexing 681k sub-20-char messages was the wrong
**unit**. Turning on twenty sources without a policy is the wrong **source
mix** — miniflux alone can contribute 100k article rows you never opened,
immich has 400 near-identical burst frames per moment, and the archive becomes
millions of events that are ~90% chaff. Precision collapses the same way.

So every source declares a **density**, and density decides how hard the
adapter aggregates *before* anything reaches the episode layer:

- `NARRATIVE` — deliberate human text. Segmented into episodes.
- `DISCRETE` — one row really is one thing that happened. Passed through.
- `TELEMETRY` — meaningful only in aggregate. **The adapter rolls it up**:
  wakapi heartbeats → coding sessions, dawarich points → stays, lastfm
  scrobbles → listening sessions, immich photos → photo sessions.
- `AMBIENT` — stored, but kept off the default retrieval surface.

`chronicle/sources.py` also lists the ~40 homelab stacks that are explicitly
**not** sources, so the boundary is documented rather than rediscovered. All
57 stacks is not the goal; monitoring, qdrant and vaultwarden describe the
machine, not the life.

`make test` enforces this: every adapter must have a policy, densities must
match, and `dawarich`/`owntracks` are flagged as mutually exclusive (same GPS
signal — enabling both double-counts every trip and the duplicate reads as
corroboration).

### Why more channels actually helps

*"What was happening in the months before things went wrong?"* Telegram
tells you what you **said**. Location tells you whether you stopped leaving
the house, wakapi whether you stopped coding, firefly whether spending
changed, lastfm what you played at 3am, immich whether you stopped taking
photos. **The behavioural signals are more honest than the conversational
ones, because you don't curate them.**

And cross-source corroboration turns a guess into evidence: a trip mentioned
in Telegram, confirmed by dawarich coordinates, photographed in immich, and
paid for in firefly is a fact you can trust.

## Relationship to Hindsight

They are opposites, which is why they compose.

| | Hindsight | Chronicle |
|---|---|---|
| origin | you decided it mattered | you never chose to save any of it |
| volume | ~5,200 facts | 681k events / ~50k episodes |
| precision | high, curated | low, exhaustive |
| evidence trail | none | nothing *but* evidence |
| shape | a notebook you write in | a recording that ran the whole time |

**Chronicle does not replace Hindsight and must not flood it.** The `personal`
bank is already at 2,726 facts and times out on `sync_retain`; piping ~50k
episodes of extracted facts into it would 40× the bank and make `recall`
useless.

Two narrow flows instead:

- **`ground` (Chronicle → answer), every recall.** Hindsight facts are
  unsourced assertions. Chronicle attaches the conversations behind them —
  including ones that contradict. This replaces time-based staleness rules
  (ticket >14d, finance >30d) with a measurement.
- **Promotion (Chronicle → Hindsight), rare.** `v_promotable_facts` requires
  support across ≥3 episodes **and** ≥2 threads at confidence ≥0.7. Target
  hundreds per year. Watch `get_bank_stats` after each run.

## Architecture

```
sources ──► adapters ──► event (immutable, partitioned by year)
                            │
                            ▼
                     SEGMENTATION            per-thread fitted time gap
                     681k ──► ~50k           + caps + reply-edge anchors
                            │
                            ▼
                     episode                 raw_text (returned)
                            │                embed_text (indexed)
                            ▼
                     PostgreSQL              halfvec(1024) exact scan
                     one system              tsvector + trgm + B-tree
                            │
                            ▼
                     MCP ──► agent-runner ──► tg-assistant
```

Everything is in one PostgreSQL. At ~50k episodes for one user, ANN solves a
problem that doesn't exist: exact cosine over ~123 MB is single-digit ms, and
it keeps every date filter exact — sidestepping the HNSW percolation failure
that bites hardest at the ~1%-cardinality date ranges you query most.

## Quick start

```bash
git clone <this repo> chronicle && cd chronicle
make test                     # 57 unit tests, no DB or models needed
cp .env.example .env          # fill in, then `make encrypt STACK=chronicle` in homelab
make smoke                    # migrations + every SQL function, throwaway DB

make doctor                   # ← ALWAYS. validates sources before you ingest
make ingest                   # sources -> event -> episode -> embedding
make eval-init && make eval   # chronicle vs ripgrep, on your questions
```

### `make doctor` is not optional

Two adapter assumptions were already wrong on first contact — wakapi is SQLite
not Postgres, firefly is MariaDB with amounts on `transactions` — and each
would have surfaced hours into a backfill, after the worker had written wrong
rows. Doctor finds that class of problem in ~10 seconds, read-only.

It checks: driver reachability, timestamps that are actually datetimes (SQLite
returns TEXT), ascending order, duplicate ids, unaggregated telemetry, empty
narrative text, degenerate `thread_key`, and import-time-masquerading-as-
event-time (the immich `createdAt` vs EXIF `dateTimeOriginal` trap). Run it
again after upgrading any source stack — an upstream migration is exactly what
breaks an adapter quietly.

Deploy: see [`docs/DEPLOY.md`](docs/DEPLOY.md). Short version — clone into
`/srv/stacks/chronicle`, resolve the `chronicle-db` digest into `PINS.md`,
register `10.211.71.0/24` in `NETWORKS.md`, `make validate` in the homelab
repo, then `docker compose up -d`.

## Requirements

- PostgreSQL 16 with **pgvector ≥ 0.7** — `halfvec` does not exist in 0.6.
  The pinned `pgvector/pgvector:pg16` image is fine; a distro `postgresql-16-pgvector`
  package may not be.
- ~2.5 GB resident (api + db). The worker is `restart: "no"` and needs 8 GB
  **while it runs** — the box is 32 GB with 61.4 GB of `mem_limit` committed
  and has hit 96% swap, so it is scheduled, not resident.

## Status

Working: segmentation, gap fitting, cross-script entity resolution, intent
routing, RRF fusion, bi-temporal facts with deterministic conflict resolution,
the full schema, the source-policy layer, 18 adapters across 5 storage shapes,
MCP tool surface.

The SQL-backed adapters (telegram, wakapi, dawarich, immich, paperless,
firefly, karakeep, miniflux, forgejo) carry real queries against real schemas
but have only been run against fixtures — verify each one against your data
before trusting its output. The API adapters take an injected `fetch_page`
callable and need wiring to the corresponding MCP tool.

Stubbed: only `worker.py enrich` — the local-LLM pass for summaries, topics and
facts. Everything else runs. Enrichment is deliberately last: retrieval works
without it, so ship and measure before spending weeks of CPU there.

Tested: 57 unit tests, plus migrations and every SQL function exercised
against real PostgreSQL 16 + pgvector 0.8.0 in CI.
