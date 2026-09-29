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

## Status

**Runs:** schema, segmentation, gap fitting, cross-script entity resolution,
intent routing, RRF fusion, bi-temporal facts, source policy, 18 adapters,
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

Lookups with the gold outside the top 20 on the old 37: 11 -> 8 of 28. The 8 left are
vocabulary mismatch (the answer never uses the question's words; paraphrase
or other-script spelling) and one single short message still flagged
non-substantive. Cross-script spelling variants were tried and reverted: no
fixed misses, 4x latency. first_mention 100% on par with grep; evolution
25% both.

**Enrich works and is OFF by default** (`ENRICH_LIMIT=0` in compose.yaml).
gemini-3.5-flash-lite through LiteLLM on chronicle's own key (fact 42). One
batch ran 2026-09-26 — 2,446 newest segments, 939 facts, 2 failed calls —
and the eval went 55.0% -> 52.3% (lookup 45.8% -> 42.3%). The new tier-2
sources were ruled out (6 of 560 top-20 slots, all firefly); only 4 gold
segments were enriched, so the likely mechanism is enriched segments
crowding older gold out of the top 20. One question of 28: not conclusive,
not positive. Turn it on only for a measured A/B.

**Erasure runs.** `chronicle/purge.py` + `make purge-excluded` delete whatever
each adapter's `excluded_thread_keys()` now excludes but a laxer rule already
indexed. Dry-run by default. Every run writes an
`erasure_log` row.

**Secrets are redacted at ingest** (`chronicle/redact.py`), for every source
and tier, before the row exists — token formats, labelled values, and a bare
value whose label sits on another line. `make redact-secrets` does the same
to what is already stored. Measured 2026-09-29 on the reference archive: 63
of 689,912 events, and all 5 bare credentials in karakeep's text bookmarks.

**Tested:** 157 unit tests; `make smoke` runs every migration and SQL
function plus five integration tests against a real PostgreSQL —
purge-itest, tally-itest, worker-itest (the pipeline run five times),
resegment-itest and redact-itest. CI
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
is the deliberate `max_messages` cap in `segment.py:194`, not a defect.

**Configuration boundary.** Nothing about one deployment lives in code. The
owner (`CHRONICLE_OWNER`, `CHRONICLE_OWNER_ALIASES`) and every per-user id are
in `.env`; how the worker reaches its sources is `compose.override.yaml`
(template: `compose.sources.example.yaml`). Secrets that belong to another
stack — a source's database password, a shared Last.fm key — are read in
place at run time by `scripts/nightly.sh` and `scripts/doctor-homelab.sh` and
passed by name, so `.env` holds only chronicle's own secrets plus ids and
paths.

## Commands

```bash
make test                 # 157 unit tests; bootstraps .venv, no DB or models
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

1. **wakapi is SQLite, not Postgres.** The first adapter used server-side named
   cursors, which do not exist there.
2. **SQLite returns `TIMESTAMP` columns as TEXT.** Date arithmetic raises
   `unsupported operand type(s) for -: 'str' and 'str'`. Everything routes
   through `adapters.base.coerce_ts`.
3. **firefly is MariaDB**, and amounts live on `transactions` (two signed rows
   per journal), not on the journal. The naive join double-counts.
4. **immich `createdAt` is upload time, not capture time.** Use EXIF
   `dateTimeOriginal`. A 2019 photo imported in 2024 lands five years out and
   silently corrupts every timeline it touches.
5. **Rollup adapters must set `watermark_ts` to the span END.** `ts` is the
   span start; resuming from it re-reads the span and emits a NEW partial
   event with a different `source_id`, so `ON CONFLICT DO NOTHING` does not
   catch it and duplicates accumulate on every scheduled run.
6. **`is_substantive` filtering is NARRATIVE-only.** The filler-burst heuristic
   ("a burst of `ок` is not a memory") marked every telemetry segment false and
   hid them from every query.
7. **Cross-script entity resolution needs two tiers.** Straight ISO-9
   transliteration gives four keys for one person (ehor/iehor/yehor/egor).
   Tier 1 coarse phonetic handles names; tier 2 consonant skeleton is required
   because RU/UK differ by systematic vowel alternation (Киев→`kev` vs
   Київ→`kiv`, both →`kv`). Minimum skeleton length is **2**, not 3 — `Київ`→
   `kv` is the case it exists for. See `docs/ADR-002`.
8. **`_merge_runts` must stay O(n).** An earlier version called
   `segments.index(s)` in a loop — quadratic, and it matched by dataclass
   equality so identical segments resolved to the same index.
9. **`fit_thread_gaps` must skip non-conversational sources.** wakapi's p90 gap
   is 2 days, which clamps to the 6h ceiling and then claims to be a session
   boundary. The SQL fallback also records `clamped` so a degenerate fit is
   visible rather than authoritative.
10. **telegram-sync is SQLite too**, at `telegram-sync/data/telegram.db`
   (682,099 rows, WAL). There is no Postgres in that stack — the only other
   store is Qdrant. Fact 1, one stack over.
11. **A TEXT timestamp bound must match ITS OWN source's storage format.**
   telegram stores `2026-03-01T09:00:00+00:00`, wakapi stores
   `2026-03-01 09:00:00+00:00`. `date > :since` is a STRING compare; `' '` is
   0x20 and `'T'` is 0x54, so the wrong separator makes every row compare
   greater and `since` silently stops filtering. Each adapter has its own
   `_bound()`; do not unify them. Never bind a raw datetime — sqlite3's
   default adapter emits a third format and is deprecated since 3.12.
12. **Every adapter must set `thread_key`.** telegram was the only one that
   did not, so all 682k events landed in `default`: one fitted gap across 491
   conversations, and segmentation merging unrelated chats by time proximity.
13. **dawarich `points.timestamp` is an `integer` epoch**, not a timestamp
   (1.8.1). `EXTRACT(EPOCH FROM int - int)` does not typecheck and binding a
   datetime raises `operator does not exist: integer > timestamp`. Convert in
   Python, not with `to_timestamp()` — wrapping the column drops the index.
14. **DBSCAN gives the PLACE, not the VISIT.** Grouping stays by spatial
   cluster alone merged every visit to one place into a single event of
   19,163 min — 13.3 days, the whole span of the data, and every other doctor
   check passed it. Cluster spatially, then cut into consecutive runs in time
   (gaps-and-islands). Order every window by `(timestamp, id)`: 280 points
   share a timestamp, and the tie made two identical runs return 56 then 54
   visits. `source_id` keys on `min(id)`, never on the DBSCAN cluster id —
   cluster numbering shifts whenever `since` changes the input set.
15. **A WAL SQLite file cannot be opened through a `:ro` bind mount.** WAL
   creates a `-shm` file for locking even on a `mode=ro` connection, so the
   open fails with `unable to open database file`. Mount the directory rw and
   let the URI enforce read-only. telegram.db is WAL; wakapi.db is not.
16. **Cast every nullable bound in a Postgres query.** A bare `%(since)s`
   arrives as an untyped NULL, Postgres plans the statement with parameter
   type `unknown`, and the next execution with a real value dies on `type of
   parameter 3 (bigint) does not match that when preparing the plan
   (unknown)`. Bit forgejo and dawarich. Write `%(since)s::bigint IS NULL OR
   col > %(since)s::bigint`.
17. **forgejo's `action.content` is a JSON envelope, not prose.** A push
   stores `{"Commits":[{"Sha1":…,"Message":…,"AuthorEmail":…}]}`. forgejo is
   NARRATIVE because commit MESSAGES are deliberate text — indexing `content`
   verbatim embeds SHA1s and author emails and buries the one sentence that
   means anything. Also `content or op_type` leaks a bare integer into
   indexed text on the 1-in-87 push whose content is empty.
18. **firefly soft-deletes `transactions` independently of the journal.**
   Filtering only `j.deleted_at IS NULL` is not enough. It changes nothing
   today (924 rows either way, 0 duplicate journals — all 232 deleted legs
   hang off already-deleted journals) but the day one leg is deleted and
   rewritten, both come back and the journal id stops being unique. It is the
   event PK.
19. **Doctor's constant-`thread_key` check fires on the DEFAULT, not on a
   constant.** forgejo yields one key for all 87 actions because the forge
   holds exactly one repo; nothing is wrongly merged. The bug worth catching
   is an adapter that never ASSIGNS thread_key (fact 12), so the check tests
   for `{"default"}` specifically.

20. **`chronicle-api` needs 5g, not 2g — and the failure is invisible.**
   BGE-M3 *loads* in well under 2g; the first `encode()` is what allocates.
   So `/health` and `/stats` passed while every `/recall` OOM-killed uvicorn
   mid-request: the client saw a dropped connection, the container restarted,
   reloaded the model, and looked healthy again. `dmesg`:
   `Memory cgroup out of memory: Killed process (uvicorn) anon-rss:2067848kB`,
   `CONSTRAINT_MEMCG`. The worker peaked at 3.885 GiB doing the same encode.
21. **An MCP SDK major breaks the server API — pin the major.** 2.0.0
   removed `mcp.server.fastmcp`; an unpinned rebuild pulled it and
   crash-looped the container. Ported to 2.x on 2026-09-26
   (`mcp.server.mcpserver.MCPServer`) and pinned `>=2.2,<3` in both
   `Dockerfile.mcp` and `pyproject.toml`; `tests/test_mcp_server.py` keeps the
   two pins in step. The next major is deliberate work too.
22. **host and port go to `run()` in 2.x — the opposite of 1.x.** 1.x took
   them on the constructor and `FastMCP.run()` rejected them; 2.x's
   constructor has none and `mcp.run("sse", host=, port=)` takes them. The
   default host is `127.0.0.1`, which inside a container means nothing outside
   it can ever connect — and in 2.x a loopback host also switches on the DNS-
   rebinding guard, which rejects every Host header but localhost.
23. **The SDK's SSE transport serves `/sse` and `/messages/` — there is no
   `/health`.** The healthcheck probed `/health`, got 404, and marked a
   working server unhealthy; with `autoheal=true` that is an infinite restart
   loop on a service that was fine. `/sse` is a stream so curl always exits
   28 — compare `%{http_code}` instead of trusting the exit code. Unchanged
   in 2.x, as are all seven tool schemas.
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

27. **A bot DM is a `chat_type = 'user'` chat.** `personal_only` was written
   to keep groups and channels out and was believed to keep bots out too. It
   does neither: `chat_type` is `'user'` for 100% of the 690,177 live rows, so
   the predicate is a no-op in both directions. Without a row-level rule an
   assistant's own Telegram chat flows telegram-sync → chronicle → back to the
   assistant through chronicle's MCP, and it reads its own output as external
   memory. Not hypothetical: the assistant's own bot chat already had **138 events
   and 21 embedded segments** in the live database before JARVIS was written.
   Exclusion needs BOTH signals — telegram-sync's own `chat:bot` tag (29 chats)
   and the Telegram platform rule that every bot username ends in `bot`
   (92 chats). Neither is a superset: the union is 5,806 messages, the tag
   alone misses 3,126 and the username rule alone misses BotFather. And the
   join to `chats` must be LEFT: 33 chat_ids have no `chats` row and carry
   1,168 messages, which an INNER join deletes silently.
28. **Bot chats are 0.81% of rows but 11.3% of the long-form text.** They
   average 151.6 chars per message against 27.8 for everything else. Sizing
   this decision by row count says "not worth it"; sizing it in the unit the
   index actually exists to serve — messages over 200 chars — says the
   opposite. Use the project's own units when judging whether a filter matters.
29. **`x <> ANY(arr)` is NOT the negation of `x = ANY(arr)`.** It is true
   whenever arr holds *any* element differing from x, so against 94 bot thread
   keys it is true for every row and a prune written that way deletes nothing
   while reporting a rowcount. The negation is `<> ALL(arr)`. Silent: no error,
   no warning, and the enclosing `WHERE EXISTS (... = ANY ...)` still matches,
   so the UPDATE "succeeds" on exactly the right rows and changes none of them.
30. **Data-modifying CTEs all see the SAME snapshot.** A `DELETE` CTE cannot
   see rows an `UPDATE` CTE in the same statement just emptied, so
   `WITH pruned AS (UPDATE … RETURNING …) DELETE … WHERE id IN (SELECT … FROM
   pruned WHERE cardinality = 0)` deletes zero rows. Split into two statements
   over disjoint row sets — drop the wholly-doomed rows first, then prune the
   mixed ones. Both of these (29 and 30) were caught by
   `scripts/purge-itest.py` and by nothing else; both read as obviously correct.
31. **Three edges into `segment` cascade and one does not.**
   `entity_mention.segment_id`, `fact.source_segment_id` and
   `commitment.source_segment_id` are `ON DELETE CASCADE` (`confdeltype='c'`);
   `commitment.resolution_segment_id` is **NO ACTION** (`'a'`) and aborts the
   whole erasure with a foreign key violation. NULL it first — the commitment
   is evidence from a chat that survives, only its resolution is being erased.
   Zero rows today because `cmd_enrich` is stubbed, which is exactly why it has
   to be written now rather than discovered by a rollback later.
32. **`segment.source_event_ids` is source-PREFIXED; `event.source_id` is not.**
   The same message is `telegram:123456789:4242` in the segment array and
   `123456789:4242` in the event row, so joining them needs
   `event.source || ':' || event.source_id`. Joining on `source_id` alone
   returns zero matches for 100% of rows and reads as total corruption — it
   briefly did, at 679,664 "dangling" citations out of 679,664.
33. **The erasure path must run in chronicle-WORKER, not chronicle-api.**
   Deciding what to delete means reading each source's own database, and the
   worker is the only container that mounts them (`compose.yaml:168`). The api
   inherits `TELEGRAM_DB_URL` from `env_file` and has no `/srv/telegram`, so it
   dies on `sqlite3.OperationalError: unable to open database file` — the same
   message as hard-won fact 15 with a completely different cause, which is what
   makes it worth writing down.
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
36. **`/tally` runs agent-written SQL, so its boundary is a ROLE, not a
   string filter.** It ran over the superuser pool behind a keyword blocklist
   that `WITH x AS (DELETE …) SELECT` walked through; api and mcp were pulled
   off `edge` on the box 2026-09-24 for it, and the repo's compose.yaml kept
   `edge` until 2026-09-26 — one `rsync` from re-exposing it. A READ ONLY
   transaction is not enough for a superuser either (`pg_read_file`,
   `set_config('statement_timeout','0')`). It now runs as `chronicle_tally`
   (migrations/004): SELECT-only, a fresh connection per call, never committed,
   password rotated at api start. `scripts/tally-itest.py` fails if the role
   ever regains superuser. Nothing on `edge` consumes chronicle today.
37. **telegram resumes on `synced_at`, never on the message date.**
   telegram-sync UPSERTs, and its `DO UPDATE` rewrites `text` and `synced_at`
   together, so write time is the only watermark that sees a transcript
   landing on an old voice note, an edit, or a late backfill. On the date
   watermark, 4,544 transcribed notes could never reach chronicle (4,637 empty
   events against 93 empty source rows). `synced_at` is stamped BEFORE
   telegram-sync's batch commits, so resume re-reads a 10-minute overlap
   (`SYNC_LAG`), and the bound always carries six fractional digits —
   `isoformat()` drops them at microsecond 0, and `'+'` sorts below `'.'`.
38. **The worker must be safe to run twice.** `segment` used to re-read every
   thread and INSERT with no conflict target: right exactly once, a full
   duplicate of 50,096 segments the second time. So nothing re-ran it and the
   index froze at 2026-08-11. It now segments only unsegmented events and
   continues a thread's last segment IN PLACE (same `segment_id`, embedding
   and enrichment cleared), and ingest upserts and rewrites any segment citing
   a changed event. `scripts/worker-itest.py` runs the pipeline five times
   against a real database; it is the only test that can see this.
39. **SQLite orders every INTEGER below every TEXT.** wakapi 2.18 stores
   `heartbeats.time` as epoch MILLIS (all 18,903 rows), so fact 11's TEXT
   bound matched nothing: ingest sat at its 2026-06-18 watermark for three
   months while heartbeats arrived, and doctor only said "dormant". Each row
   is compared against the bound of its own storage class now. Same family:
   karakeep's `createdAt` is epoch SECONDS and its bound was millis; and 19 of
   its 28 bookmarks are TEXT bookmarks whose body is only in `bookmarkTexts`.
40. **psycopg parses placeholders inside SQL comments.** A `%(x)s` written in
   a `--` comment explaining fact 16 failed immich with `query parameter
   missing: x`. Never put placeholder syntax in a comment inside a query.
41. **A source can die upstream of the box, silently.** dawarich and
   OwnTracks (location) and immich (photo backup) all stopped on or around
   2026-06-17 — immich's last upload AND last capture are that day. The
   phone stopped reporting; nothing on the box failed, so no alert fired.
   dawarich's only recent POST to `/api/v1/owntracks/points` returned 401.
   `doctor`'s "no events in 90 days" is the only signal; read it as an
   outage, not a quiet quarter.
42. **Copied secrets die with their source's rotation.** chronicle's
   `LITELLM_API_KEY` was copied from telegram-sync at first deploy; the
   2026-09-23 master-key rotation (deploy repo #98) gave each consumer its
   own virtual key and left chronicle's copy returning 401. Nothing noticed,
   because only `enrich` uses it. chronicle needs its own `chronicle` key.
43. **A rollup keyed on "the current project" fragments under parallel
   work.** wakapi's rollup closed a session on every project switch, which
   was right for one editor and wrong once several Claude Code sessions ran in
   different repos: interleaved heartbeats made 2,420 of 2,898 sessions one
   heartbeat long on the first live run. Each project keeps its own open
   session now. Check the SHAPE of a rollup's output (how many 1-unit spans),
   not just that it produced rows — doctor passed this.
44. **Every two-message segment was hidden from `/recall`.** `_substantive()`
   let a short segment through only if it was ONE message over 80 chars, so
   2,812 two-message telegram segments were non-substantive however long —
   525 of them 200+ chars, and 5 of the 57 gold messages in the first eval.
   Short segments now pass on content. A filter tuned against filler bursts
   has to be checked against the SHORT exchanges that matter most.
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
48. **The reply-edge rule never ran.** `segment_chat` suppresses a split when
   a reply points back into the open segment, and the worker built every
   Event with `reply_to_id=None`, so none of the 7.9% of messages carrying a
   reply ever did. `event.reply_to` and `event.source_id` are both
   `{chat}:{msg}` for telegram and join directly. A rule with no caller looks
   exactly like a rule that works — `resegment-itest` fails if it goes dark.
49. **Score p@1 in the unit the system retrieves.** The harness flattened
   chronicle's ranked segments into one id list and asked whether the FIRST
   EVENT was gold — false for nearly every ~13-event segment even when it
   ranked first and held the answer. 11.8% flattened vs 29.4% by segment on
   17 lookups, retrieval unchanged. Fact 28 one level up.
50. **Telegram's service account (777000) is not a bot to either signal.**
   Username `telegram`, never tagged `chat:bot`, so it survived the bot purge:
   192 events averaging 299 chars, and it owned the earliest match of an eval
   first_mention term. It has a rule of its own in the adapter and in
   `excluded_thread_keys`.
51. **A secret's label is not always next to it.** karakeep held five
   credentials, each alone on one line with its label on another after it
   ("…\n\n<service> api key"); every token-format and `label: value` pattern
   found none of them. The bare-value rule needs all of: a label somewhere in
   the text, the value alone on its line, 20+ chars mixing letters and
   digits, not a URL, not code shape. Each guard came from a measured miss or
   false hit on the live archive — lastfm's one-line JSON, XAML `xmlns` lines,
   `Claude35Sonnet` beside "api key" — and a character blocklist tried on the
   way lost two of the five real values. Redaction must also be idempotent:
   `[REDACTED:url_password]` matched `user:pass@` again, which would have
   rewritten the event and re-embedded its segment on every run.

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

## Source policy — why it exists

"Use all possible channels" has the same failure mode as the original mistake,
one level up. Indexing 681k sub-20-char messages was the wrong **unit**;
enabling twenty sources without a policy is the wrong **source mix**. miniflux
alone can contribute 100k article rows never opened; immich has 400
near-identical burst frames per moment.

So every source declares a **density** in `chronicle/sources.py`, and density
decides how hard the adapter aggregates *before* the segment layer:

- `NARRATIVE` — deliberate human text → segmented
- `DISCRETE` — one row is one thing → passed through
- `TELEMETRY` — meaningful only in aggregate → **the adapter rolls it up**
- `AMBIENT` — stored, off the default retrieval surface

`NOT_SOURCES` lists the 50 homelab stacks explicitly excluded. All 57 is not
the goal — monitoring, qdrant and vaultwarden describe the machine, not the
life. `make test` fails if an adapter has no policy, or if `dawarich` and
`owntracks` are both on (same GPS signal, every trip double-counted).

## Relationship to a curated memory (Hindsight)

Opposites, which is why they compose. Hindsight: ~5,200 curated facts, no
evidence trail — a notebook. Chronicle: 681k events, nothing but evidence — a
recording.

**Chronicle must not flood Hindsight.** The reference deployment's largest bank
is already at 2,726 facts and times out on `sync_retain`. Piping ~50k segments of facts into it
would 40× the bank and make `recall` useless.

- **`ground` (Chronicle → answer), every recall.** Attach real conversations to
  Hindsight's unsourced claims, including contradicting ones. Replaces
  time-based staleness rules with a measurement.
- **Promotion (Chronicle → Hindsight), rare.** `v_promotable_facts` requires
  ≥3 segments AND ≥2 threads at confidence ≥0.7. Target hundreds/year. Check
  `get_bank_stats` after each run.

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
