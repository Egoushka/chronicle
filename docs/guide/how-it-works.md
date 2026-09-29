---
title: "How it works"
description: "Events, the source policy, how segments are cut, how a question is searched, and what enrichment, facts and entity keys add."
order: 5
section: "Concepts"
---

This page follows one row from a source to an answer. The words it uses are the ones in the code: an **event** is one row from a source, a **thread** is what segmentation runs within, a **segment** is the unit that is embedded, searched and returned.

## The pipeline

1. An **adapter** reads its source and yields events in ascending order of their resume point ([base.py](../../chronicle/adapters/base.py)).
2. **`ingest`** redacts secrets, writes events to `event`, rebuilds any segment that cites an event whose text changed, and advances the source's resume point after each committed batch.
3. **`fit-gaps`** measures, per chat, the silence that separates two conversations.
4. **`segment`** turns events that no segment cites yet into segments.
5. **`enrich`**, off by default, asks an LLM for a summary, topics, facts and commitments.
6. **`embed`** encodes segments that have no embedding and refreshes the term statistics.
7. **The api** answers questions from segments and events; **the MCP server** forwards an assistant's tool calls to it.

The stages live in [worker.py](../../chronicle/worker.py); the schema and the search functions in [migrations/](../../migrations).

## Events

An adapter yields a `SourceEvent` ([base.py](../../chronicle/adapters/base.py)):

| Field | Meaning |
|---|---|
| `source`, `source_id` | the source, and an id stable within it |
| `ts` | when it happened, not when it was imported |
| `text` | what gets indexed |
| `actor`, `kind` | who, and what sort of row |
| `payload` | everything else from the source, kept verbatim |
| `reply_to` | the id this row answers, if any |
| `thread_key` | the group segmentation runs within: a chat, a project |
| `watermark_ts` | the resume point; defaults to `ts` |

A roll-up sets `watermark_ts` to the end of its span. Resuming from the start would re-read the span and emit a second, partial event under a new id.

`ingest` writes each event to `event`, keyed by `(source, source_id, ts)`. Before the row exists, [redact.py](../../chronicle/redact.py) replaces secrets in the text and in every string of the payload. [001_core.sql](../../migrations/001_core.sql) calls the table immutable, but its text does change in two cases. When a row comes back with different, non-empty text (a transcript landing on a voice note, an edited message), `ingest` takes the new text and rebuilds every segment that cites the row, clearing its embedding; an empty text never overwrites a non-empty one. And the redaction backfill rewrites stored secrets ([Deploy and operate](operate.md#redact-secrets-already-stored)).

Each source's resume point is `source.last_ingested_at`, advanced to the largest `watermark_ts` of each batch once the batch commits. A crash re-reads one batch, which the upsert makes harmless. A source that raises an error loses only its uncommitted batch, its error goes to `source.last_error`, and the next source runs.

`event` is partitioned by year from 2018, with one partition for 2027 onward. Full-text and trigram indexes on its text serve [first mentions](#the-other-questions).

## Source policy

Every source declares a density, a tier and a bank in [sources.py](../../chronicle/sources.py). The density decides how hard a source is aggregated before anything reaches the segment layer. Indexing short messages one by one was the wrong unit; turning on every source without a policy is the same mistake with the source mix ([CLAUDE.md](../../CLAUDE.md#source-policy--why-it-exists)).

| Density | Meaning | Sources |
|---|---|---|
| narrative | deliberate text, segmented by time | `telegram`, `forgejo`, `gmail`, `notion`, `github`, `slack` |
| discrete | one row is one thing | `calendar`, `firefly`, `jira`, `paperless`, `karakeep`, `linkedin` |
| telemetry | meaningful only in aggregate; the adapter rolls it up | `wakapi`, `dawarich`, `lastfm`, `immich`, `owntracks` |
| ambient | a weak attention signal | `miniflux` |

The tier is the rollout order: open one, measure, then open the next ([Connect a source](sources.md#open-the-next-tier)). `conflicts()` flags `dawarich` together with `owntracks`, which read the same GPS signal. `NOT_SOURCES` names the stacks on the host that carry no life signal, so that "every channel" has a written boundary. The bank is where a promoted fact would go in a curated memory; nothing promotes facts yet. [test_adapters.py](../../tests/test_adapters.py) fails if an adapter has no policy or its density disagrees with the policy.

The README and the `Density` docstring say ambient sources stay off the default retrieval surface. Nothing enforces that yet: `hybrid_search()` does not filter by density, so `miniflux` segments are searched like the rest.

## Segments

Only narrative sources are cut by time. A telemetry, discrete or ambient event becomes one segment on its own and is marked substantive: a roll-up already stands for one stretch of activity, and a discrete row is already one thing.

**The gap.** `fit-gaps` looks at every narrative thread with at least 100 events ([segment.py](../../chronicle/segment.py)). It builds a histogram of the logarithm of the gaps between messages and looks for the deepest valley between 10 minutes and 6 hours. A valley counts only if it sits below 60% of the peaks on both sides; otherwise the 90th-percentile gap stands in. The result, clamped to 10 minutes to 6 hours, goes to `thread_config`. A thread with no fit uses 30 minutes.

**The cut.** `segment_chat` starts a new segment when:

- the gap to the previous event exceeds the thread's gap, unless the event replies to a message in the open segment and the gap is under four times the thread's gap;
- the open segment already holds 30 events (`SEGMENT_MAX_MESSAGES`);
- or it holds about 250 tokens, counted as characters divided by three.

Then a segment of fewer than 3 events folds into the one before it, when that one is at least as close in time as the one after and the merged segment stays within 60 events; otherwise it stands. [test_segment.py](../../tests/test_segment.py) covers the gap fit, the caps, the reply edge and the merge.

**Incremental.** `segment` reads only events that no segment cites. When new events reach back into a thread's last segment, that segment is rebuilt in place with them: same id, embedding and enrichment cleared. A conversation still going at the last run keeps growing instead of being cut at the run boundary. Events older than the thread's last segment, a late backfill, are segmented among themselves and never merged into older segments ([worker.py](../../chronicle/worker.py)).

**Two texts.** `raw_text` is one `actor: text` line per event; it is what a hit returns. `embed_text` is what gets indexed: a header, then `raw_text`, then topics and facts once enrichment has run ([segment.py](../../chronicle/segment.py)).

```text title="The header embed_text starts with"
[chat: <chat title>] [with: <people>] [date: YYYY-MM] [weekday: <day>]
```

**Substantive.** A burst of acknowledgements is not a memory. A narrative segment of one or two events counts as substantive when its text runs past 80 characters; three or more events need 80 characters and at least three different messages. Search and enrichment skip segments that are not substantive.

## Retrieval

`/recall` runs `hybrid_search()` ([002_retrieval.sql](../../migrations/002_retrieval.sql)), which ranks segments two ways and fuses the ranks.

- **Dense.** The exact cosine distance between the question's embedding and every embedded, substantive segment that passes the filters; the closest 100 go on. There is no approximate index. About 50,000 segments of `halfvec(1024)` fit in memory, an exact scan keeps every date filter exact, and a filtered approximate index loses recall exactly on narrow date ranges ([ADR-001](../ADR-001-postgres-not-qdrant.md)).
- **Lexical.** The question's words, stemmed with the `ru_unaccent` text-search configuration, that occur in fewer than 5% of substantive segments. Each segment scores the summed inverse document frequency of the ones it contains; the best 100 go on. The statistics live in `lexeme_df`, which `embed` refreshes after every run. Until the first refresh the lexical side finds nothing and search is dense-only.
- **Fusion.** Reciprocal rank fusion with k = 60: a segment scores 1/(60 + its dense rank) plus 1/(60 + its lexical rank). The best `limit` come back, 20 by default.

**Dates in the question.** When you set neither `date_from` nor `date_to` and the question names exactly one year, [`route.window()`](../../chronicle/route.py) confines the search to it, with margins: a named month widens by a month either side, a named season likewise, and a bare year runs from the November before to the January after. Two different years give no window. `window_from_query` in the response shows what was applied; [test_route.py](../../tests/test_route.py) has the cases.

**Two encoders, one vector space.** The worker writes vectors with BGE-M3 through FlagEmbedding on PyTorch. The api encodes each question with BAAI's fp32 ONNX export of the same weights, at a pinned revision. [ADR-003](../ADR-003-onnx-query-encoder.md) measured identical top-20 results on 40 queries against the PyTorch encoder, with a 1,723 MB peak instead of 4,430 MB. [test_embed.py](../../tests/test_embed.py) keeps the worker on the encoder that wrote the stored vectors.

**The intent label.** [`route()`](../../chronicle/route.py) matches the question against fixed English, Russian and Ukrainian patterns and labels it `aggregate`, `first_mention`, `evolution`, `timeline` or `lookup`. `/recall` returns the label and the pattern that matched (`intent`, `routed_because`), and runs the same search whatever the label says. Choosing the right tool for a question is the caller's job. `route()` also computes an `apply_recency_decay` flag; nothing reads it, and no retrieval function discounts old content ([test_migrations.py](../../tests/test_migrations.py)).

## The other questions

Each kind of question has its own operation, because the most similar text is rarely the earliest, and a burst of recent talk should not hide earlier years ([api.py](../../chronicle/api.py)).

- **First mention** searches `event`, not segments: a first mention is often one word in a short message. The api tries the term and its lemma (pymorphy3, Russian and Ukrainian), each as a full-text phrase or as a substring of the text, and returns the 20 earliest matches, oldest first, with a note to check the earliest before calling it the first. It does not transliterate, so a term in Latin letters does not find the same word written in Cyrillic.
- **Evolution** runs `stratified_search()`: it cuts the archive into bins of `bin_width` (3 months by default) and returns the 10 segments closest to the topic in each bin, in time order. Bins are fixed lengths counted from the Unix epoch, with a month counted as 30 days. The api makes no model call here and writes no summary.
- **Timeline** returns every substantive segment that starts in the window, from all sources or the ones you name, in time order, each as its summary or its first 300 characters. This is where coding sessions, stays, payments and photos sit beside the chats.
- **Ground** runs the hybrid search on a claim from another memory, with no date filter, and returns the hits in time order, so a change of position over the years is visible.
- **Tally** runs SQL the caller writes as the `chronicle_tally` role ([004_tally_role.sql](../../migrations/004_tally_role.sql)): SELECT only, read-only transactions, a 10-second statement timeout, at most 4 connections, a fresh connection per call that is closed without committing, and a password the api rotates at every start. It returns the SQL with up to 200 rows. [tally-itest.py](../../scripts/tally-itest.py) checks that the role can read, and that it refuses writes, superuser functions and role changes and cancels a query past 10 seconds.
- **Commitments** lists open promises older than 90 days from `v_forgotten_commitments`. Only enrichment creates commitments.

## Enrichment and facts

`enrich` sends narrative, substantive, not yet enriched segments to an OpenAI-compatible chat endpoint, newest first, `ENRICH_LIMIT` per run ([enrich.py](../../chronicle/enrich.py)). It is off by default. The model's JSON reply is treated as untrusted input: numbers are clamped, lists are capped at 6 topics, 8 facts and 4 commitments per segment, and a fact whose predicate is outside the closed list in `fact_predicate` is dropped rather than invented.

What it writes:

- **A summary, topics, importance and sentiment** on the segment. The summary is shown next to a hit and never indexed on its own; summaries are the weakest memory unit in the research the project follows ([RESEARCH.md](../RESEARCH.md)).
- **Topics and facts appended to `embed_text`**, never replacing the conversation. `raw_text` does not change, and the segment is re-embedded.
- **Facts** in the bi-temporal `fact` table. Each fact's subject resolves to an entity, its predicate is one of the 16 that [005_incremental.sql](../../migrations/005_incremental.sql) defines, and its version and `t_valid` are the segment's start.
- **Commitments**, each with a direction (`i_owe` or `owed_to_me`) and an optional due date.

After each batch, `resolve_fact_conflicts()` closes every single-valued fact that a later version superseded, by setting its `t_invalid` to the next version's `t_valid`. Nothing is deleted: `v_current_facts` shows the live value, and the history stays queryable. The newest fact wins by `max(version)` in SQL; no model is asked ([002_retrieval.sql](../../migrations/002_retrieval.sql), covered by [smoke.sql](../../scripts/smoke.sql)).

Every fact and commitment cites its events in `projection_dep`, the table [purge](operate.md#erase-what-a-filter-now-excludes) walks to delete what was derived from erased events. Enriching a segment again replaces its facts, commitments and entity mentions. `CHRONICLE_OWNER` names the owner in the prompt and in facts, and `CHRONICLE_OWNER_ALIASES`, plus `me`, are the sender names that mean the owner.

`v_promotable_facts` selects facts that could move to a curated memory: confidence of 0.7 or more, and a subject mentioned in at least 3 segments across at least 2 threads. Nothing promotes them yet ([ROADMAP.md](../../ROADMAP.md#stage-1--someone-else-can-run-it-v1x)).

## Entities

One name can be written in Russian, Ukrainian and Latin letters, and those spellings share no characters. [resolve.py](../../chronicle/resolve.py) gives every name two keys ([ADR-002](../ADR-002-entity-resolution.md)):

- **A phonetic key**: transliterate, then collapse the differences between Russian, Ukrainian and Latin romanisations. The Russian, Ukrainian and Latin spellings of one first name all map to the same key.
- **A consonant skeleton**: the phonetic key without vowels, which catches the vowel alternation between Russian and Ukrainian. `Kiev` and `Kyiv` get the phonetic keys `kev` and `kiv` and the same skeleton, `kv`; `Kharkov` and `Kharkiv` share `grkv`. A skeleton shorter than 2 characters is not used.

The skeleton over-merges on purpose, so it can only propose candidates, never decide. In the pipeline, `enrich` finds or creates a person entity for each fact's subject by its phonetic key, and stores the skeleton without acting on it. `resolution_candidates()`, which uses both keys and blocks candidates by chat, is written and tested in [test_resolve.py](../../tests/test_resolve.py), and nothing calls it yet.

People who send messages were meant to need none of this: ADR-002 treats Telegram's sender id as ground truth, and the schema has a `person` table with `telegram_user_id`. Nothing writes to `person` yet.
