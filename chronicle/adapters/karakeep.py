"""Karakeep — bookmarks. What caught your attention, and when.

SQLite on the `karakeep_data` volume. A bookmark is a strong, cheap interest
signal: it is deliberate (unlike an RSS fetch) and timestamped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, coerce_ts, register


@register
class KarakeepAdapter(SqlAdapter):
    source = "karakeep"
    dialect = "sqlite"
    density = Density.DISCRETE

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        sql = """
            SELECT b.id, b.createdAt, l.url, l.title, l.description, b.note
            FROM bookmarks b
            LEFT JOIN bookmarkLinks l ON l.id = b.id
            WHERE (%(since)s IS NULL OR b.createdAt > %(since)s)
              AND (%(until)s IS NULL OR b.createdAt <= %(until)s)
            ORDER BY b.createdAt
        """
        params = {
            "since": since.timestamp() * 1000 if since else None,
            "until": until.timestamp() * 1000 if until else None,
        }
        for bid, created, url, title, desc, note in self._stream(sql, params):
            ts = coerce_ts(created)
            if ts is None:
                continue
            yield SourceEvent(
                source=self.source, source_id=str(bid), ts=ts,
                text="\n".join(x for x in (title, desc, note) if x) or (url or ""),
                actor="me", kind="bookmark", thread_key="karakeep:saved",
                payload={"url": url, "title": title, "has_note": bool(note)},
            )
