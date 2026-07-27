"""Miniflux — RSS. Information diet.

Postgres 16. Marked AMBIENT deliberately: an unread article is not a memory.
Only entries you actually READ or STARRED are ingested, because the unread
firehose is exactly the noise that would drown the archive — thousands of rows
carrying no evidence you ever encountered them.

This is the source where "use all possible channels" most needs a filter.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, register


@register
class MinifluxAdapter(SqlAdapter):
    source = "miniflux"
    dialect = "postgres"
    density = Density.AMBIENT

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        sql = """
            SELECT e.id, e.published_at, e.title, e.url, f.title AS feed,
                   e.status, e.starred
            FROM entries e
            JOIN feeds f ON f.id = e.feed_id
            WHERE (e.status = 'read' OR e.starred)     -- evidence you saw it
              AND (%(since)s IS NULL OR e.published_at > %(since)s)
              AND (%(until)s IS NULL OR e.published_at <= %(until)s)
            ORDER BY e.published_at
        """
        for eid, pub, title, url, feed, status, starred in self._stream(
                sql, {"since": since, "until": until}):
            yield SourceEvent(
                source=self.source, source_id=str(eid), ts=pub,
                text=f"{title} ({feed})", actor="me",
                kind="article_read", thread_key=f"miniflux:{feed}",
                payload={"url": url, "feed": feed, "starred": bool(starred)},
            )
