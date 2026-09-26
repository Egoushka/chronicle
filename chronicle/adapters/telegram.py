"""Telegram adapter — reads telegram-sync's SQLite/Postgres, never writes.

telegram-sync stays a pure ingest+sync service. Chronicle owns aggregation
and indexing. Once Chronicle's segment index is live, telegram-sync's own
`telegram_personal` Qdrant collection and its embedding path become
redundant and should be deleted — the brain must REMOVE something, not just
add a stack.

Corpus as measured 2026-07-25: 681,331 messages, 487 chats, 457 senders,
2018-12-30 -> present. 65% of messages under 20 chars, 7.9% with reply_to.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, coerce_ts, register

log = logging.getLogger(__name__)

BATCH = 5_000

#: How far back each resume re-reads, on top of the watermark. telegram-sync
#: stamps `synced_at` with now() BEFORE its batch commits, so a row stamped at
#: T can become visible after this adapter has already read a row stamped
#: T+1 and advanced past it. Re-reading is free — the worker's upsert is a
#: no-op on unchanged text — so the window is sized for safety, not cost: a
#: backfill chunk commits within seconds.
SYNC_LAG = timedelta(minutes=10)


def _bound(dt: datetime | None) -> str | None:
    """Render a datetime the way telegram-sync stores it.

    `messages.date` is a TEXT column, so `date > :since` is a STRING compare,
    not a date compare. sqlite3's own datetime adapter emits
    '2026-07-28 21:04:33' — space separator, no offset — while every one of
    the 682,099 stored values is '2026-07-28T21:04:33+00:00' (verified:
    length(date) = 25 for 100% of rows, suffix '+00:00' for 100%).

    ' ' is 0x20 and 'T' is 0x54, so a bound in the adapter's format sorts
    BEFORE every stored row at the same instant. `since` would silently stop
    filtering and the resumable worker would re-read the whole archive on
    every scheduled run.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Always six fractional digits. `synced_at`, the column this bounds, is
    # 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00' on all but 2 of 692,207 rows, and
    # isoformat() drops the fraction whenever microsecond == 0 — at which
    # point '+' (0x2B) sorts below '.' (0x2E) and the bound lands a whole
    # second's worth of rows on the wrong side.
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


@register
class TelegramAdapter(SqlAdapter):
    source = "telegram"
    # telegram-sync is SQLite at /data/telegram.db — there is no Postgres in
    # that stack at all (compose.yaml mounts ./data, and the only other store
    # is Qdrant). Same mistake as wakapi, one stack over: a named server-side
    # cursor does not exist here, so `dialect` drives _stream down the
    # fetchmany path instead.
    dialect = "sqlite"
    density = Density.NARRATIVE
    itersize = BATCH

    def __init__(self, dsn: str, personal_only: bool = True,
                 exclude_chat_ids: tuple[int, ...] = (),
                 exclude_bot_chats: bool = True):
        super().__init__(dsn)
        self.personal_only = personal_only
        # Coerced to int on the way in. These are inlined into the SQL below
        # rather than bound, because a variable-length IN list cannot be
        # expressed once across pyformat and qmark; int() is what makes that
        # safe, so do not relax it to accept strings.
        self.exclude_chat_ids = tuple(int(c) for c in exclude_chat_ids)
        self.exclude_bot_chats = exclude_bot_chats

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        """Every message telegram-sync WROTE after `since`, oldest write first.

        `since`/`until` bound `synced_at`, not the message date. This is
        change-data capture, and it has to be: telegram-sync upserts, and its
        `ON CONFLICT DO UPDATE` rewrites `text` AND `synced_at` together. So
        this one watermark sees three things a date watermark never can:

          * a transcript landing on a voice note weeks after the note arrived
            — 4,544 of them had, and none could reach chronicle (2026-09-26);
          * an edited message;
          * an old message that arrives late, e.g. a backfill of a chat that
            had never been synced.

        The message date stays the event's `ts`; only the resume point moves.
        """
        # A bot DM IS a `chat_type = 'user'` chat, so `personal_only` does not
        # keep bots out — measured, chat_type is 'user' for 100% of 690,177
        # rows, which makes that filter a no-op in both directions.
        #
        # This matters beyond tidiness. sources.py NOT_SOURCES already excludes
        # tg-assistant, agent-runner and hindsight at the STACK level; without
        # the same rule at the ROW level, an assistant's own Telegram chat flows
        # telegram-sync -> chronicle -> back to the assistant through chronicle's
        # MCP, and it reads its own output as external memory about Yehor.
        #
        # Bot chats are 0.81% of rows but 11.3% of messages over 200 chars
        # (they average 151.6 chars against 27.8 for everything else), so in the
        # unit this index actually exists to serve they are ~14x more present
        # than the row count suggests.
        exclude_ids = ""
        if self.exclude_chat_ids:
            ids = ",".join(str(c) for c in self.exclude_chat_ids)
            exclude_ids = f"AND v.chat_id NOT IN ({ids})"

        # `messages`, not the `v_messages` view: the view does not expose
        # `synced_at`, and it is the only column this query is ordered by.
        sql = f"""
            SELECT v.chat_id, v.id, v.chat_title, v.chat_type, v.sender_name,
                   v.date, v.text, v.media_type, v.reply_to_id,
                   CASE WHEN v.is_outgoing = 1 THEN 'sent' ELSE 'received' END,
                   v.synced_at
            FROM messages v
            -- LEFT, never INNER: 33 chat_ids in `messages` have no `chats` row
            -- at all and carry 1,165 messages between them. An inner join drops
            -- those silently, which is a data-loss bug wearing a filter's
            -- clothes. v_chat_stats inside telegram.db joins the other way
            -- round — do not copy it as the house pattern.
            LEFT JOIN chats c ON c.chat_id = v.chat_id
            WHERE (%(since)s IS NULL OR v.synced_at > %(since)s)
              AND (%(until)s IS NULL OR v.synced_at <= %(until)s)
              -- `IS FALSE` is SQLite >= 3.23 only and psycopg would send a
              -- real boolean; an int compare works on every dialect.
              AND (%(personal)s = 0 OR v.chat_type = 'user')
              {exclude_ids}
              AND (%(nobots)s = 0 OR (
                    -- Telegram REQUIRES every bot username to end in "bot", so
                    -- this is a platform invariant rather than a guess. The
                    -- residual risk is a human named e.g. @talbot; that is what
                    -- TELEGRAM_EXCLUDE_CHAT_IDS is for, in either direction.
                    coalesce(lower(c.username), '') NOT LIKE %(botpat)s
                    -- telegram-sync already auto-classifies bot chats, and the
                    -- tag catches the ones with no bot-shaped username at all
                    -- (BotFather, Crypto Bot). Neither signal is a superset of
                    -- the other: 29 chats are tagged, 92 match the username
                    -- rule, and the overlap is partial. Union, not either.
                    AND NOT EXISTS (SELECT 1 FROM chat_tags t
                                    WHERE t.chat_id = v.chat_id
                                      AND t.tag = %(bottag)s)))
            -- Write order, so a batch that commits is a prefix of the stream
            -- and resuming from its max(synced_at) skips nothing. Unindexed:
            -- a full sort of ~700k rows, about a second, once a night.
            ORDER BY v.synced_at, v.chat_id, v.id
        """
        params = {"since": _bound(since - SYNC_LAG) if since else None,
                  "until": _bound(until),
                  "personal": int(self.personal_only),
                  "nobots": int(self.exclude_bot_chats),
                  "botpat": "%bot", "bottag": "chat:bot"}
        if self.exclude_bot_chats or self.exclude_chat_ids:
            log.info("telegram: excluding bot chats=%s, explicit chat_ids=%s",
                     self.exclude_bot_chats,
                     list(self.exclude_chat_ids) or "none")
        for row in self._stream(sql, params):
                (chat_id, msg_id, chat_title, chat_type, sender_name,
                 date, text, media_type, reply_to_id, direction, synced_at) = row
                ts = coerce_ts(date)          # SQLite hands back TEXT
                if ts is None:
                    log.warning("skipping %s:%s — unparseable date %r",
                                chat_id, msg_id, date)
                    continue
                yield SourceEvent(
                    source=self.source,
                    source_id=f"{chat_id}:{msg_id}",
                    ts=ts,
                    text=text or "",
                    # One name for Yehor. telegram-sync writes "me" for an
                    # outgoing message on its backfill path and his display
                    # name on the live path, so 7,781 of 407,650 sent messages
                    # carried a second identity (2026-09-26) — into segment
                    # headers and into entity mention counts.
                    actor="me" if direction == "sent" else sender_name,
                    kind="message",
                    reply_to=f"{chat_id}:{reply_to_id}" if reply_to_id else None,
                    payload={
                        "chat_id": chat_id,
                        "chat_title": chat_title,
                        "chat_type": chat_type,
                        "media_type": media_type,
                        "direction": direction,
                    },
                    thread_key=f"telegram:{chat_id}",
                    watermark_ts=coerce_ts(synced_at) or ts,
                )

    def excluded_thread_keys(self) -> set[str]:
        """The chats `fetch` filters out, as thread keys.

        Deliberately the SAME three predicates as `fetch`, in the same union
        order, because the purge is only correct while it selects exactly the
        complement of what gets ingested. Kept as one query rather than reusing
        `fetch`'s SQL string: that one is a row filter with `NOT`s threaded
        through it, and inverting it textually is how the two drift apart.

        Chat-level, not row-level. `thread_key` is `telegram:{chat_id}` for
        every event (hard-won fact 12), so a chat is entirely in or entirely
        out — there is no partially-excluded segment to split.
        """
        keys: set[str] = set()

        if self.exclude_chat_ids:
            keys |= {f"{self.source}:{c}" for c in self.exclude_chat_ids}

        if self.exclude_bot_chats:
            # Union of the two signals, for the reason given in `fetch`:
            # neither is a superset (the tag alone misses the 92 bot-shaped
            # usernames, the username rule alone misses BotFather). LEFT-join
            # semantics are irrelevant here — a chat with no `chats` row has
            # neither signal and so is not excluded, which is the same answer
            # `fetch`'s `coalesce(lower(c.username),'')` gives it.
            sql = """
                SELECT chat_id FROM chats
                 WHERE lower(coalesce(username,'')) LIKE %(botpat)s
                UNION
                SELECT chat_id FROM chat_tags WHERE tag = %(bottag)s
            """
            params = {"botpat": "%bot", "bottag": "chat:bot"}
            keys |= {f"{self.source}:{row[0]}" for row in self._stream(sql, params)}

        return keys
