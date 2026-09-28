# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions
follow [ROADMAP.md](ROADMAP.md): the minor number is the roadmap goals done,
the patch number is fixes in between. A release that needs anything beyond
pulling the code and restarting — a migration, a new setting, a backfill —
says so under **Upgrade**.

## [Unreleased]

### Added
- `chronicle.__version__`, reported by the api's `/health`, so a deployment
  says which release it runs.
- This changelog and [ROADMAP.md](ROADMAP.md).
- `worker resegment --thread K --max-messages N` and `SEGMENT_MAX_MESSAGES`,
  to measure the segment cap; `make eval-threads` for its scope.

### Fixed
- Reply edges now suppress segment splits; the worker had disabled the rule.
- Telegram's service account (777000) is excluded; run `make purge-excluded`.
- Eval scores p@1 per segment, routes evolution questions to `/evolution`,
  and `make eval` runs at all (it was a silent no-op).

## [0.1.0] - 2026-09-29

First public release: the state of the reference deployment when the
repository went public.

### Added
- Event store on PostgreSQL 16 + pgvector (migrations 001-005): events,
  segments, bi-temporal facts, commitments, entities, erasure log.
- Segmentation by a per-thread fitted time gap; incremental and safe to
  re-run (hard-won facts 37-38).
- 18 adapters behind a source policy (density, tier, Hindsight bank), with
  `doctor` to validate a source before ingesting it.
- Hybrid retrieval: dense BGE-M3 + IDF-weighted lexical, fused by RRF, with
  the question's own date window (facts 46-47).
- HTTP api (`/recall`, `/first-mention`, `/evolution`, `/tally`, `/stats`)
  and an MCP server (mcp 2.x) over the same functions.
- Enrichment through any OpenAI-compatible endpoint, off by default.
- Erasure: `make purge-excluded` deletes what an adapter's filters now
  exclude, with an `erasure_log` row per run.
- Evaluation harness: chronicle vs ripgrep on your own questions.
