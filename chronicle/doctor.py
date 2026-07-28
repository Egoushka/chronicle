"""Preflight — validate every configured source BEFORE committing to a backfill.

This module exists because of a specific, repeated failure. Chronicle's
adapters were written against assumptions about the homelab, and two of those
assumptions were wrong on first contact:

    wakapi   assumed PostgreSQL with server-side named cursors.  It is SQLite.
    firefly  assumed amounts on the journal.  They are on `transactions`,
             two signed rows per journal, so the naive join double-counts.
    immich   `createdAt` is the upload time, not the capture time. A 2019
             photo imported in 2024 lands five years out and silently
             corrupts every timeline it touches.

Each of those would have surfaced hours or days into a backfill, after the
worker had already written wrong rows. `doctor` finds them in ~10 seconds.

It is strictly READ-ONLY and touches nothing Chronicle owns. Run it before the
first ingest, and again whenever you upgrade one of the source stacks — an
upstream schema migration is exactly the kind of thing that breaks an adapter
quietly.

    python -m chronicle.doctor              # check the CORE tier
    python -m chronicle.doctor --tier 4     # check everything configured
    python -m chronicle.doctor --json       # machine-readable, for gatus/CI
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .adapters import ADAPTERS, Density
from .sources import BY_SOURCE, Tier, conflicts, enabled

log = logging.getLogger(__name__)

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


@dataclass
class Check:
    source: str
    status: str
    detail: str
    sample: dict[str, Any] | None = None
    hint: str | None = None


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def exit_code(self) -> int:
        return 1 if (self.failed or self.conflicts) else 0


# ---------------------------------------------------------------------------
#  Per-source construction from the environment
# ---------------------------------------------------------------------------

def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def build(source: str):
    """Instantiate an adapter from env, or return None if not configured.

    Unconfigured is not a failure — Chronicle is designed to run with any
    subset of sources. It is only a failure if a source is configured and
    then does not work.
    """
    cls = ADAPTERS.get(source)
    if cls is None:
        return None

    if source == "telegram":
        dsn = _env("TELEGRAM_DB_URL")
        return cls(dsn) if dsn else None
    if source == "wakapi":
        dsn, user = _env("WAKAPI_DB_PATH", "WAKAPI_DB_URL"), _env("WAKAPI_USER")
        return cls(dsn, user=user) if dsn and user else None
    if source == "dawarich":
        dsn, uid = _env("DAWARICH_DB_URL"), _env("DAWARICH_USER_ID")
        return cls(dsn, user_id=int(uid)) if dsn and uid else None
    if source == "immich":
        dsn = _env("IMMICH_DB_URL")
        return cls(dsn, owner_id=_env("IMMICH_OWNER_ID")) if dsn else None
    if source == "paperless":
        dsn = _env("PAPERLESS_DB_URL")
        return cls(dsn) if dsn else None
    if source == "firefly":
        dsn = _env("FIREFLY_DB_URL")
        return cls(dsn) if dsn else None
    if source == "karakeep":
        dsn = _env("KARAKEEP_DB_PATH")
        return cls(dsn) if dsn else None
    if source == "miniflux":
        dsn = _env("MINIFLUX_DB_URL")
        return cls(dsn) if dsn else None
    if source == "forgejo":
        dsn = _env("FORGEJO_DB_URL")
        return cls(dsn, author_email=_env("FORGEJO_EMAIL")) if dsn else None
    if source == "owntracks":
        root = _env("OWNTRACKS_STORE")
        return cls(root) if root else None
    # API adapters need an injected fetch_page; doctor cannot construct them
    # without the MCP wiring, so they are reported as SKIP rather than FAIL.
    return None


# ---------------------------------------------------------------------------
#  Checks
# ---------------------------------------------------------------------------

def check_source(source: str) -> Check:
    policy = BY_SOURCE[source]
    cls = ADAPTERS.get(source)

    if cls is None:
        return Check(source, FAIL, "no adapter registered for a declared policy")

    try:
        ad = build(source)
    except Exception as exc:                              # noqa: BLE001
        return Check(source, FAIL, f"could not construct: {type(exc).__name__}: {exc}",
                     hint="check the DSN format in .env")

    if ad is None:
        kind = "api" if not hasattr(cls, "dialect") else "db"
        return Check(source, SKIP,
                     "not configured" + (" (API adapters need MCP wiring)" if kind == "api" else ""),
                     hint=f"set the {source.upper()}_* vars in .env to enable")

    # Pull a bounded slice rather than the whole history — this must be fast
    # enough to run before every deploy.
    window_start = datetime.now(timezone.utc) - timedelta(days=90)

    def _sample(since):
        rows = []
        for ev in ad.fetch(since=since):
            rows.append(ev)
            if len(rows) >= 25:
                break
        return rows

    try:
        rows = _sample(window_start.replace(tzinfo=None))
        dormant = False
        if not rows:
            # A dormant source must still be VALIDATABLE. Falling back to the
            # full history means we can check the adapter's output shape even
            # for a chat that went quiet in 2023 — otherwise the sources most
            # likely to have drifted are the ones we never check.
            rows = _sample(None)
            dormant = True
    except Exception as exc:                              # noqa: BLE001
        return Check(source, FAIL, f"{type(exc).__name__}: {exc}",
                     hint=_diagnose(source, exc))

    if not rows:
        return Check(source, WARN, "reachable, but produced no events at all",
                     hint="empty upstream, or the query filters everything out")

    problems = _validate(source, rows, policy.density)
    if dormant:
        problems.append("no events in the last 90 days (validated against history "
                        "instead) — expected for a dormant source, suspicious for "
                        "telegram or wakapi")
    ts0 = rows[0].ts
    sample = {"text": (rows[0].text or "")[:120],
              "ts": ts0.isoformat() if isinstance(ts0, datetime) else str(ts0),
              "kind": rows[0].kind, "thread_key": rows[0].thread_key}
    if problems:
        return Check(source, WARN, f"{len(rows)} events, but: " + "; ".join(problems),
                     sample=sample)
    return Check(source, OK, f"{len(rows)} events look sane", sample=sample)


def _validate(source: str, rows: list, density: Density) -> list[str]:
    """Cheap invariants that catch the failure modes seen so far."""
    problems: list[str] = []

    # 1. Timestamps must be datetimes. SQLite hands back TEXT and every date
    #    comparison downstream then raises or silently misorders.
    bad_ts = [r for r in rows if not isinstance(r.ts, datetime)]
    if bad_ts:
        problems.append(f"{len(bad_ts)}/{len(rows)} timestamps are not datetime "
                        f"(got {type(bad_ts[0].ts).__name__}) — route through coerce_ts")

    # 2. Ascending order is a contract the resumable worker depends on.
    ts = [r.ts for r in rows if isinstance(r.ts, datetime)]
    if ts != sorted(ts):
        problems.append("events are not in ascending ts order — resume will skip rows")

    # 3. The immich lesson: upload time masquerading as capture time. Any
    #    source whose 'historical' events all land in a tight recent window is
    #    reporting ingest time, not event time.
    if len(ts) >= 10:
        span = max(ts) - min(ts)
        if span < timedelta(hours=1) and source in {"immich", "paperless", "karakeep"}:
            problems.append(f"all {len(ts)} events within {span} — this looks like "
                            "import time, not event time (immich: use EXIF "
                            "dateTimeOriginal, not createdAt)")

    # 4. Duplicate source_ids break the primary key on `event`.
    ids = [r.source_id for r in rows]
    if len(set(ids)) != len(ids):
        problems.append("duplicate source_id in one batch — the PK will reject these")

    # 5. Telemetry must arrive pre-aggregated. If the adapter is emitting raw
    #    rows, this is the wrong-unit mistake reappearing at the source layer.
    if density is Density.TELEMETRY:
        if len(ts) >= 10:
            gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
            median = sorted(gaps)[len(gaps) // 2] if gaps else 0
            if median < 60:
                problems.append(f"median gap {median:.0f}s between TELEMETRY events — "
                                "the adapter is not rolling up; raw points will "
                                "flood the archive")

    # 6. Narrative sources must actually carry text.
    if density is Density.NARRATIVE:
        empty = sum(1 for r in rows if not (r.text or "").strip())
        if empty > len(rows) * 0.5:
            problems.append(f"{empty}/{len(rows)} narrative events have empty text")

    # 7. thread_key drives segmentation; a constant one merges everything.
    if density is Density.NARRATIVE and len({r.thread_key for r in rows}) == 1 \
            and len(rows) >= 20:
        problems.append("all events share one thread_key — segmentation cannot "
                        "separate conversations")
    return problems


def _diagnose(source: str, exc: Exception) -> str:
    """Turn the common driver errors into an actionable sentence."""
    msg = str(exc).lower()
    name = type(exc).__name__
    if "no module named 'pymysql'" in msg:
        return "firefly is MariaDB: pip install pymysql"
    if "no module named 'psycopg'" in msg:
        return "pip install 'psycopg[binary]'"
    if "unsupported operand" in msg and "str" in msg:
        return "driver returned TEXT timestamps — route through coerce_ts()"
    if "no such table" in msg or "does not exist" in msg:
        return (f"schema mismatch: the upstream {source} stack has a different "
                "table layout than the adapter expects. Check its version, then "
                "fix the query in the adapter — do not guess.")
    if "unable to open database" in msg:
        return "SQLite path wrong, or the volume is not mounted into this container"
    if "connection refused" in msg or "could not connect" in msg:
        return "host unreachable — is the stack up, and is chronicle on its network?"
    if "password authentication failed" in msg:
        return "wrong credentials; re-run decrypt-all.sh"
    return f"{name} — no known diagnosis"


def run(max_tier: Tier = Tier.CORE) -> Report:
    rep = Report()
    policies = enabled(max_tier)
    for p in policies:
        rep.checks.append(check_source(p.source))

    active = {c.source for c in rep.checks if c.status in (OK, WARN)}
    rep.conflicts = conflicts(active)
    return rep


def _print(rep: Report) -> None:
    icons = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "·"}
    width = max((len(c.source) for c in rep.checks), default=10)
    for c in rep.checks:
        print(f"  {icons[c.status]} {c.source:<{width}}  {c.detail}")
        if c.sample and c.status in (OK, WARN):
            print(f"      e.g. [{c.sample['ts'][:16]}] {c.sample['text']!r}")
        if c.hint:
            print(f"      → {c.hint}")
    for msg in rep.conflicts:
        print(f"  ✗ CONFLICT  {msg}")

    n_ok = sum(1 for c in rep.checks if c.status == OK)
    n_warn = sum(1 for c in rep.checks if c.status == WARN)
    print(f"\n  {n_ok} ok · {n_warn} warn · {len(rep.failed)} fail · "
          f"{sum(1 for c in rep.checks if c.status == SKIP)} not configured")
    if rep.exit_code:
        print("\n  Do NOT start a backfill until the failures above are resolved —\n"
              "  a wrong adapter writes wrong rows for as long as it runs.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chronicle.doctor",
                                 description="validate sources before ingesting")
    ap.add_argument("--tier", type=int, default=1, choices=[1, 2, 3, 4],
                    help="check sources up to this tier (default: 1, core)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    rep = run(Tier(args.tier))

    if args.json:
        print(json.dumps({"checks": [asdict(c) for c in rep.checks],
                          "conflicts": rep.conflicts}, indent=2, default=str))
    else:
        print(f"\nchronicle doctor — tier {args.tier}\n")
        _print(rep)
    return rep.exit_code


if __name__ == "__main__":
    sys.exit(main())
