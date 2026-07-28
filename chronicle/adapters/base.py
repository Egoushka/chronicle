"""Source adapter framework.

Chronicle ingests every channel that carries life signal. The homelab holds
those in five different shapes, so the framework abstracts the shape and not
much else:

    PostgreSQL   telegram-sync, immich, paperless, ghostfolio, miniflux,
                 nextcloud, forgejo, dawarich
    MariaDB      firefly
    SQLite       wakapi, karakeep, changedetection
    Files        owntracks (.rec JSONL)
    HTTP/MCP     gmail, calendar, notion, slack, jira, linkedin, lastfm, github

Two rules that are not negotiable:

1. **Adapters are READ-ONLY against their source.** Chronicle never writes
   back. Those systems own their data; Chronicle owns its projection of it.

2. **Every adapter is independently droppable.** If one rots, Chronicle loses
   a source and keeps working. Anything that makes a source mandatory is a
   design error, and `ingest_all` enforces this by catching per-adapter.

The third rule is the one that is easy to get wrong — see `Density` below.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

log = logging.getLogger(__name__)


class Density(str, Enum):
    """How much meaning a source carries per event.

    This exists because "use all possible channels" has a failure mode that
    looks exactly like the one Chronicle was built to fix.

    The original mistake was embedding 681,331 messages when 65% of them were
    under 20 characters — the wrong UNIT. Adding twenty sources naively
    reproduces it one level up: the wrong SOURCE MIX. miniflux can contribute
    100k article-fetch rows you never opened; changedetection fires on every
    diff; immich has a row per photo including 400 near-identical burst shots.
    Ingest those raw and the archive becomes 5M events of which ~90% is chaff,
    and retrieval precision collapses again.

    So every source declares its density, and the density decides the
    aggregation policy BEFORE anything reaches the episode layer.
    """

    #: Deliberate human utterance. Index at event granularity, segment into
    #: episodes. telegram, gmail, slack, notion.
    NARRATIVE = "narrative"

    #: Discrete, meaningful, low-volume. One row is genuinely one thing that
    #: happened. calendar events, paperless documents, life milestones.
    DISCRETE = "discrete"

    #: Meaningful only in aggregate. Individual rows are noise; rolled-up
    #: spans are signal. wakapi heartbeats, dawarich GPS points, lastfm
    #: scrobbles. The ADAPTER does the rollup — never the episode layer.
    TELEMETRY = "telemetry"

    #: Weak attention signal, high volume. Worth having for "what was I
    #: interested in", not worth full-text indexing. miniflux, changedetection.
    #: Stored, but excluded from the default retrieval surface.
    AMBIENT = "ambient"


@dataclass
class SourceEvent:
    """One timestamped thing that happened, from any channel.

    `text` is what gets indexed. `payload` keeps everything else verbatim so a
    future extraction schema can mine it without re-ingesting from upstream.
    """

    source: str
    source_id: str                    # stable id WITHIN that source
    ts: datetime
    text: str = ""
    actor: str | None = None
    kind: str | None = None
    payload: dict = field(default_factory=dict)
    reply_to: str | None = None

    #: Groups events for segmentation. Telegram uses chat_id, wakapi uses
    #: project, calendar uses a constant. Never None — segmentation needs it.
    thread_key: str = "default"

    #: Resume watermark. Defaults to `ts`, but ROLLUP adapters must set it to
    #: the END of the aggregated span.
    #:
    #: Without this the worker resumes from the span's START, so on the next
    #: run every upstream row inside that span is re-read and forms a NEW
    #: partial span. Measured: a second no-op ingest of 75 wakapi heartbeats
    #: produced a 4th spurious coding session. The duplicate has a different
    #: source_id, so ON CONFLICT DO NOTHING does not catch it — it silently
    #: accumulates on every scheduled run.
    watermark_ts: datetime | None = None

    def __post_init__(self):
        if self.watermark_ts is None:
            self.watermark_ts = self.ts

    def dedupe_key(self) -> str:
        return f"{self.source}:{self.source_id}"


class Adapter(ABC):
    source: str
    density: Density = Density.DISCRETE

    #: Only NARRATIVE sources get time-gap segmentation. A wakapi coding
    #: session or a dawarich stay is ALREADY an episode — its adapter did the
    #: aggregation. Gap-fitting them produces meaningless numbers (measured:
    #: wakapi p90 = 2 days, which clamps to the 6h ceiling and then claims to
    #: be a session boundary).
    @property
    def conversational(self) -> bool:
        return self.density is Density.NARRATIVE

    @abstractmethod
    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        """Yield events in ascending timestamp order.

        Must be resumable: given `since` = last successfully ingested
        timestamp, return everything after and nothing before. The batch
        worker depends on this after an OOM kill, which on a box running
        61.4 GB of committed mem_limit on 32 GB of RAM is a routine event.
        """

    def healthcheck(self) -> tuple[bool, str]:
        try:
            next(iter(self.fetch(until=datetime.min.replace(tzinfo=None))), None)
            return True, "reachable"
        except Exception as exc:                       # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
#  Storage-shape bases
# ---------------------------------------------------------------------------

class SqlAdapter(Adapter):
    """Adapter over a relational source.

    Dialects differ in ways that matter for a multi-million-row scan:

      postgres  server-side named cursors, %(name)s params
      mariadb   SSCursor for unbuffered reads, %(name)s params
      sqlite    no server cursors at all; iterate the cursor directly and
                page with LIMIT/OFFSET on an indexed timestamp

    Getting this wrong is not subtle — a buffered read of wakapi's heartbeat
    table pulls the whole thing into the worker's 8 GB and gets OOM-killed.
    """

    dialect: str = "postgres"          # postgres | mariadb | sqlite
    itersize: int = 5_000

    def __init__(self, dsn: str, **kw: Any):
        self.dsn = dsn
        self.opts = kw

    def _stream(self, sql: str, params: dict) -> Iterator[tuple]:
        if self.dialect == "postgres":
            import psycopg
            with psycopg.connect(self.dsn) as conn:
                with conn.cursor(name=f"{self.source}_stream") as cur:
                    cur.itersize = self.itersize
                    cur.execute(sql, params)
                    yield from cur

        elif self.dialect == "mariadb":
            import pymysql
            from pymysql.cursors import SSCursor
            conn = pymysql.connect(**_parse_mysql_dsn(self.dsn), cursorclass=SSCursor)
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    yield from cur
            finally:
                conn.close()

        elif self.dialect == "sqlite":
            import sqlite3
            # read-only URI so a bug here can never corrupt wakapi's data
            conn = sqlite3.connect(f"file:{self.dsn}?mode=ro", uri=True)
            try:
                conn.row_factory = None
                cur = conn.execute(_to_qmark(sql), _ordered_params(sql, params))
                while batch := cur.fetchmany(self.itersize):
                    yield from batch
            finally:
                conn.close()
        else:
            raise ValueError(f"unknown dialect {self.dialect!r}")


class FileAdapter(Adapter):
    """Adapter over files on disk. owntracks writes JSONL `.rec` files."""

    def __init__(self, root: str | Path, glob: str = "**/*"):
        self.root = Path(root)
        self.glob = glob

    def _iter_files(self) -> Iterator[Path]:
        if not self.root.exists():
            raise FileNotFoundError(f"{self.root} not mounted")
        yield from sorted(p for p in self.root.glob(self.glob) if p.is_file())

    @staticmethod
    def _iter_jsonl(path: Path) -> Iterator[dict]:
        with path.open("r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # owntracks .rec lines are "<iso>\t<type>\t<json>"
                if "\t" in line:
                    line = line.rsplit("\t", 1)[-1]
                if not line.startswith("{"):
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


class ApiAdapter(Adapter):
    """Adapter over an HTTP API or MCP server.

    Used for channels with no local database: gmail, calendar, notion, slack,
    jira, linkedin, lastfm, github.

    These are the sources where a naive full backfill is expensive or
    rate-limited, so `page_window` bounds each pull and the worker resumes
    from `source.last_ingested_at`.
    """

    page_window: timedelta = timedelta(days=30)

    def __init__(self, fetch_page: Callable[..., Any], **kw: Any):
        #: Injected so the adapter never owns credentials. In the homelab this
        #: is a thin wrapper over the corresponding MCP tool.
        self.fetch_page = fetch_page
        self.opts = kw

    def _windows(self, since: datetime | None,
                 until: datetime | None) -> Iterator[tuple[datetime, datetime]]:
        start = since or datetime(2018, 12, 1, tzinfo=None)
        end = until or datetime.now()
        while start < end:
            stop = min(start + self.page_window, end)
            yield start, stop
            start = stop


# ---------------------------------------------------------------------------
#  Registry
# ---------------------------------------------------------------------------

ADAPTERS: dict[str, type[Adapter]] = {}


def register(cls: type[Adapter]) -> type[Adapter]:
    if cls.source in ADAPTERS:
        raise ValueError(f"duplicate adapter source {cls.source!r}")
    ADAPTERS[cls.source] = cls
    return cls


def ingest_all(adapters: list[Adapter], sink, since_for) -> dict[str, Any]:
    """Run every adapter, isolating failures.

    One broken source must never fail the run. This is rule 2 made executable.
    """
    report: dict[str, Any] = {}
    for ad in adapters:
        try:
            n = 0
            for ev in ad.fetch(since=since_for(ad.source)):
                sink(ev)
                n += 1
            report[ad.source] = {"ok": True, "events": n}
            log.info("ingested %s: %d events", ad.source, n)
        except Exception as exc:                        # noqa: BLE001
            report[ad.source] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            log.warning("adapter %s failed (continuing): %s", ad.source, exc)
    return report


# ---------------------------------------------------------------------------
#  dialect helpers
# ---------------------------------------------------------------------------

def coerce_ts(value: Any) -> datetime | None:
    """Normalize whatever the driver returned into a datetime.

    SQLite has no date type: it hands back the TEXT it stored, so any date
    arithmetic downstream raises `unsupported operand type(s) for -: 'str'`.
    Postgres and MariaDB return real datetimes. Epoch ints/floats show up in
    karakeep (millis) and forgejo (seconds).

    Every SQLite-backed adapter must route timestamps through this.
    """
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # Heuristic: anything past ~2001 in seconds is <1e10; millis are >1e11.
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value)
    if isinstance(value, str):
        txt = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(txt)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d"):
                try:
                    return datetime.strptime(txt[:26], fmt)
                except ValueError:
                    continue
        log.warning("unparseable timestamp %r", value)
        return None
    return None


def _parse_mysql_dsn(dsn: str) -> dict:
    """mysql://user:pass@host:port/db -> pymysql kwargs."""
    from urllib.parse import unquote, urlparse
    u = urlparse(dsn)
    return {
        "host": u.hostname or "localhost",
        "port": u.port or 3306,
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "database": (u.path or "/").lstrip("/"),
        "charset": "utf8mb4",
    }


def _to_qmark(sql: str) -> str:
    """%(name)s -> ? so one SQL string works across all three dialects."""
    import re
    return re.sub(r"%\((\w+)\)s", "?", sql)


def _ordered_params(sql: str, params: dict) -> list:
    import re
    return [params[m] for m in re.findall(r"%\((\w+)\)s", sql)]
