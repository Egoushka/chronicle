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
from datetime import datetime
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, register

log = logging.getLogger(__name__)

BATCH = 5_000


@register
class TelegramAdapter(SqlAdapter):
    source = "telegram"
    dialect = "postgres"
    density = Density.NARRATIVE

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
              AND (%(personal)s IS FALSE OR chat_type = 'user')
            ORDER BY date, chat_id, msg_id
        """
        params = {"since": since, "until": until, "personal": self.personal_only}
        for row in self._stream(sql, params):
                (chat_id, msg_id, chat_title, chat_type, sender_name,
                 date, text, media_type, reply_to_id, direction) = row
                yield SourceEvent(
                    source=self.source,
                    source_id=f"{chat_id}:{msg_id}",
                    ts=date,
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
                )
