"""Wakapi adapter — what you actually worked on, by the minute.

Pure SQL, zero NLP, and it answers "what projects was I working on
simultaneously" exactly, from data, with no extraction and no LLM.

This is the adapter that proves the event-store generalization was worth
building: it costs ~80 lines and immediately improves every timeline.

Heartbeats are dense (one every ~2 min while typing), so they are rolled up
into coding *durations* before they reach the segment layer. Emitting raw
heartbeats would reproduce the per-message indexing mistake in a new form.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, coerce_ts, register

log = logging.getLogger(__name__)

#: Gap above which two heartbeats belong to different coding sessions.
#: Wakatime's own convention is 15 minutes; keeping it aligned means
#: Chronicle's numbers reconcile with the Wakapi UI.
HEARTBEAT_GAP = timedelta(minutes=15)


def _bound(dt: datetime | None) -> tuple[str | None, int | None]:
    """Render a datetime BOTH ways wakapi has stored `heartbeats.time`.

    SQLite has no date type, and wakapi has written this column two ways.
    Until mid-2026 it was TEXT, '2026-06-17 13:01:41+00:00' — SPACE separator,
    optional fraction, '+00:00' suffix. On 2.18.0 it is an INTEGER of epoch
    milliseconds: all 18,903 live rows on 2026-09-26, 1781701301000 onward.

    Against a TEXT bound every integer compares LESS (SQLite orders NULL <
    numbers < text < blob), so `time > :since` matched nothing and the
    resumable ingest silently stopped at its 2026-06-18 watermark while the
    server logged thousands of heartbeats. `fetch` compares each row against
    the bound of its own storage class, so either layout — or a table holding
    both — resumes correctly.

    Note the TEXT separator differs from the telegram adapter's 'T'. The bound
    must match ITS OWN source's storage format; there is no shared one.
    """
    if dt is None:
        return None, None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat(sep=" "), int(dt.timestamp() * 1000)


@register
class WakapiAdapter(SqlAdapter):
    source = "wakapi"
    # Wakapi in this homelab is SQLite on a volume, not Postgres. Named
    # server-side cursors do not exist there, and a buffered read of the
    # heartbeat table would pull the whole thing into the worker's 8 GB.
    dialect = "sqlite"
    density = Density.TELEMETRY
    itersize = 20_000

    def __init__(self, dsn: str, user: str):
        super().__init__(dsn)
        self.user = user

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        sql = """
            SELECT time, project, language, entity, branch
            FROM heartbeats
            WHERE user_id = %(user)s
              AND (%(since)s IS NULL OR CASE typeof(time)
                     WHEN 'integer' THEN time > %(since_ms)s
                     ELSE time > %(since)s END)
              AND (%(until)s IS NULL OR CASE typeof(time)
                     WHEN 'integer' THEN time <= %(until_ms)s
                     ELSE time <= %(until)s END)
            ORDER BY time
        """
        (since_txt, since_ms), (until_txt, until_ms) = _bound(since), _bound(until)
        params = {"user": self.user, "since": since_txt, "since_ms": since_ms,
                  "until": until_txt, "until_ms": until_ms}
        yield from self._rollup(self._stream(sql, params))

    def _rollup(self, rows) -> Iterator[SourceEvent]:
        """Collapse dense heartbeats into coding-session durations."""
        cur_project = None
        start = last = None
        langs: set[str] = set()
        files: set[str] = set()

        def emit():
            if cur_project is None or start is None:
                return None
            minutes = max(1, int((last - start).total_seconds() // 60))
            return SourceEvent(
                source=self.source,
                source_id=f"{cur_project}:{start.isoformat()}",
                ts=start,
                text=f"coded on {cur_project} for {minutes} min "
                     f"({', '.join(sorted(langs))})",
                actor=self.user,
                kind="coding_session",
                payload={
                    "project": cur_project,
                    "languages": sorted(langs),
                    "files_touched": len(files),
                    "minutes": minutes,
                    "ended_at": last.isoformat(),
                },
                thread_key=f"wakapi:{cur_project}",
                watermark_ts=last,   # span END, not start — see SourceEvent.watermark_ts
            )

        for raw_ts, project, language, entity, _branch in rows:
            # SQLite hands back TEXT here, Postgres hands back datetime.
            ts = coerce_ts(raw_ts)
            if ts is None:
                continue
            new_block = (
                cur_project is not None
                and (project != cur_project or ts - last > HEARTBEAT_GAP)
            )
            if new_block:
                ev = emit()
                if ev:
                    yield ev
                start, langs, files = ts, set(), set()
            if cur_project != project or start is None:
                start = start or ts
            cur_project = project
            last = ts
            if language:
                langs.add(language)
            if entity:
                files.add(entity)

        ev = emit()
        if ev:
            yield ev
