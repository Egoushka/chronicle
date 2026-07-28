"""OwnTracks — location, from flat JSONL `.rec` files.

The recorder writes `store/rec/<user>/<device>/YYYY-MM.rec`, one JSON object
per line. No database at all.

Overlaps dawarich, which reads the same underlying signal from PostGIS. Run
one or the other, not both — otherwise every trip is in the timeline twice and
the duplicate reads as corroboration when it is the same GPS fix.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .base import Density, FileAdapter, SourceEvent, register

log = logging.getLogger(__name__)

STAY_MIN = timedelta(minutes=20)
STAY_RADIUS_DEG = 0.0015          # ~150 m


@register
class OwnTracksAdapter(FileAdapter):
    source = "owntracks"
    density = Density.TELEMETRY

    def __init__(self, root: str | Path):
        super().__init__(root, glob="rec/**/*.rec")

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        buf: list[tuple[datetime, float, float]] = []

        def flush():
            if len(buf) < 2:
                return None
            start, end = buf[0][0], buf[-1][0]
            if end - start < STAY_MIN:
                return None
            lat = sum(p[1] for p in buf) / len(buf)
            lon = sum(p[2] for p in buf) / len(buf)
            mins = int((end - start).total_seconds() // 60)
            return SourceEvent(
                source=self.source,
                source_id=f"stay:{start.isoformat()}",
                ts=start,
                text=f"stayed at {lat:.4f},{lon:.4f} for {mins} min",
                actor="me", kind="stay", thread_key="owntracks:stays",
                payload={"lat": lat, "lon": lon, "minutes": mins,
                         "points": len(buf), "ended_at": end.isoformat()},
                watermark_ts=end,
            )

        for path in self._iter_files():
            for rec in self._iter_jsonl(path):
                if rec.get("_type") != "location":
                    continue
                try:
                    ts = datetime.fromtimestamp(rec["tst"], tz=timezone.utc)
                    lat, lon = float(rec["lat"]), float(rec["lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                if since and ts <= since:
                    continue
                if until and ts > until:
                    continue

                moved = buf and (abs(lat - buf[-1][1]) > STAY_RADIUS_DEG
                                 or abs(lon - buf[-1][2]) > STAY_RADIUS_DEG)
                if moved:
                    ev = flush()
                    if ev:
                        yield ev
                    buf = []
                buf.append((ts, lat, lon))

        ev = flush()
        if ev:
            yield ev
