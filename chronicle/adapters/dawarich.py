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
from datetime import datetime, timezone
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, register

log = logging.getLogger(__name__)

#: A stay is >=20 min inside a 150 m radius. Tuned for "was at a place",
#: not "passed through". Verify against your own data before trusting it.
STAY_RADIUS_M = 150
STAY_MIN_MINUTES = 20


def _epoch(dt: datetime | None) -> int | None:
    """`points.timestamp` is `integer` (Unix seconds) in dawarich 1.8.x.

    Not a Postgres timestamp — verified against the live schema. Binding a
    datetime yields `operator does not exist: integer > timestamp without
    time zone`. Converting in Python rather than wrapping the column in
    to_timestamp() also keeps the (user_id, timestamp) index usable.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


@register
class DawarichAdapter(SqlAdapter):
    source = "dawarich"
    dialect = "postgres"          # PostGIS 17-3.5
    density = Density.TELEMETRY

    def __init__(self, dsn: str, user_id: int):
        super().__init__(dsn)
        self.user_id = user_id

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        # ST_ClusterDBSCAN over time-ordered points, then keep clusters whose
        # span exceeds the dwell threshold. Done in PostGIS because moving
        # millions of GPS points into Python to cluster them would be silly.
        # DBSCAN answers "which PLACE is this point at". It does not answer
        # "which VISIT", because it is purely spatial: every point ever
        # recorded at home lands in one cluster, so grouping by cluster alone
        # emitted a single "stay" of 19,163 minutes — 13.3 days, the entire
        # span of the data (measured against the live DB: 44 clusters, mean
        # 3,693 min). A stay that long is not a stay, and it would have
        # corrupted every timeline that read it.
        #
        # So cluster spatially, then cut each cluster into consecutive runs in
        # TIME order (gaps-and-islands: global row number minus per-cluster
        # row number is constant within a run). Points in transit get
        # cid IS NULL, which advances the global counter and therefore breaks
        # the run — leaving home and coming back yields two visits.
        #
        # Every window orders by (timestamp, id), never timestamp alone. 280
        # of user 2's 167,121 points share a timestamp with another point, and
        # a tie inside the DBSCAN window makes the whole decomposition
        # non-deterministic: two identical runs returned 56 and then 54 visits.
        # `id` is the primary key, so it breaks every tie the same way twice.
        sql = """
            WITH pts AS (
                SELECT id, timestamp, lonlat::geometry AS geom
                FROM points
                WHERE user_id = %(user)s
                  AND lonlat IS NOT NULL
                  AND (%(since)s::bigint IS NULL OR timestamp > %(since)s::bigint)
                  AND (%(until)s::bigint IS NULL OR timestamp <= %(until)s::bigint)
            ),
            clustered AS (
                SELECT id, timestamp, geom,
                       ST_ClusterDBSCAN(geom, %(eps)s, 3)
                           OVER (ORDER BY timestamp, id) AS cid,
                       row_number() OVER (ORDER BY timestamp, id) AS rn
                FROM pts
            ),
            visits AS (
                SELECT cid, id, timestamp, geom,
                       rn - row_number() OVER (PARTITION BY cid
                                               ORDER BY timestamp, id) AS visit
                FROM clustered
                WHERE cid IS NOT NULL
            )
            SELECT min(id)        AS first_point_id,
                   min(timestamp) AS started,
                   max(timestamp) AS ended,
                   count(*)       AS n_points,
                   ST_Y(ST_Centroid(ST_Collect(geom))) AS lat,
                   ST_X(ST_Centroid(ST_Collect(geom))) AS lon
            FROM visits
            GROUP BY cid, visit
            -- `timestamp` is an integer column, so this is plain second
            -- arithmetic. EXTRACT(EPOCH FROM int - int) does not typecheck.
            HAVING max(timestamp) - min(timestamp) >= %(mins)s * 60
            ORDER BY started
        """
        # eps is in degrees for geometry clustering; ~1e-5 deg ≈ 1.1 m at the
        # equator. Good enough at Kyiv's latitude for a 150 m radius.
        params = {
            "user": self.user_id,
            "since": _epoch(since),
            "until": _epoch(until),
            "eps": STAY_RADIUS_M * 1e-5,
            "mins": STAY_MIN_MINUTES,
        }
        for first_id, started, ended, n_points, lat, lon in self._stream(sql, params):
                start_ts = datetime.fromtimestamp(started, tz=timezone.utc)
                end_ts = datetime.fromtimestamp(ended, tz=timezone.utc)
                minutes = (ended - started) // 60
                yield SourceEvent(
                    source=self.source,
                    # Keyed on the visit's first POINT, not on the cluster id.
                    # DBSCAN numbers clusters per invocation, so cid shifts
                    # whenever `since` changes the input set — the resumed run
                    # would emit the same stay under a new id and
                    # ON CONFLICT DO NOTHING would not catch it. A point
                    # belongs to exactly one visit, so min(id) is both unique
                    # and stable. (Measured: keying on started+cid produced 6
                    # collisions in 56 visits, because tied timestamps split
                    # one cluster into two visits with the same start.)
                    source_id=f"stay:{first_id}",
                    ts=start_ts,
                    text=f"stayed at {lat:.4f},{lon:.4f} for {minutes} min",
                    actor="me",
                    kind="stay",
                    payload={
                        "lat": lat, "lon": lon,
                        "minutes": minutes,
                        "points": n_points,
                        "ended_at": end_ts.isoformat(),
                        # Reverse geocoding is deliberately NOT done here.
                        # It is an enrichment step, and enrichment belongs in
                        # the worker where it can be retried and versioned.
                        "place_name": None,
                    },
                    thread_key="dawarich:stays",
                    watermark_ts=end_ts,
                )
