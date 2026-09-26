"""Immich — photos. Visual segments, travel, who you were with.

Immich runs its own Postgres 14 (`immich_postgres`, vectorchord build).

The trap here is burst shots: 40 near-identical frames of the same moment are
40 rows and one memory. Ingesting them raw reproduces the per-message mistake
with pixels. So this adapter clusters by time+place into PHOTO SESSIONS, the
same way wakapi rolls up heartbeats.

EXIF `dateTimeOriginal` is preferred over `createdAt` — the latter is when the
file was uploaded, which for a 2019 photo imported in 2024 is off by five
years and would silently corrupt every timeline it touches.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, register

log = logging.getLogger(__name__)

PHOTO_SESSION_GAP = timedelta(hours=3)


@register
class ImmichAdapter(SqlAdapter):
    source = "immich"
    dialect = "postgres"
    density = Density.TELEMETRY

    def __init__(self, dsn: str, owner_id: str | None = None):
        super().__init__(dsn)
        self.owner_id = owner_id

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        sql = """
            SELECT a.id::text,
                   COALESCE(e."dateTimeOriginal", a."fileCreatedAt") AS taken_at,
                   a.type, a."originalFileName",
                   e.latitude, e.longitude, e.city, e.country, e.model
            -- immich v2 renamed the tables to the singular: `assets` ->
            -- `asset`, `exif` -> `asset_exif` (doctor, 2026-09-26: relation
            -- "assets" does not exist, on immich-server:v2). Columns kept
            -- their names.
            FROM asset a
            LEFT JOIN asset_exif e ON e."assetId" = a.id
            WHERE a."deletedAt" IS NULL
              -- `hidden` is the video half of a live photo, which would
              -- double-count the moment; `locked` is the Locked Folder,
              -- which Yehor hid from immich's own timeline on purpose.
              AND a.visibility::text NOT IN ('hidden', 'locked')
              -- Cast every nullable bound (hard-won fact 16): a bare
              -- placeholder in `IS NULL` is planned as `unknown` and the next
              -- call with a real value dies on a parameter type mismatch.
              -- (No placeholder syntax in these comments: psycopg parses
              -- them too, and one here failed with "parameter missing".)
              AND (%(owner)s::text IS NULL OR a."ownerId"::text = %(owner)s::text)
              AND (%(since)s::timestamptz IS NULL
                   OR COALESCE(e."dateTimeOriginal", a."fileCreatedAt") > %(since)s::timestamptz)
              AND (%(until)s::timestamptz IS NULL
                   OR COALESCE(e."dateTimeOriginal", a."fileCreatedAt") <= %(until)s::timestamptz)
            ORDER BY taken_at
        """
        params = {"owner": self.owner_id, "since": since, "until": until}
        yield from self._cluster(self._stream(sql, params))

    def _cluster(self, rows) -> Iterator[SourceEvent]:
        buf: list[tuple] = []
        last_ts = None

        def flush():
            if not buf:
                return None
            start = buf[0][1]
            places = {r[6] for r in buf if r[6]}
            countries = {r[7] for r in buf if r[7]}
            where = ", ".join(sorted(places | countries)) or "unknown place"
            kinds = {r[2] for r in buf}
            return SourceEvent(
                source=self.source,
                source_id=f"photoset:{start.isoformat()}",
                ts=start,
                text=f"took {len(buf)} photos at {where}",
                actor="me",
                kind="photo_session",
                thread_key="immich:photos",
                payload={
                    "count": len(buf),
                    "asset_ids": [r[0] for r in buf][:200],
                    "places": sorted(places),
                    "countries": sorted(countries),
                    "types": sorted(kinds),
                    "lat": next((r[4] for r in buf if r[4] is not None), None),
                    "lon": next((r[5] for r in buf if r[5] is not None), None),
                    "ended_at": buf[-1][1].isoformat(),
                },
                watermark_ts=buf[-1][1],
            )

        for row in rows:
            ts = row[1]
            if ts is None:
                continue
            if last_ts is not None and ts - last_ts > PHOTO_SESSION_GAP:
                ev = flush()
                if ev:
                    yield ev
                buf = []
            buf.append(row)
            last_ts = ts
        ev = flush()
        if ev:
            yield ev
