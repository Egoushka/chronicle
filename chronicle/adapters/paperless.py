"""Paperless-ngx — documents. Contracts, receipts, official life events.

Postgres 16. Already OCR'd, so `content` is real text and this is one of the
few sources where a single row is genuinely one meaningful thing.

`created` is the document's own date (when the contract was signed), not the
scan date. Use it — a 2021 lease imported in 2024 belongs in 2021.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, register


@register
class PaperlessAdapter(SqlAdapter):
    source = "paperless"
    dialect = "postgres"
    density = Density.DISCRETE

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        sql = """
            SELECT d.id, d.created, d.title, d.content,
                   c.name AS correspondent,
                   dt.name AS doctype
            FROM documents_document d
            LEFT JOIN documents_correspondent c ON c.id = d.correspondent_id
            LEFT JOIN documents_documenttype  dt ON dt.id = d.document_type_id
            -- Cast every nullable bound (hard-won fact 16). `created` is a
            -- DATE on current paperless-ngx, so the bare bound was planned
            -- as `date` on one use and `unknown` on the other.
            WHERE (%(since)s::timestamptz IS NULL OR d.created > %(since)s::timestamptz)
              AND (%(until)s::timestamptz IS NULL OR d.created <= %(until)s::timestamptz)
            ORDER BY d.created, d.id
        """
        for doc_id, created, title, content, corr, doctype in self._stream(
                sql, {"since": since, "until": until}):
            # OCR output can be enormous; the head carries the identifying
            # detail and the rest stays in paperless where it belongs.
            body = (content or "")[:4000]
            if not isinstance(created, datetime):
                # A DATE comes back as datetime.date: midnight UTC of that day.
                created = datetime(created.year, created.month, created.day,
                                   tzinfo=timezone.utc)
            yield SourceEvent(
                source=self.source,
                source_id=str(doc_id),
                ts=created,
                text=f"{title or 'document'}\n{body}",
                actor=corr,
                kind="document",
                thread_key="paperless:docs",
                payload={"title": title, "correspondent": corr,
                         "doctype": doctype, "truncated": len(content or "") > 4000},
            )
