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

Telegram is the richest source, not the only one. Every adapter turns some
existing homelab Postgres into the same `Event` shape; everything downstream
is source-agnostic.

| adapter | holds | status |
|---|---|---|
| `telegram` | 681k messages | implemented |
| `wakapi` | what you actually coded, by the minute | implemented |
| `dawarich` | where you actually were (PostGIS stays/trips) | implemented |
| `immich`, `paperless`, `firefly`, `karakeep`, `miniflux` | photos, docs, money, bookmarks, reading | interface ready |

This matters most for the questions worth asking. *"What was happening in the months
before things went wrong?"* — Telegram tells you what you **said**. Location, coding
activity and spending tell you what you **did**, and you don't curate those.

Every adapter must be independently droppable. If one rots, Chronicle loses a
source and keeps working.

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
make test                     # 31 unit tests, no DB or models needed
cp .env.example .env          # fill in, then `make encrypt STACK=chronicle` in homelab
make smoke                    # migrations + every SQL function, throwaway DB
```

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
the full schema, three adapters, MCP tool surface.

Stubbed: `api.py`, `worker.py`, `embed.py` — the FastAPI handlers and the batch
enrichment loop. The hard parts are done; these are wiring.

Tested: 31 unit tests, plus migrations and every SQL function exercised
against real PostgreSQL 16 + pgvector 0.8.0 in CI.
