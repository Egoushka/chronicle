"""Telegram adapter — reads telegram-sync's SQLite/Postgres, never writes.

telegram-sync stays a pure ingest+sync service. Chronicle owns aggregation
and indexing. Once Chronicle's episode index is live, telegram-sync's own
`telegram_personal` Qdrant collection and its embedding path become
redundant and should be deleted — the brain must REMOVE something, not just
add a stack.

Corpus as measured 2026-07-25: 681,331 messages, 487 chats, 457 senders,
2018-12-30 -> present. 65% of messages under 20 chars, 7.9% with reply_to.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, coerce_ts, register

log = logging.getLogger(__name__)

BATCH = 5_000


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
    return dt.astimezone(timezone.utc).isoformat()


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

    def __init__(self, dsn: str, personal_only: bool = True):
        super().__init__(dsn)
        self.personal_only = personal_only

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        sql = """
            SELECT chat_id, msg_id, chat_title, chat_type, sender_name,
                   date, text, media_type, reply_to_id, direction
            FROM v_messages
            WHERE (%(since)s IS NULL OR date > %(since)s)
              AND (%(until)s IS NULL OR date <= %(until)s)
              -- `IS FALSE` is SQLite >= 3.23 only and psycopg would send a
              -- real boolean; an int compare works on every dialect.
              AND (%(personal)s = 0 OR chat_type = 'user')
            ORDER BY date, chat_id, msg_id
        """
        params = {"since": _bound(since), "until": _bound(until),
                  "personal": int(self.personal_only)}
        for row in self._stream(sql, params):
                (chat_id, msg_id, chat_title, chat_type, sender_name,
                 date, text, media_type, reply_to_id, direction) = row
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
                    actor=sender_name,
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
                )
