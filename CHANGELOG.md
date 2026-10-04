# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions
follow [ROADMAP.md](ROADMAP.md): the minor number is the roadmap goals done,
the patch number is fixes in between. A release that needs anything beyond
pulling the code and restarting — a migration, a new setting, a backfill —
says so under **Upgrade**.

## [Unreleased]

### Added
- Pre-extraction gate for enrichment (`chronicle/gate.py`): `GATE_BACKEND=none|jev|chat`,
  default `none`. `jev` is TypeSafe's Jev through LiteLLM's `/typesafe`
  pass-through; `chat` is any LiteLLM chat model. Scores land in
  `segment.gate_p` / `gate_version` and are reused. `GATE_SKIP=0` (default) is
  shadow mode: score, still extract. `GATE_SKIP=1` skips segments under
  `GATE_THRESHOLD` (0.42). Replay on 600 enriched segments: 97% of facts and
  commitments kept, 21% of calls skipped.
- `ENRICH_RPM` (default 120) and `GATE_RPM` (default 300) cap calls a minute.

### Upgrade
- Apply `migrations/007_enrich_gate.sql` (`make migrate`): two columns.
- Nothing changes until `GATE_BACKEND` is set. The proxy needs
  `TYPESAFE_API_KEY` and a `GATE_KEY` virtual key first.

## [0.7.0] - 2026-09-29

Goals 6, 7 and 8 of ROADMAP.md: the lookup misses are explained, enrichment is
decided, a silent source is noticed.

### Added
- `GET /freshness`: each source's silence against its own longest gap in the
  past year; `ok` is false when any source is silent (a gatus body condition).
- `evaluate` logs every question with zero recall.
- `use_enrich` on `/recall`, `evaluate --enrich`, `ENRICH_THREADS`: the A/B
  switch for enrichment as a third lexical list.

### Changed
- Enrichment writes `segment.enrich_text` and no longer rewrites `embed_text`
  or drops the embedding. Still off by default: the A/B scored 71.8% against
  75.4% without it (36,635 segments enriched).

### Upgrade
- Apply `migrations/006_enrich_side_index.sql` (`make migrate`): a column, a
  partial index, and `hybrid_search` gains `use_enrich`.

## [0.4.0] - 2026-09-29

Goal 0.5 of ROADMAP.md: segment size is measured, not assumed. The cap was
swept on the eval's threads and the reference archive rebuilt at the winner:
71 questions, chronicle 75.4% (was 71.1%) vs ripgrep 54.2%.

### Changed
- Default events per segment is 15, not 30 (`SEGMENT_MAX_MESSAGES`,
  `segment_chat(max_messages=)`, `make resegment`'s `CAP`). Swept on 46 eval
  threads: 15 -> 73.2%, 20 -> 71.6%, 30 -> 71.1%. Rebuilt archive at 15:
  lookup recall 67.0% -> 75.5%, complete misses 10 -> 7, lookup p@1 unchanged
  (36.2%); the one evolution question that scored went from found to missed.
- `chronicle-db` runs PostgreSQL 18 (`pgvector/pgvector:0.8.6-pg18`, pinned by
  digest; pgvector stays 0.8.6). From 18 the image keeps its data in
  `/var/lib/postgresql/18/docker`, so the volume is a new one, `chronicle_pg18`,
  mounted at `/var/lib/postgresql`. The old `chronicle_pg` stays declared and
  untouched.
- README states the 2026-09-29 eval (71 questions: chronicle 71.1% vs ripgrep
  54.2%, 68.4% with answer words), not the 2026-09-26 one (63.5% vs 62.8%, which
  had leaked answer words into grep's keywords), and no longer calls retrieval
  level with grep.
- CLAUDE.md records the segment cap sweep and the rebuild at 15.

### Upgrade
- A new major cannot read the old data directory. On an existing install, dump
  before switching and restore into the new, empty volume: stop the api,
  `pg_dumpall` from the 16 container, stop it, deploy this compose file, start
  `chronicle-db` alone, restore the dump through the `postgres` database, then
  start the rest. The migrations in `./migrations` run on the empty volume
  first; drop the `chronicle` database they built before restoring, or the
  restore collides with it. A fresh install needs nothing.
- The new cap applies to NEW segments only. To rebuild existing ones, run
  `python -m chronicle.worker resegment --thread K …` in the worker for each
  narrative thread with a segment over 15 events, then `embed`. Rebuilding
  drops those segments' enrichment (facts, commitments, entity mentions,
  summaries); re-run `enrich` afterwards if you use it. Reference archive: 184
  threads, 37,540 -> ~49,300 segments, ~5 h of CPU embedding at ~170/min.

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
