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
            SELECT b.id, b.createdAt, coalesce(l.url, t.sourceUrl),
                   coalesce(b.title, l.title), l.description, t.text, b.note
            FROM bookmarks b
            LEFT JOIN bookmarkLinks l ON l.id = b.id
            -- 19 of the 28 live bookmarks are TEXT bookmarks, whose body is
            -- here and nowhere else; without this join they were indexed as ''.
            LEFT JOIN bookmarkTexts t ON t.id = b.id
            WHERE (%(since)s IS NULL OR b.createdAt > %(since)s)
              AND (%(until)s IS NULL OR b.createdAt <= %(until)s)
            ORDER BY b.createdAt
        """
        # SECONDS. Drizzle's `mode: "timestamp"` stores epoch seconds (live:
        # 1781644968 .. 1790404646). The bound was in milliseconds, 1000x
        # past every row, so a resume matched nothing and doctor reported
        # the source dormant on the day it last saved a bookmark.
        params = {
            "since": int(since.timestamp()) if since else None,
            "until": int(until.timestamp()) if until else None,
        }
        for bid, created, url, title, desc, body, note in self._stream(sql, params):
            ts = coerce_ts(created)
            if ts is None:
                continue
            yield SourceEvent(
                source=self.source, source_id=str(bid), ts=ts,
                text="\n".join(x for x in (title, desc, body, note) if x) or (url or ""),
                actor="me", kind="bookmark", thread_key="karakeep:saved",
                payload={"url": url, "title": title, "has_note": bool(note)},
            )
