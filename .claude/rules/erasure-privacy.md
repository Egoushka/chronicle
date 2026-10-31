---
paths:
  - "chronicle/purge.py"
  - "chronicle/redact.py"
  - "chronicle/adapters/telegram.py"
  - "chronicle/api.py"
  - "migrations/004_tally_role.sql"
  - "tests/test_redact.py"
  - "scripts/purge-itest.py"
  - "scripts/tally-itest.py"
  - "scripts/redact-itest.py"
---
Split from CLAUDE.md, loaded when matching files are touched: Erasure, redaction, bot-chat exclusion and /tally: hard-won facts.

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
