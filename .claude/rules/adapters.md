---
paths:
  - "chronicle/adapters/**"
  - "chronicle/sources.py"
  - "chronicle/doctor.py"
  - "tests/test_adapters.py"
  - "tests/test_nytka.py"
  - "scripts/nytka-itest.py"
  - "scripts/doctor-homelab.sh"
  - "compose.sources.example.yaml"
---
Split from CLAUDE.md, loaded when matching files are touched: Source adapters, source policy and their hard-won facts.

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
43. **A rollup keyed on "the current project" fragments under parallel
   work.** wakapi's rollup closed a session on every project switch, which
   was right for one editor and wrong once several Claude Code sessions ran in
   different repos: interleaved heartbeats made 2,420 of 2,898 sessions one
   heartbeat long on the first live run. Each project keeps its own open
   session now. Check the SHAPE of a rollup's output (how many 1-unit spans),
   not just that it produced rows — doctor passed this.
52. **Nytka's conversations are hard-deleted and merged, and its mute windows
   are not retroactive.** `DELETE /conversations/{id}` cascades with no
   tombstone, and late audio that bridges two conversations MERGES them: the
   survivor takes the other's segments and the other row is deleted. So a
   conversation id missing upstream does not mean its speech is gone, and
   purging it would erase utterances that still exist (chronicle keeps them
   under the old thread key, and no later ingest re-reads them). The adapter
   excludes a thread only when its conversation AND every one of its segments
   are gone. `conversations.updated_at` moves on new speech and on close, not
   on an AI title, so it is a complete cursor over closed conversations. A
   mute window drops audio before transcription only from the moment it is
   saved: 350 of 4,798 utterances were inside the configured one, all captured
   earlier. The adapter applies the window itself.

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
