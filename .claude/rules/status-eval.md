---
paths:
  - "chronicle/evaluate.py"
  - "chronicle/enrich.py"
  - "chronicle/gate.py"
  - "chronicle/rerank.py"
  - "chronicle/segment.py"
  - "chronicle/adapters/nytka.py"
  - "eval/**"
  - "scripts/eval-*.sh"
  - "ROADMAP.md"
  - "CHANGELOG.md"
  - "docs/guide/status.md"
  - "docs/guide/evaluate.md"
---
Split from CLAUDE.md, loaded when matching files are touched: Status, eval history and measurements.

## Status

**Runs:** schema, segmentation, gap fitting, cross-script entity resolution,
intent routing, RRF fusion, bi-temporal facts, source policy, 19 adapters,
doctor, worker (ingest/fit-gaps/segment/enrich/embed), api, evaluate, MCP
surface (mcp 2.x since 2026-09-26).

**Eval on the reference deployment, 2026-09-26: chronicle 63.5% vs ripgrep 62.8%** (`make
eval-homelab`, 37 questions) — level with grep, not yet clearly ahead.

| step | overall | lookup |
|---|---|---|
| first run | 48.2% | 40.5% |
| + short segments un-hidden, first_mention fixed (facts 44-45) | 55.0% | 45.8% |
| + tier 2 data, one enrich batch | 52.3% | 42.3% |
| + lexical branch revived, question's own date window (facts 46-47) | 63.5% | 57.1% |

**2026-09-29, 71 questions, grep on question words only: chronicle 71.1%
vs ripgrep 54.2%** (lookup 67.0% vs 41.8%, lookup p@1 36.2% vs 4.3%). Three
changes, measured one at a time against the same live index: p@1 scored
per segment (fact 49) and evolution routed to `/evolution` took the old 37
from +0.7 to +2.0; a second, independent set of 34 questions joined them
(2 near-duplicates dropped); and grep's keywords lost the answer words the
first drafts had lifted from the gold. With those words back grep scores
68.4% on the 71 — its upper bound, and still below chronicle.
The question-words rule is settled (2026-09-29, roadmap goal 4, v0.3.0):
grep's keywords may hold only the question's own words — stems, other
spellings, the RU/UK form of the same word — never a word from the answer.
A lint for it was tried and not kept: prefix, transliteration and skeleton
matching still flagged 24 of 142 keywords on the cleaned set, all
legitimate synonyms, and the set is not in the repository for CI to read.

Lookups with the gold outside the top 20 on the old 37: 11 -> 8 of 28. The 8 left are
vocabulary mismatch (the answer never uses the question's words; paraphrase
or other-script spelling) and one single short message still flagged
non-substantive. Cross-script spelling variants were tried and reverted: no
fixed misses, 4x latency. first_mention 100% on par with grep; evolution
25% both.

**Segment cap sweep, 2026-09-29 (roadmap goal 5): 15 beats 20 beats 30,
by two questions of 71.** Scope: the 46 threads the eval's gold lives in,
minus the 3 largest chats (22,111 of their 34,539 segments — the full
49 would have been ~25 h of embedding); 34 of 47 lookups have gold only in
the 46. Each cap rebuilt those threads with `worker resegment`, embedded,
and ran `eval-homelab`; baseline is the same threads as the nightly left
them (incremental, cap 30).

| cap | segments | avg / max events | overall | lookup (47) | lookup, rebuilt threads (34) | missed |
|---|---|---|---|---|---|---|
| 30, incremental | 12,428 | 12.1 / 37 | 71.1% | 67.0% | 67.6%, p@1 41.2% | 6 |
| 30, rebuilt | 12,389 | 12.1 / 34 | 71.1% | 67.0% | 67.6%, p@1 38.2% | 6 |
| 20 | 13,721 | 11.0 / 24 | 71.6% | 67.7% | 68.6%, p@1 38.2% | 6 |
| **15** | 15,497 | 9.7 / 22 | **73.2%** | **69.1%** | **70.6%, p@1 41.2%** | 5 |

Against a rebuilt 30, cap 15 moves four questions and loses none: one
missed lookup found (rank 15), one lookup to rank 1 (from 3), one missed
evolution question half-found. ripgrep is untouched by the cap (54.2%).
Direction is consistent, size is small: not a result to build on alone.
The cap is a split point, not a ceiling:
`_merge_runts` folds a runt into its neighbour up to `2 x max_messages`,
hence max 22 at cap 15 and 34-37 at 30. Embedding ran at ~150-165
segments/min (torch, worker, 8 threads) — 5-6x the 2026-08-11 rate, so a
full-archive rebuild is hours, not ~10 h.

**The archive was rebuilt at 15 the same day, and 15 is the default since
v0.4.0.** 184 narrative threads had a segment over 15 events: 37,540
segments became ~49,300, embedded in batches of ~4k so only one batch was
ever missing from the dense index (~5 h). The other threads would cut
identically and were left alone. Result on all 71 questions: **chronicle
75.4% vs ripgrep 54.2%** — lookup recall 67.0% -> 75.5%, complete misses
10 -> 7, lookup p@1 unchanged at 36.2%. No lookup lost gold; five gained
(three misses found). Ranks moved both ways (9 up, 11 down). The one loss:
the evolution question that scored went from found to missed, so evolution
is 0% (was 16.7%). Rebuilding cascaded away the one enrich batch's output
in those threads (590 of 605 facts, 1,742 of 1,798 summaries); enrich is
off and goal 7 re-runs it as an A/B.

**Nytka source, measured 2026-10-04 before enabling (tier 4, off until `TIER=4`).**
Nytka is ambient speech from a wearable, read in place from its server's
PostgreSQL: 129 conversations and 4,798 utterances in five days (298k chars;
37% under 20 chars, 67% under 60, 6% over 200 — Telegram is 65 / 94 / 1.5). The
default segmenter turns them into 456 segments of 9.8 events and 622 chars
(91% substantive) in 105 threads; 350 utterances (7%) sit inside a mute window
the server only applies to audio captured after it was saved, and are skipped.
Redaction changed 0 of 4,448 utterances.
Does it crowd the Telegram archive out? The 71 questions on a throwaway clone
of the live index (68,931 segments), same questions and api each time:

| Nytka in the index | segments | overall | lookup p@1 | Nytka share of top-20 |
|---|---|---|---|---|
| none | 0 | 75.4% | 36.2% | 0% |
| as measured | 456 (5 days) | 75.4% | 36.2% | 0.4% |
| x8, copied forward in time | 3,648 (~40 days) | 75.4% | 31.9% | 2.2% |
| x40, copied forward in time | 18,240 (~200 days) | 76.1% | 34.0% | 1.0% |

No drop beyond the one-question noise of 71 (1.4 points), so no stricter
`is_substantive` rule and no gate: `chronicle/gate.py` gates ENRICHMENT calls,
not indexing, so it could not have been one. The x8 and x40 rows are copies of
the real vectors, so ranks clump; they test volume, not variety. Median
`/recall` on that box: 0.78 s with none, 0.76 s with 456, 0.99 s with 18,240.
Re-run `make eval-homelab` (`EVAL_DB`, `EVAL_API`) when Nytka passes ~20k
segments or the live eval loses two questions. The eval has no Nytka gold
question, so it shows harm to old questions, not whether Nytka answers its own.
Cost: 456 segments embedded in 298 s (8 threads, model load included, ~4.1 GiB
sampled peak, load average 2.0 -> 7.4). At the observed ~91 segments a day a
night adds about a minute to the worker that already embeds Telegram, and the
index grows by ~33k segments a year on top of 68.9k. No new service.

**Enrich works and is OFF by default** (`ENRICH_LIMIT=0` in compose.yaml).
gemini-3.5-flash-lite through LiteLLM on chronicle's own key (fact 42). One
batch ran 2026-09-26 — 2,446 newest segments, 939 facts, 2 failed calls —
and the eval went 55.0% -> 52.3% (lookup 45.8% -> 42.3%). The new tier-2
sources were ruled out (6 of 560 top-20 slots, all firefly); only 4 gold
segments were enriched, so the likely mechanism is enriched segments
crowding older gold out of the top 20. One question of 28: not conclusive,
not positive. Turn it on only for a measured A/B.

**Enrichment A/B, 2026-09-29 (roadmap goal 7): off.** Enrichment moved out of
`embed_text` into `segment.enrich_text`, fused by RRF as a third lexical list
only when asked (`use_enrich`, migration 006; `evaluate --enrich`), so the
embedding and the raw-text lists are identical in both arms. 36,635 segments
in the 49 eval threads were enriched (78 failed, ~40 min at 16 workers).
Result on the 71 questions: **75.4% without, 71.8% with** — lookup 75.5% ->
70.2%, p@1 36.2% -> 29.8%, two more complete misses. The 2026-09-26 loss was
therefore not crowding by embedded text alone: the enrichment's own words pull
wrong segments up. Not worth a second design; the code stays as the switch
that reproduced this, the default is off.

**Erasure runs.** `chronicle/purge.py` + `make purge-excluded` delete whatever
each adapter's `excluded_thread_keys()` now excludes but a laxer rule already
indexed. Dry-run by default. Every run writes an
`erasure_log` row.

**Secrets are redacted at ingest** (`chronicle/redact.py`), for every source
and tier, before the row exists — token formats, labelled values, and a bare
value whose label sits on another line. `make redact-secrets` does the same
to what is already stored. Measured 2026-09-29 on the reference archive: 63
of 689,912 events, and all 5 bare credentials in karakeep's text bookmarks.

**Tested:** 195 unit tests; `make smoke` runs every migration and SQL
function plus six integration tests against a real PostgreSQL —
purge-itest, tally-itest, worker-itest (the pipeline run five times),
resegment-itest, redact-itest and nytka-itest. CI
runs both, plus `bash -n` on every script, gitleaks, and a check that every
commit uses a noreply address.

**Reference deployment, first full ingest (2026-08-11):**

| stage | result |
|---|---|
| ingest | **685,401** telegram events — exactly the source row count, no loss, no duplicates |
| thread_key | **494** distinct. Before the fix this would have been 1 |
| fit-gaps | 171 threads (those with ≥100 events); 142 interior, 28 at the 10-min floor, 1 at the 6h ceiling, **0 clamped** |
| segment | **51,044 segments** — 13.4× reduction, avg 13.4 events / 488 chars, 76% substantive |
| embed | **51,044 / 51,044** at 1024 dims, ~9.8 h on CPU |
| serving | all three containers healthy; `/recall`, `/first-mention`, `/stats` answering against the real archive |

Query latency is **~0.4 s warm** (2026-09-14), after fixing the defect that
made it look like a hardware problem. The earlier "~25 s, essentially all of
it BGE-M3 encoding on a CPU box under load 28" was wrong twice over: that
number was the FIRST `/recall` ever served — cold model load included — and
the steady-state cost was 5.7 s of SQL against 0.2 s of encoding. See
hard-won fact 24. Encoding was never the bottleneck and a smaller model was
never the answer.

Retrieval is doing the thing the project was built for. A search for
`квартира ремонт` returns whole apartment-hunting exchanges — a viewing
arranged and cancelled, prices compared, a listing forwarded — as single
segments. Indexed per message, `Да, вийде` and `Ок` would be meaningless
vectors; that is the 65%-under-20-chars problem, gone.

Segmentation was read before embedding, and it holds: every segment sits
inside one `thread_key`, exchanges read as complete units, and
`is_substantive` correctly marks filler bursts false. The bump at 30 events
was the deliberate `max_messages` cap in `segment.py:194`, not a defect (the
cap is 15 since v0.4.0).

**Configuration boundary.** Nothing about one deployment lives in code. The
owner (`CHRONICLE_OWNER`, `CHRONICLE_OWNER_ALIASES`) and every per-user id are
in `.env`; how the worker reaches its sources is `compose.override.yaml`
(template: `compose.sources.example.yaml`). Secrets that belong to another
stack — a source's database password, a shared Last.fm key — are read in
place at run time by `scripts/nightly.sh` and `scripts/doctor-homelab.sh` and
passed by name, so `.env` holds only chronicle's own secrets plus ids and
paths.
