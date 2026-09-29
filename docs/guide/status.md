---
title: "Status and evidence"
description: "Each capability marked works, partial or not yet, with the test, script, ADR or roadmap entry behind the mark, and the decisions on record."
order: 7
section: "Project"
---

Each row names the evidence for its status. `works` means a test in this repository covers it. `partial` means part of it is missing, or nothing in the repository tests it. `not yet` means the code, the README or the roadmap describes it and it is not built. This page describes the `v0.3.0` tag.

## Status

| Capability | Status | Evidence |
|---|---|---|
| Segmentation: fitted gaps, caps, reply edges | works | [test_segment.py](../../tests/test_segment.py), [resegment-itest.py](../../scripts/resegment-itest.py) |
| A worker that is safe to re-run on a schedule | works | [worker-itest.py](../../scripts/worker-itest.py) |
| Hybrid search: dense, IDF-weighted lexical, RRF | works | [smoke.sql](../../scripts/smoke.sql), [test_migrations.py](../../tests/test_migrations.py) |
| The date window a question names | works | [test_route.py](../../tests/test_route.py) |
| First-mention and per-period search | works | [smoke.sql](../../scripts/smoke.sql) |
| `/tally` with your SQL, as a SELECT-only role | works | [tally-itest.py](../../scripts/tally-itest.py) |
| Secret redaction at ingest, and the backfill | works | [test_redact.py](../../tests/test_redact.py), [redact-itest.py](../../scripts/redact-itest.py) |
| Erasure of what a filter now excludes | works | [purge-itest.py](../../scripts/purge-itest.py), [test_adapters.py](../../tests/test_adapters.py) |
| `doctor` preflight checks | works | [test_adapters.py](../../tests/test_adapters.py) |
| telegram, wakapi, karakeep, owntracks, lastfm | works | [test_adapters.py](../../tests/test_adapters.py) |
| Bi-temporal facts resolved by `max(version)` | works | [smoke.sql](../../scripts/smoke.sql), [worker-itest.py](../../scripts/worker-itest.py) |
| Evaluation harness against ripgrep | works | [test_evaluate.py](../../tests/test_evaluate.py) |
| One version in the package, pyproject and changelog | works | [test_version.py](../../tests/test_version.py) |
| `/timeline`, `/ground`, `/commitments` | partial | [api.py](../../chronicle/api.py) |
| MCP server, seven tools over SSE | partial | [test_mcp_server.py](../../tests/test_mcp_server.py) |
| dawarich, firefly, forgejo, immich, miniflux, paperless | partial | [test_adapters.py](../../tests/test_adapters.py), [README](../../README.md#status) |
| Enrichment: summary, topics, facts, commitments | partial | [worker-itest.py](../../scripts/worker-itest.py), [ROADMAP.md](../../ROADMAP.md) |
| Cross-script entity keys | partial | [test_resolve.py](../../tests/test_resolve.py), [ADR-002](../ADR-002-entity-resolution.md) |
| Browser search page at `/` | partial | [ui.html](../../chronicle/ui.html) |
| calendar, jira, gmail, notion, github, linkedin, slack | not yet | [api_sources.py](../../chronicle/adapters/api_sources.py), [doctor.py](../../chronicle/doctor.py) |
| Ambient sources kept off the default search | not yet | [002_retrieval.sql](../../migrations/002_retrieval.sql) |
| Trips between location stays | not yet | [dawarich.py](../../chronicle/adapters/dawarich.py) |
| People from Telegram sender ids | not yet | [001_core.sql](../../migrations/001_core.sql) |
| Reranking by mentions, participants, importance | not yet | [rank.py](../../chronicle/rank.py) |
| Promotion of recurring facts to a curated memory | not yet | [ROADMAP.md](../../ROADMAP.md#stage-1--someone-else-can-run-it-v1x) |
| A measured segment cap | not yet | [ROADMAP.md](../../ROADMAP.md#stage-0--worth-using-over-grep-for-its-owner-v0x) |
| An alert when a source goes silent | not yet | [ROADMAP.md](../../ROADMAP.md#stage-0--worth-using-over-grep-for-its-owner-v0x) |
| Events dated before 2018 | not yet | [001_core.sql](../../migrations/001_core.sql) |
| Late backfills merged into older segments | not yet | [worker.py](../../chronicle/worker.py), [ROADMAP.md](../../ROADMAP.md#could-have) |
| Release container images | not yet | [ROADMAP.md](../../ROADMAP.md#stage-1--someone-else-can-run-it-v1x) |

## Tests and CI

`make test` runs the unit suite, which needs no database and no model. At this tag it collects 158 tests: 157 pass, and the MCP tool-surface test skips unless `httpx` and the MCP SDK are installed. `make smoke` applies every migration to a throwaway PostgreSQL database, exercises every SQL function with [smoke.sql](../../scripts/smoke.sql), and runs the five integration tests: [purge-itest.py](../../scripts/purge-itest.py), [tally-itest.py](../../scripts/tally-itest.py), [worker-itest.py](../../scripts/worker-itest.py), [resegment-itest.py](../../scripts/resegment-itest.py) and [redact-itest.py](../../scripts/redact-itest.py).

[CI](../../.github/workflows/ci.yml) runs on every pull request and every push to `main`:

- `bash -n` on every script and hook;
- the unit suite on Python 3.12, with only pytest, numpy and ruff installed, so the MCP tool-surface test skips there too;
- `ruff check`, reported but never failing the job;
- `smoke.sh` with the integration tests, against the `pgvector/pgvector:pg16` image;
- gitleaks, and a check that every commit uses a GitHub noreply address.

Nothing in the repository starts the Compose stack or calls the api or the MCP server over HTTP. The api's routes run on the reference deployment, and the evaluation harness calls `/recall`, `/first-mention` and `/evolution` there ([CHANGELOG.md](../../CHANGELOG.md)), but no test here does.

## Limits of what works

- **Hybrid search** scans every segment exactly. [ADR-001](../ADR-001-postgres-not-qdrant.md) expects that to stay cheap until about a million segments. On the reference host a warm `/recall` takes about 0.4 s ([README](../../README.md#running-it)); the first call after a start is slower because the model loads on first use, and the first call after an idle period is slow too ([CLAUDE.md](../../CLAUDE.md#hard-won-facts--do-not-relearn-these), facts 25 and 34).
- **The date window** applies only when a question names exactly one year.
- **Redaction** matches patterns and has no entropy heuristic, so a secret in an unknown format with no label nearby stays ([redact.py](../../chronicle/redact.py)). [ROADMAP.md](../../ROADMAP.md#stage-0--worth-using-over-grep-for-its-owner-v0x) keeps goal 3, "no secret reaches the index, the MCP or an LLM", in progress.
- **Erasure** follows each adapter's `excluded_thread_keys()`, and only `telegram` excludes anything today.
- **Segmentation** caps a segment at 30 events, a number nobody has measured yet; that is roadmap goal 5.
- **Evaluation** depends on a private question set that is not in the repository, so CI cannot run it ([.gitignore](../../.gitignore)).

## What is partial

**`/timeline`, `/ground`, `/commitments`.** No test calls these handlers. `/ground` runs the same `hybrid_search()` that smoke.sql covers. `/commitments` returns rows only if enrichment ran, its `with` field is always null because nothing fills `person`, and the view behind it drops everything younger than 90 days whatever `older_than_days` says.

**MCP server.** The seven tools forward to the api ([mcp_server.py](../../chronicle/mcp_server.py)). The `tally` tool cannot succeed: it sends no SQL, and `/tally` answers 400 without it. The tool descriptions promise a rerank step, per-period LLM summaries and cross-script spelling for first mentions, and the code does none of them ([Reference](reference.md#mcp-tools)). The tool-surface test skips in `make test` and in CI.

**The six database adapters.** No test runs the queries of dawarich, firefly, forgejo, immich, miniflux or paperless. [test_adapters.py](../../tests/test_adapters.py) covers firefly's amount formatting, forgejo's commit-message extraction and dawarich's clustering radius. The README says every database-backed adapter has run against its real application's database on the reference deployment; [doctor-homelab.sh](../../scripts/doctor-homelab.sh) is how that check runs.

**Enrichment.** [worker-itest.py](../../scripts/worker-itest.py) drives it against a stub model: it writes facts, commitments and citations, a second pass replaces the first, and a dead endpoint stops after one batch. It is off by default, and the one live batch lowered the evaluation score, so roadmap goal 7 decides by an A/B whether it stays.

**Entity keys.** [test_resolve.py](../../tests/test_resolve.py) covers both keys, the short-name guard and blocking by chat. The pipeline uses only the phonetic key, only during enrichment, and `resolution_candidates()` has no caller.

**Browser search page.** [ui.html](../../chronicle/ui.html) offers recall, first mention, evolution and timeline. Evolution always shows "no results": the page reads the answer as a list, and `/evolution` returns its bins as an object keyed by date. No test covers the page.

## What is not built

**API adapters.** `calendar`, `jira`, `gmail`, `notion`, `github`, `linkedin` and `slack` take an injected page fetcher that nothing in the repository supplies, so `doctor` reports them as not configured and `ingest` skips them. The calendar source, with OAuth, is a stage-1 goal in [ROADMAP.md](../../ROADMAP.md#stage-1--someone-else-can-run-it-v1x).

**Ambient sources off the default search.** The README, the `Density` docstring and a comment in [001_core.sql](../../migrations/001_core.sql) say ambient sources are stored but kept off the default retrieval surface, and the schema has an index for it, `source_retrievable_idx`. `hybrid_search()` never filters by density and no query uses that index, so `miniflux` segments are searched with the rest.

**Trips.** The README says GPS points become stays and trips. [dawarich.py](../../chronicle/adapters/dawarich.py) and [owntracks.py](../../chronicle/adapters/owntracks.py) emit stays only.

**People.** The schema has `person` (keyed by Telegram user id), `event.person_id` and `segment.participant_ids`, and nothing writes any of them. The same holds for `life_event`, which `v_on_this_day` uses to hide sensitive periods.

**Reranking.** [rank.py](../../chronicle/rank.py) has a Python reciprocal rank fusion and a rerank by entity mentions, graph distance, importance, participant match and recency. Nothing calls either; the fusion that runs is the SQL in `hybrid_search()`.

**Promotion to a curated memory.** `v_promotable_facts`, the `hindsight_*` columns on `fact` and `source.hindsight_bank` are in place. Nothing promotes a fact yet; it is a stage-1 goal.

**A measured segment cap.** Roadmap goal 5, marked next. The tool for the sweep, `resegment`, works ([Deploy and operate](operate.md#rebuild-threads-at-a-new-segment-cap)).

**An alert when a source goes silent.** Roadmap goal 8. Today the only signal is `doctor`'s warning for a source with nothing in 90 days.

**Events dated before 2018.** `event` has no partition before 2018-01-01, so such an event fails its batch ([Connect a source](sources.md#how-a-source-is-wired)).

**Late backfills.** Events older than a thread's last segment are segmented among themselves, never merged into the older segments they fall between ([worker.py](../../chronicle/worker.py)). The roadmap lists this under "could have".

**Release images.** Images are built from the checkout. Stage 1 of the roadmap plans releases that publish container images, so an upgrade becomes a pull and a migration.

## Decisions

Architecture decisions live in `docs/` as `ADR-NNN-slug.md`. The research they draw on is [RESEARCH.md](../RESEARCH.md), and the design rules that follow from it are in [CLAUDE.md](../../CLAUDE.md#design-rules-that-are-not-negotiable).

| ADR | Status | What it decides |
|---|---|---|
| [ADR-001: One PostgreSQL, not Postgres + Qdrant](../ADR-001-postgres-not-qdrant.md) | accepted, 2026-07-25 | vectors in pgvector `halfvec(1024)`, no approximate index |
| [ADR-002: Two-tier cross-script entity resolution](../ADR-002-entity-resolution.md) | accepted, 2026-07-25 | phonetic key, then a consonant skeleton that only proposes |
| [ADR-003: ONNX query encoder](../ADR-003-onnx-query-encoder.md) | accepted, 2026-09-26 | queries use BAAI's fp32 ONNX export; int8 rejected |
