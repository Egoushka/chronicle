# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions
follow [ROADMAP.md](ROADMAP.md): the minor number is the roadmap goals done,
the patch number is fixes in between. A release that needs anything beyond
pulling the code and restarting — a migration, a new setting, a backfill —
says so under **Upgrade**.

## [Unreleased]

### Changed
- README states the 2026-09-29 eval (71 questions: chronicle 71.1% vs ripgrep
  54.2%, 68.4% with answer words), not the 2026-09-26 one (63.5% vs 62.8%, which
  had leaked answer words into grep's keywords), and no longer calls retrieval
  level with grep.
- CLAUDE.md records the segment cap sweep (roadmap goal 5): cap 15 scores
  73.2% vs 71.1% at 30 on the eval threads; no code or default changes yet.

## [0.3.0] - 2026-09-29

Goal 0.4 of ROADMAP.md: chronicle beats ripgrep by 10+ points on its
owner's questions, measured honestly. Goal 0.3 (secret redaction) is still
open; the minor number counts goals done, not the highest one.

### Changed
- The grep baseline's keywords are the question's own words only — stems,
  other spellings, the RU/UK form of the same word, never a word from the
  answer (`Question.keywords` in `chronicle/evaluate.py`). On 71 questions:
  chronicle 71.1% vs ripgrep 54.2%; grep with answer words scores 68.4%.

## [0.2.0] - 2026-09-29

Goal 0.2 of ROADMAP.md: a deployment says which release it runs.

### Added
- `chronicle.__version__`, reported by the api's `/health`, so a deployment
  says which release it runs.
- This changelog and [ROADMAP.md](ROADMAP.md).
- `worker resegment --thread K --max-messages N` and `SEGMENT_MAX_MESSAGES`,
  to measure the segment cap; `make eval-threads` for its scope.
- Secret redaction at ingest for every source (`chronicle/redact.py`), and
  `make redact-secrets` to rewrite what is already stored.

### Fixed
- Reply edges now suppress segment splits; the worker had disabled the rule.
- Telegram's service account (777000) is excluded; run `make purge-excluded`.
- Eval scores p@1 per segment, routes evolution questions to `/evolution`,
  and `make eval` runs at all (it was a silent no-op).

### Upgrade
- Run `python -m chronicle.redact` in the worker to count stored secrets,
  then `--apply` and `embed`. Rotate anything it finds: redaction removes the
  value from chronicle, not from wherever else it was pasted.

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
