"""Adapter framework tests.

The homelab stores life signal in five shapes. These tests exist because the
first version of this framework assumed everything was Postgres, which was
wrong for wakapi (SQLite), firefly (MariaDB) and owntracks (flat files).
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from chronicle.adapters import ADAPTERS, Density
from chronicle.adapters.base import (_ordered_params, _parse_mysql_dsn,
                                     _to_qmark, ingest_all)
from chronicle.adapters.owntracks import OwnTracksAdapter
from chronicle.adapters.wakapi import WakapiAdapter
from chronicle.sources import BY_SOURCE, NOT_SOURCES, Tier, conflicts, enabled


# --------------------------------------------------------------------------
#  dialect translation — one SQL string must work across three engines
# --------------------------------------------------------------------------

def test_named_params_translate_to_qmark_in_order():
    sql = "SELECT 1 WHERE a > %(since)s AND b <= %(until)s AND c = %(since)s"
    assert _to_qmark(sql).count("?") == 3
    assert "%(" not in _to_qmark(sql)
    got = _ordered_params(sql, {"since": 10, "until": 20})
    assert got == [10, 20, 10], "positional order must follow the SQL, not dict order"


def test_mysql_dsn_parsing():
    got = _parse_mysql_dsn("mysql://ff:p%40ss@firefly-db:3306/firefly")
    assert got["host"] == "firefly-db"
    assert got["port"] == 3306
    assert got["password"] == "p@ss", "percent-encoded passwords must decode"
    assert got["database"] == "firefly"


# --------------------------------------------------------------------------
#  wakapi — SQLite, and heartbeats must roll up
# --------------------------------------------------------------------------

@pytest.fixture
def wakapi_db(tmp_path) -> str:
    db = tmp_path / "wakapi.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE heartbeats (user_id TEXT, time TIMESTAMP, "
                 "project TEXT, language TEXT, entity TEXT, branch TEXT)")
    base = datetime(2026, 3, 1, 9, 0)
    rows = []
    # two coding blocks on the same project, separated by a 2h break
    for i in range(30):
        rows.append(("yehor", base + timedelta(minutes=2 * i), "chronicle",
                     "Python", f"f{i%4}.py", "main"))
    for i in range(20):
        rows.append(("yehor", base + timedelta(hours=3, minutes=2 * i),
                     "chronicle", "Python", "api.py", "main"))
    # a different project
    for i in range(10):
        rows.append(("yehor", base + timedelta(hours=6, minutes=2 * i),
                     "acme", "C#", "Handler.cs", "main"))
    conn.executemany("INSERT INTO heartbeats VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return str(db)


def test_wakapi_is_sqlite_not_postgres():
    # The original version assumed Postgres named cursors, which do not exist
    # in SQLite. Regression guard.
    assert WakapiAdapter.dialect == "sqlite"


def test_wakapi_rolls_heartbeats_into_sessions(wakapi_db):
    events = list(WakapiAdapter(wakapi_db, user="yehor").fetch())
    # 60 heartbeats -> 3 coding sessions, not 60 events.
    assert len(events) == 3, [e.text for e in events]
    assert all(e.kind == "coding_session" for e in events)
    assert {e.payload["project"] for e in events} == {"chronicle", "acme"}
    assert all(e.payload["minutes"] > 0 for e in events)
    # thread_key partitions by project so segmentation never merges them
    assert events[-1].thread_key == "wakapi:acme"


def test_wakapi_is_telemetry_so_it_is_not_segmented(wakapi_db):
    ad = WakapiAdapter(wakapi_db, user="yehor")
    assert ad.density is Density.TELEMETRY
    assert ad.conversational is False, \
        "gap-fitting telemetry produces meaningless thresholds"


# --------------------------------------------------------------------------
#  owntracks — flat JSONL, no database at all
# --------------------------------------------------------------------------

def test_owntracks_reads_rec_files_and_emits_stays(tmp_path):
    rec = tmp_path / "rec" / "yehor" / "phone"
    rec.mkdir(parents=True)
    f = rec / "2026-03.rec"
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    lines = []
    for i in range(40):                       # 40 min in one spot -> a stay
        lines.append(f"{base.isoformat()}\t*\t" + json.dumps(
            {"_type": "location", "tst": int((base + timedelta(minutes=i)).timestamp()),
             "lat": 50.4501, "lon": 30.5234}))
    for i in range(5):                        # then move away
        lines.append(f"{base.isoformat()}\t*\t" + json.dumps(
            {"_type": "location", "tst": int((base + timedelta(minutes=60 + i)).timestamp()),
             "lat": 50.5000, "lon": 30.6000}))
    f.write_text("\n".join(lines))

    events = list(OwnTracksAdapter(tmp_path).fetch())
    assert len(events) >= 1
    stay = events[0]
    assert stay.kind == "stay"
    assert stay.payload["minutes"] >= 20
    assert round(stay.payload["lat"], 3) == 50.450


def test_owntracks_survives_corrupt_lines(tmp_path):
    rec = tmp_path / "rec" / "u" / "d"
    rec.mkdir(parents=True)
    (rec / "x.rec").write_text("garbage\n\n{not json}\n" + json.dumps(
        {"_type": "location", "tst": 1772000000, "lat": 1.0, "lon": 2.0}))
    # A truncated line in a 7-year archive must not kill the run.
    list(OwnTracksAdapter(tmp_path).fetch())


# --------------------------------------------------------------------------
#  isolation — one broken source must never fail the run
# --------------------------------------------------------------------------

class _Boom:
    source = "boom"
    def fetch(self, since=None, until=None):
        raise RuntimeError("upstream is down")


class _Fine:
    source = "fine"
    def fetch(self, since=None, until=None):
        from chronicle.adapters import SourceEvent
        yield SourceEvent(source="fine", source_id="1", ts=datetime(2026, 1, 1))


def test_a_broken_adapter_does_not_fail_the_run():
    got = []
    report = ingest_all([_Boom(), _Fine()], got.append, lambda s: None)
    assert report["boom"]["ok"] is False
    assert report["fine"]["ok"] is True and report["fine"]["events"] == 1
    assert len(got) == 1, "the healthy source must still have been ingested"


# --------------------------------------------------------------------------
#  policy — the thing that stops "all channels" becoming noise
# --------------------------------------------------------------------------

def test_every_registered_adapter_has_a_policy():
    missing = set(ADAPTERS) - set(BY_SOURCE)
    assert not missing, f"adapters with no declared policy: {missing}"


def test_policy_density_matches_adapter_density():
    for name, cls in ADAPTERS.items():
        assert cls.density is BY_SOURCE[name].density, name


def test_core_tier_is_small():
    core = enabled(Tier.CORE)
    assert len(core) <= 5, \
        "CORE is meant to be the few sources that carry most of the value"
    assert {p.source for p in core} >= {"telegram", "wakapi", "dawarich"}


def test_infrastructure_stacks_are_explicitly_not_sources():
    # Documented boundary: "all channels" does not mean all 57 stacks.
    assert "monitoring" in NOT_SOURCES
    assert "qdrant" in NOT_SOURCES
    assert not set(ADAPTERS) & NOT_SOURCES, \
        "a stack cannot be both an adapter and explicitly not a source"


def test_dawarich_and_owntracks_conflict_is_detected():
    assert conflicts({"dawarich", "owntracks"})
    assert not conflicts({"dawarich"})


def test_ambient_sources_are_last():
    for p in enabled(Tier.CORE) + enabled(Tier.BEHAVIOUR):
        assert p.density is not Density.AMBIENT
