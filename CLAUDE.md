# CLAUDE.md — chronicle

Context for agents and contributors. The deployment-specific half — host,
schedule, stacks, open operational items — lives in `CLAUDE.local.md`
(gitignored) on a machine that runs chronicle. User-facing docs: `README.md`.

## What this is

A self-hosted personal event store and retrieval layer. It ingests a chat
archive and the activity streams its owner already self-hosts into one
timeline, groups events into segments before indexing, and serves the result
to an assistant over MCP.

## The one idea everything follows from

The reference archive was measured live, not assumed:

| | |
|---|---|
| messages | **681,331** across 487 chats, 457 senders |
| span | 2018-12-30 → present (7.6 years, ~245/day) |
| under 20 chars | **65.0%** (442,954) |
| under 60 chars | **94.0%** |
| over 200 chars | **1.5%** (10,484) |
| with `reply_to_id` | **7.9%** |
| voice + video notes | 18,638 · photos 12,100 · documents 2,113 |

The reference deployment's Telegram exporter embeds **every message** into
Qdrant, so ~442,000 of its vectors represent `ок`, `ага`, `+1`, `да`, `😂`. They
crowd every ANN neighbourhood and bury the 1.5% that carry a proposition.

**The core move is aggregation before indexing.** Events group into *segments*
by a per-thread fitted time gap: 681k units → ~50k. The index gets ~11×
smaller and retrieval gets better simultaneously.

Evidence (SeCom, ICLR 2025, retrieval quality by memory unit, at ~30
tokens/turn where ours are 5–10):

```
segment-level   71.57   <- what this builds
turn-level      65.58
session-level   63.16
summaries       53.87-56.25   <- WORST. Never build a summary pyramid.
```

Full reasoning and citations: `docs/RESEARCH.md` (16k words).

## Commands

```bash
make test                 # 195 unit tests; bootstraps .venv, no DB or models
make smoke                # migrations + every SQL function, throwaway DB
make doctor               # validate sources BEFORE ingesting  ← always first
make ingest               # sources -> event -> segment -> embedding
./scripts/nightly.sh      # ON THE BOX: what cron runs — all stages, tier 2
make eval-init && make eval   # chronicle vs ripgrep on real questions
make resegment CAP=15 THREADS="$(make -s eval-threads)"   # a cap sweep's rebuild
make purge-excluded       # what the filters now exclude but already indexed
make purge-excluded APPLY=1   # ...and delete it, in one transaction
make redact-secrets       # count stored events holding a secret (APPLY=1 rewrites)
```

`make smoke` also runs `scripts/purge-itest.py`, which exercises the erasure
path against a throwaway database: both event partitions, all four `segment`
edges, `life_event`'s bare TEXT[], `projection_dep`, and the audit row. It is
the only test that can catch facts 29-31 and it has caught two of them.
`scripts/worker-itest.py` runs the worker five times against a changing
telegram fixture and a stub LLM: re-runs create nothing, conversations extend
across runs, transcripts reach segments, enrich replaces rather than
accumulates, a dead endpoint stops after one batch. Facts 37-38 live there.
`scripts/resegment-itest.py` rebuilds threads at a new cap under facts,
commitments and citations (fact 31 again, from the other side), and pins the
reply-edge rule (fact 48).

Requires PostgreSQL 16 + **pgvector ≥ 0.7** — `halfvec` does not exist in 0.6.
The distro `postgresql-16-pgvector` package may ship 0.6; the pinned
`pgvector/pgvector:pg16` image is fine.

## Hard-won facts — do not relearn these

Each cost a debugging cycle. Regression tests exist for all of them; if a test
named after one starts failing, the fix is being undone.

Path-scoped rules in `.claude/rules/` hold the facts, loaded when matching files are touched:
`adapters` (facts 1-5, 10-19, 39-41, 43, 52; source policy), `segmentation` (6-9, 44, 48),
`retrieval` (20, 24-26, 34, 35, 45-47, 49), `mcp` (21-23), `erasure-privacy` (27-33, 36, 50, 51),
`worker` (37, 38, 42). `status-eval` holds Status and the eval history; `memory-promotion` the Hindsight section.

## Design rules that are not negotiable

- **Never index individual messages.** 65% are under 20 chars. This is the
  whole point of the project.
- **Never let an LLM decide which of two facts is newer.** `max(version)` in
  code. LLM adjudication loses 14 points from 64K→262K context
  (arXiv:2606.01435). Telegram timestamps make this free.
- **Never apply a global recency prior.** Solr-style `recip()` is an 8.6×
  penalty on 2018 content across a 7.6-year span. Archival queries are the
  interesting ones. Decay is OFF unless intent detection finds no temporal
  anchor.
- **Never train a query router.** Adaptive-RAG's learned classifier scores
  54.52% on a 3-way task; in ablation a router LOST to fixed hybrid by 1.8 EM.
  Routing is deterministic, logged, and user-overridable.
- **Cap the agentic loop at 2 iterations.** 5→2 costs −0.3 EM at 1/5 latency.
- **`raw_text` is what you return; `embed_text` is what you index.** Facts
  concatenate with the original, never replace it (LongMemEval: condensed
  forms alone do *not* improve recall).
- **Every projection cites `source_event_ids` and is refcounted.** Deletion
  without a dependency graph leaves shards ("backflow").
- **Calendar rollups are VIEWS, not tables.** Summarizing a summary is how
  archives rot.
- **Adapters are read-only and independently droppable.** `ingest_all` catches
  per-adapter; one broken source must never fail the run.

## Versions and releases

One version, in `chronicle/__init__.py` and `pyproject.toml`
(`tests/test_version.py` keeps them equal and requires a CHANGELOG section
for it); `/health` reports it. The minor number is roadmap goals done
(ROADMAP.md), the patch number is fixes between them. Every PR adds a line
under `## [Unreleased]` in CHANGELOG.md, and one that needs a migration, a
new setting or a backfill says so under **Upgrade**. To release: move
Unreleased into a dated `## [X.Y.Z]` section, bump both version strings in
the same commit, merge, tag `vX.Y.Z` on main, and publish a GitHub release
from that section. A deployment runs a tag, never a working tree.

## Working style for this repo

- Investigate before changing; evidence for every claim (file/line/snippet).
- Do not add dependencies casually — this runs on a box with no RAM headroom.
- Every non-obvious decision gets a comment explaining *why*, with the number
  behind it. The codebase is dense with these deliberately; the reasoning is
  the expensive part, not the code.
- New ADRs go in `docs/` as `ADR-NNN-slug.md`.
