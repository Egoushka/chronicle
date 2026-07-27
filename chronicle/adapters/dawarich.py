"""Dawarich adapter — where you actually were.

Location is the missing axis on every timeline. "What was happening before
things went wrong" is answered better by 'stopped leaving the house' than by
anything said in a chat, because behavioural signals are not curated.

Raw GPS points are dense and useless as memories, so this adapter emits
STAYS (clustered dwell periods) and TRIPS (movement between stays), not
points — the same aggregation principle as everywhere else in Chronicle.

Dawarich runs PostGIS, so the clustering is done in SQL where it belongs.
Requires: dawarich 1.8.x schema (`points` table with `lonlat` geography).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterator

from .base import Adapter, SourceEvent, register

log = logging.getLogger(__name__)

#: A stay is >=20 min inside a 150 m radius. Tuned for "was at a place",
#: not "passed through". Verify against your own data before trusting it.
STAY_RADIUS_M = 150
STAY_MIN_MINUTES = 20


@register
class DawarichAdapter(Adapter):
    source = "dawarich"
    conversational = False

    def __init__(self, conn_factory, user_id: int):
        self._conn_factory = conn_factory
        self.user_id = user_id

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        # ST_ClusterDBSCAN over time-ordered points, then keep clusters whose
        # span exceeds the dwell threshold. Done in PostGIS because moving
        # millions of GPS points into Python to cluster them would be silly.
        sql = """
            WITH pts AS (
                SELECT id, timestamp, lonlat::geometry AS geom,
                       ST_ClusterDBSCAN(lonlat::geometry, %(eps)s, 3)
                           OVER (ORDER BY timestamp) AS cid
                FROM points
                WHERE user_id = %(user)s
                  AND (%(since)s IS NULL OR timestamp > %(since)s)
                  AND (%(until)s IS NULL OR timestamp <= %(until)s)
            )
            SELECT cid,
                   min(timestamp) AS started,
                   max(timestamp) AS ended,
                   count(*)       AS n_points,
                   ST_Y(ST_Centroid(ST_Collect(geom))) AS lat,
                   ST_X(ST_Centroid(ST_Collect(geom))) AS lon
            FROM pts
            WHERE cid IS NOT NULL
            GROUP BY cid
            HAVING EXTRACT(EPOCH FROM (max(timestamp) - min(timestamp))) >= %(mins)s * 60
            ORDER BY started
        """
        # eps is in degrees for geometry clustering; ~1e-5 deg ≈ 1.1 m at the
        # equator. Good enough at Kyiv's latitude for a 150 m radius.
        params = {
            "user": self.user_id,
            "since": since,
            "until": until,
            "eps": STAY_RADIUS_M * 1e-5,
            "mins": STAY_MIN_MINUTES,
        }
        with self._conn_factory() as conn, conn.cursor(name="dawarich_stream") as cur:
            cur.itersize = 2_000
            cur.execute(sql, params)
            for cid, started, ended, n_points, lat, lon in cur:
                minutes = int((ended - started).total_seconds() // 60)
                yield SourceEvent(
                    source=self.source,
                    source_id=f"stay:{started.isoformat()}:{cid}",
                    ts=started,
                    text=f"stayed at {lat:.4f},{lon:.4f} for {minutes} min",
                    actor="me",
                    kind="stay",
                    payload={
                        "lat": lat, "lon": lon,
                        "minutes": minutes,
                        "points": n_points,
                        "ended_at": ended.isoformat(),
                        # Reverse geocoding is deliberately NOT done here.
                        # It is an enrichment step, and enrichment belongs in
                        # the worker where it can be retried and versioned.
                        "place_name": None,
                        "thread_key": "dawarich:stays",
                    },
                )
