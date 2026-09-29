---
title: "Overview"
description: "What chronicle is, how a chat archive becomes searchable segments, what it leaves out on purpose, and where it stands at v0.3.0."
order: 0
section: "Get started"
---

Chronicle is a self-hosted event store and retrieval layer for one person's archive. It reads a chat archive and the activity streams its owner already self-hosts (coding time, location, spending, listening, photos, documents, bookmarks) into one timeline in PostgreSQL. It groups conversations into segments before anything is indexed, and serves the result to an assistant over [MCP](https://modelcontextprotocol.io) and to you over a local HTTP API. It is written in Python.

## Why segments

Most of a chat archive says nothing on its own. The reference deployment's archive holds 681,331 Telegram messages over seven and a half years: 65.0% of them are under 20 characters, and 1.5% are over 200 ([README](../../README.md#why-segments-not-messages)). Embedded one by one, the short acknowledgements crowd every nearest-neighbour search and bury the messages that say something.

So chronicle embeds conversations, not messages. It cuts each chat at a time gap fitted to that chat, caps each piece, and lets a reply bridge a pause in the exchange it answers. On the reference deployment, 685,401 events became 51,044 segments, about 13 events each ([README](../../README.md#why-segments-not-messages)). The research behind the choice is [docs/RESEARCH.md](../RESEARCH.md).

## How it works

1. **Adapters** read each source read-only and map what they read onto one event shape. A source that only means something in aggregate is rolled up first: editor heartbeats become coding sessions, GPS points become stays, scrobbles become listening sessions.
2. **The worker** runs in batches: `ingest`, `fit-gaps`, `segment`, `enrich` (off by default), `embed`. Every stage is incremental and safe to re-run, so the same command is the nightly job.
3. **The api** answers each kind of question with its own operation: hybrid search for open questions, an earliest-match search for first mentions, per-period search for how something changed, SQL for counting, and a date window for timelines.
4. **The MCP server** exposes those operations to an assistant as seven tools over SSE.

Search results carry the ids of the events they came from, so an answer can be checked against the source. [How it works](how-it-works.md) has the details.

## Who it is for

One person who self-hosts their own data and wants an assistant to look things up in it. The code has one owner (`CHRONICLE_OWNER`) and no users or accounts. Expect to read Python and SQL: the reference deployment is one home server, and the roadmap stage "someone else can run it" has not started ([ROADMAP.md](../../ROADMAP.md#stage-1--someone-else-can-run-it-v1x)).

## What it does not do

- **It does not embed individual messages**, and it builds no summary pyramid. A summary from enrichment is shown next to a hit and never indexed on its own ([enrich.py](../../chronicle/enrich.py)).
- **It never writes to a source.** Adapters only read, and SQLite files are opened with a `mode=ro` URI ([base.py](../../chronicle/adapters/base.py)).
- **It makes no LLM call to answer a question.** The api encodes the question with a local embedding model. The one LLM call in the system is the optional enrichment pass, and it is off by default ([compose.yaml](../../compose.yaml)).
- **No model decides which of two facts is newer.** `resolve_fact_conflicts()` does it with `max(version)` in SQL ([002_retrieval.sql](../../migrations/002_retrieval.sql)).
- **No recency prior, no trained router, no approximate index.** The vector search is an exact scan in PostgreSQL, and no retrieval function discounts old content ([ADR-001](../ADR-001-postgres-not-qdrant.md), [test_migrations.py](../../tests/test_migrations.py)).
- **Nothing authenticates.** The api and the MCP server publish on loopback only ([SECURITY.md](../../SECURITY.md)).

[ROADMAP.md](../../ROADMAP.md#will-not-do) lists what the project will not do: index individual messages, build a summary pyramid, train a query router or let a model decide which fact is newer, apply a global recency prior, keep a resident local LLM on the host.

> [!IMPORTANT]
> The database holds every message of every chat it reads, other people's words included. Reach ports 8030 and 8031 over an SSH tunnel or from another container on the stack's own network, never from a public network ([README](../../README.md#connect-an-assistant)).

## Where it stands

- **Version 0.3.0**, released 2026-09-29 ([CHANGELOG](../../CHANGELOG.md)). The minor number counts roadmap goals done; `/health` reports the version.
- **Roadmap stage 0**, "worth using over grep, for its owner". [ROADMAP.md](../../ROADMAP.md#stage-0--worth-using-over-grep-for-its-owner-v0x) marks goals 1, 2 and 4 done, goal 3 (no secret reaches the index, the MCP or an LLM) in progress, and goal 5 (segment size measured) next.
- **One deployment.** It runs nightly on the owner's host. The README says every database-backed adapter has run against its real application's database there.
- **Not done yet**, in the README's words: the API adapters need a client wired in, and enrichment stays off until an A/B shows it helps. [Status and evidence](status.md) lists every capability with its status and the test behind it, including the places where the code does less than its descriptions say.
- **Supported:** only the latest commit on `main` ([SECURITY.md](../../SECURITY.md)).

How well retrieval does against plain `grep` is measured by the evaluation harness; [Measure it against grep](evaluate.md) explains how.

## Where to go next

- [Quickstart](quickstart.md): from a clone to a first answer from `/recall`.
- [Connect a source](sources.md): what each adapter reads and emits, and the variables it needs.
- [Deploy and operate](operate.md): the nightly run, upgrades, erasure, redaction and rollback.
- [Measure it against grep](evaluate.md): the evaluation harness and how to keep it honest.
- [How it works](how-it-works.md): events, segments, retrieval, facts and entities.
- [Reference](reference.md): MCP tools, HTTP routes, commands, variables and services.
- [Status and evidence](status.md): what works, what is partial, what is not built.
