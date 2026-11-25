"""Oura adapter: one event per day per kind from the Oura warehouse.

The warehouse (oura-platform, Postgres/TimescaleDB) already holds the daily
scores and the 5-minute series. Chronicle wants the day, not the sample: a
heart-rate row every few minutes would be the per-message mistake again in a
new unit, so the adapter emits at most four compact events a day:

    sleep      "Sleep 7h12, score 81, HRV 54"
    readiness  "Readiness 79, resting HR 52, temp +0.1"
    activity   "Activity 88, 9,412 steps, 540 active kcal"
    heart      "Heart rate avg 71, min 46, max 142 bpm"

Only COMPLETE days are read (day < today, UTC): `daily` is upserted while the
day is still running, and `ON CONFLICT DO NOTHING` would freeze a half-day
row at whatever the first read saw.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, register

#: Event time inside the day. Scores describe the night that ended that
#: morning, so sleep and readiness sit early; activity and heart rate describe
#: the day and sit late. Distinct minutes keep (source, source_id, ts) unique
#: and the stream ascending by construction.
_AT = {"sleep": time(7, 0), "readiness": time(7, 1),
       "activity": time(21, 0), "heart": time(21, 1)}


def _hm(seconds: int | None) -> str | None:
    if not seconds:
        return None
    return f"{int(seconds) // 3600}h{int(seconds) % 3600 // 60:02d}"


def _num(v, nd: int = 0) -> str | None:
    if v is None:
        return None
    return f"{float(v):.{nd}f}"


def _join(label: str, parts: list[str | None]) -> str:
    return f"{label} " + ", ".join(p for p in parts if p)


@register
class OuraAdapter(SqlAdapter):
    source = "oura"
    dialect = "postgres"
    # One row is one day: nothing to roll up, nothing to segment.
    density = Density.DISCRETE

    def __init__(self, dsn: str):
        from psycopg.conninfo import make_conninfo
        # Read-only at the session, not only by promise: the warehouse role is
        # the writer's own.
        super().__init__(make_conninfo(dsn, options="-c default_transaction_read_only=on"))

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        today = datetime.now(timezone.utc).date()
        floor = since.astimezone(timezone.utc).date() if since else None
        params = {"today": today, "since": floor}
        days = self._stream("""
            SELECT d.day, d.sleep_score, d.readiness_score, d.activity_score,
                   d.hrv_avg, d.rhr_lowest, d.temp_deviation, d.steps,
                   d.active_calories,
                   (SELECT sum((s.payload->>'total_sleep_duration')::int)
                      FROM sleep_sessions s
                     WHERE s.night = d.day AND s.payload->>'type' = 'long_sleep')
            FROM daily d
            WHERE d.day < %(today)s::date
              AND (%(since)s::date IS NULL OR d.day >= %(since)s::date)
            ORDER BY d.day
        """, params)
        hr = {r[0]: r[1:] for r in self._stream("""
            SELECT day, sum(avg_bpm * samples) / nullif(sum(samples), 0),
                   min(min_bpm), max(max_bpm)
            FROM hr_daily
            WHERE day < %(today)s::date
              AND (%(since)s::date IS NULL OR day >= %(since)s::date)
            GROUP BY day
        """, params)}

        for (day, sleep, ready, act, hrv, rhr, temp, steps, kcal, slept) in days:
            rows = {
                "sleep": (sleep is not None or slept) and _join("Sleep", [
                    _hm(slept), f"score {sleep}" if sleep is not None else None,
                    f"HRV {_num(hrv)}" if hrv is not None else None]),
                "readiness": ready is not None and _join("Readiness", [
                    str(ready), f"resting HR {_num(rhr)}" if rhr is not None else None,
                    f"temp {float(temp):+.1f}" if temp is not None else None]),
                "activity": act is not None and _join("Activity", [
                    str(act), f"{steps:,} steps" if steps is not None else None,
                    f"{kcal} active kcal" if kcal is not None else None]),
            }
            if day in hr and hr[day][0] is not None:
                avg, lo, hi = hr[day]
                rows["heart"] = f"Heart rate avg {_num(avg)}, min {lo}, max {hi} bpm"
            for kind in ("sleep", "readiness", "activity", "heart"):
                text = rows.get(kind)
                if not text:
                    continue
                ts = datetime.combine(day, _AT[kind], tzinfo=timezone.utc)
                if (since and ts <= since) or (until and ts > until):
                    continue
                yield SourceEvent(
                    source=self.source, source_id=f"{kind}:{day.isoformat()}", ts=ts,
                    text=text, actor="me", kind=kind,
                    payload={"day": day.isoformat()}, thread_key=f"oura:{kind}")


