---
paths:
  - "chronicle/api.py"
  - "chronicle/route.py"
  - "chronicle/embed.py"
  - "chronicle/rank.py"
  - "chronicle/rerank.py"
  - "chronicle/evaluate.py"
  - "chronicle/freshness.py"
  - "chronicle/ui.html"
  - "migrations/**"
  - "Dockerfile"
  - "compose.yaml"
  - "tests/test_route.py"
  - "tests/test_embed.py"
  - "tests/test_rerank.py"
  - "tests/test_evaluate.py"
  - "tests/test_freshness.py"
  - "tests/test_migrations.py"
  - "docs/ADR-001-*.md"
  - "docs/ADR-003-*.md"
---
Split from CLAUDE.md, loaded when matching files are touched: Retrieval, api and embedding: hard-won facts.

20. **`chronicle-api` needs 5g, not 2g — and the failure is invisible.**
   BGE-M3 *loads* in well under 2g; the first `encode()` is what allocates.
   So `/health` and `/stats` passed while every `/recall` OOM-killed uvicorn
   mid-request: the client saw a dropped connection, the container restarted,
   reloaded the model, and looked healthy again. `dmesg`:
   `Memory cgroup out of memory: Killed process (uvicorn) anon-rss:2067848kB`,
   `CONSTRAINT_MEMCG`. The worker peaked at 3.885 GiB doing the same encode.
24. **A CTE referenced twice is materialized, and a CTE scan cannot use an
   index.** `hybrid_search` factored its four filter predicates into one
   `filtered` CTE, which both the dense and the lexical branch then read. That
   is two references, so PostgreSQL materialized it — and the lexical branch
   lost `segment_fts_idx` (25 MB GIN, expression-identical to its predicate)
   and recomputed `to_tsvector()` over all 38,736 substantive segments on
   every query. Measured: lexical branch **5,506 ms via the CTE vs 3.2 ms
   against the base table**; whole function **5,694 ms → 120-255 ms**;
   `/recall` end to end **7.4 s → 0.40 s**, with a byte-identical top-20. The
   fix is to repeat the predicates in both branches; that duplication IS the
   optimization, and `tests/test_migrations.py` fails if someone tidies it
   away. Keep the predicate expression character-identical to the index or
   the planner silently seq-scans again with no error and no warning.
25. **Never record a latency number from the first request after a deploy.**
   The "~25 s query latency" in this file was one cold call: BGE-M3 loads
   lazily (`embed.py`, `_model` cached on the instance), so the first
   `/recall` after any restart pays 11-16 s of model load. It stood for a
   month and blamed the hardware — it had been measured once, on 2026-08-11,
   and `RestartCount=0` plus three `/recall` lines in 33 days of container
   logs proved no warm call had ever been served. Warm the model, then
   measure; and check `docker stats` first, because 39 MiB resident means the
   model is not loaded and the next number will be a cold one.
26. **`use_fp16=True` is FASTER on this CPU box, not slower.** The obvious
   reading — "CPU torch has no fp16 path, so `.half()` gets emulated" — is
   backed by real hardware behaviour and is still wrong here, because BGE-M3
   on 16 contended cores is memory-bandwidth bound, not FLOP bound. Measured,
   one short query, mean of 5 after warmup: fp16/16-threads **0.541 s** vs
   fp32/16-threads **4.385 s** — 8x worse. Thread count is the real lever
   (fp16: 16→0.541 s, 8→0.215 s, 4→0.238 s), hence `OMP_NUM_THREADS: "8"` in
   compose.yaml. Do not "optimize" fp16 off without re-running the matrix.

34. **A warm model is not a warm query.** With BGE-M3 already resident (3.714
   GiB RSS, `RestartCount=0`, 18 h uptime) the first `/recall` still took
   **9.99 s**, and the next two 0.443 s and 0.448 s. Fact 25 covers the cold
   MODEL; this is something else going cold on an idle box — most likely the
   25 MB `segment_fts_idx` GIN falling out of cache. Discard the first call
   after any idle period too, not just after a restart.
35. **torch's memory is the first forward pass, not the weights — and int8
   is not free.** BGE-M3 via FlagEmbedding settles at 2.27 GB after load
   (4.43 GB peak while it halves fp32), then the first `encode()` adds 1.65 GB
   that is never returned; `malloc_trim` recovers 30 MB. The api now runs
   BAAI's own fp32 ONNX export (`EMBED_BACKEND=onnx`): 1.72 GB peak, 0.05 s a
   query, 40/40 identical top-20s. int8 ONNX was 1.15 GB but cos 0.985 to the
   stored vectors, which reordered all 40 result lists and moved top-1 on 1 in
   8. Without labelled eval questions that cannot be shown not to regress.
   The worker stays on torch because it wrote the corpus. `docs/ADR-003`.
45. **`to_tsquery()` of joined patterns is a syntax error for any phrase.**
   `first_mention()` built `'game of thrones | game'` as raw tsquery text and
   returned 500 for every multi-word term; the api also split the lemma into
   words, each an ILIKE, so "of" matched everything. One `phraseto_tsquery`
   per pattern, OR-ed in a scalar subquery (keeps `event_fts_idx`).
46. **The lexical branch of `hybrid_search` was dead for questions.**
   `plainto_tsquery(q_text)` ANDs every word, so a natural-language question
   matched ZERO segments on 27 of 28 eval lookups and all retrieval was
   dense-only — the rare entity names (EPAM, MacBook, BIOS) never got a
   lexical vote. A plain OR is no fix (~15k matches, and ts_rank_cd has no
   IDF, so "як"/"і" decide). It is now the question's terms in under 5% of
   segments, one index probe each, summed IDF from `lexeme_df` (refreshed
   after every `embed`): 8 ms. The old branch also had no ORDER BY before
   its LIMIT — past 100 matches it kept an arbitrary 100. A to_tsvector
   stem is not idempotent under re-stemming ('епам' -> 'еп'): never feed
   stems back through to_tsvector.
47. **A question usually names its own date — use it.** "наприкінці 2024",
   "влітку 2025", "в січні 2026": `route.window()` turns a single named
   year/season/month into a range with generous margins and `/recall`
   applies it unless the caller set dates. It also makes those queries 3-4x
   faster (the dense scan shrinks). Two different years -> no window: a
   window that excludes the answer is worse than none.
49. **Score p@1 in the unit the system retrieves.** The harness flattened
   chronicle's ranked segments into one id list and asked whether the FIRST
   EVENT was gold — false for nearly every ~13-event segment even when it
   ranked first and held the answer. 11.8% flattened vs 29.4% by segment on
   17 lookups, retrieval unchanged. Fact 28 one level up.
