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
from chronicle.adapters.firefly import _money
from chronicle.adapters.forgejo import _describe
from chronicle.adapters.owntracks import OwnTracksAdapter
from chronicle.adapters.telegram import TelegramAdapter, _bound
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
    base = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)

    def stored(dt: datetime) -> str:
        # What wakapi's Go driver actually writes: TEXT, space separator,
        # '+00:00' suffix. Declaring the column TIMESTAMP changes nothing —
        # SQLite has no date type.
        return dt.isoformat(sep=" ")

    rows = []
    # two coding blocks on the same project, separated by a 2h break
    for i in range(30):
        rows.append(("yehor", stored(base + timedelta(minutes=2 * i)), "chronicle",
                     "Python", f"f{i%4}.py", "main"))
    for i in range(20):
        rows.append(("yehor", stored(base + timedelta(hours=3, minutes=2 * i)),
                     "chronicle", "Python", "api.py", "main"))
    # a different project
    for i in range(10):
        rows.append(("yehor", stored(base + timedelta(hours=6, minutes=2 * i)),
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


def test_sqlite_bounds_match_their_own_source_format():
    """Two SQLite sources, two incompatible TEXT layouts. Do not unify them.

    wakapi stores '2026-03-01 09:00:00+00:00' (space); telegram stores
    '2026-03-01T09:00:00+00:00' (T). ' ' is 0x20 and 'T' is 0x54, so using
    one source's bound on the other makes `since` compare against every row
    the wrong way and resume silently re-reads or skips the archive.
    """
    from chronicle.adapters.telegram import _bound as tg_bound
    from chronicle.adapters.wakapi import _bound as wk_bound

    when = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    assert wk_bound(when) == "2026-03-01 09:00:00+00:00"
    assert tg_bound(when) == "2026-03-01T09:00:00+00:00"
    assert wk_bound(None) is None and tg_bound(None) is None
    # naive input is UTC, not local — the archive spans 7.6 years of DST
    assert wk_bound(datetime(2026, 3, 1, 9, 0)) == wk_bound(when)


def test_wakapi_is_telemetry_so_it_is_not_segmented(wakapi_db):
    ad = WakapiAdapter(wakapi_db, user="yehor")
    assert ad.density is Density.TELEMETRY
    assert ad.conversational is False, \
        "gap-fitting telemetry produces meaningless thresholds"


# --------------------------------------------------------------------------
#  telegram — SQLite as well, with TEXT timestamps that must sort correctly
# --------------------------------------------------------------------------

@pytest.fixture
def telegram_db(tmp_path) -> str:
    """Mirrors the live telegram-sync schema, verbatim.

    `date` is TEXT and every one of the 682,099 real rows is exactly
    'YYYY-MM-DDTHH:MM:SS+00:00' (verified: length(date)=25 for 100% of rows).
    The fixture stores the same shape, because the bug this guards is a
    STRING comparison, and it only reproduces with the real format.
    """
    db = tmp_path / "telegram.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE messages (
        id INTEGER, chat_id INTEGER, chat_title TEXT, chat_type TEXT,
        sender_id INTEGER, sender_name TEXT, text TEXT, date TEXT,
        is_outgoing INTEGER, reply_to_id INTEGER, synced_at TEXT,
        media_type TEXT, PRIMARY KEY (chat_id, id))""")
    conn.execute("""CREATE VIEW v_messages AS SELECT
        m.chat_id, m.id AS msg_id, m.chat_title, m.chat_type, m.sender_name,
        m.date, m.text, m.media_type, m.reply_to_id,
        CASE WHEN m.is_outgoing = 1 THEN 'sent' ELSE 'received' END AS direction
        FROM messages m""")
    base = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    rows = []
    for chat in (111, 222):
        for i in range(10):
            ts = (base + timedelta(minutes=i)).isoformat()
            rows.append((i, chat, f"chat{chat}", "user", 1, "Yehor",
                         f"msg {i}", ts, i % 2, None, ts, None))
    conn.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return str(db)


def test_telegram_is_sqlite_not_postgres():
    # telegram-sync has no Postgres at all: compose.yaml mounts ./data and the
    # only other store is Qdrant. Assuming otherwise made doctor fail with
    # `missing "=" after "/srv/telegram/telegram.db" in connection info`.
    assert TelegramAdapter.dialect == "sqlite"


def test_telegram_bound_matches_the_stored_text_format():
    got = _bound(datetime(2026, 3, 1, 9, 0))
    assert got == "2026-03-01T09:00:00+00:00"
    # sqlite3's own adapter would render '2026-03-01 09:00:00'. ' ' < 'T', so
    # that bound sorts before every stored row and `since` stops filtering.
    assert "T" in got and got.endswith("+00:00")


def test_telegram_since_filter_actually_filters(telegram_db):
    ad = TelegramAdapter(telegram_db)
    everything = list(ad.fetch())
    assert len(everything) == 20

    high = max(e.watermark_ts for e in everything)
    assert list(ad.fetch(since=high)) == [], \
        "resume re-read the archive — the TEXT bound is not comparing"

    midpoint = everything[len(everything) // 2].ts
    assert 0 < len(list(ad.fetch(since=midpoint))) < 20


def test_telegram_timestamps_are_datetimes_not_text(telegram_db):
    events = list(TelegramAdapter(telegram_db).fetch())
    assert all(isinstance(e.ts, datetime) for e in events)
    assert all(e.ts.tzinfo is not None for e in events), \
        "event.ts is TIMESTAMPTZ; naive values would be read as local time"


def test_telegram_thread_key_partitions_by_chat(telegram_db):
    """Regression: telegram was the only adapter that never set thread_key.

    All 682,099 events would land in the default thread, so fit_thread_gaps
    would fit ONE gap across 491 conversations and segmentation would merge
    unrelated chats by time proximity — which is the entire point of the
    project.
    """
    events = list(TelegramAdapter(telegram_db).fetch())
    assert {e.thread_key for e in events} == {"telegram:111", "telegram:222"}


# --------------------------------------------------------------------------
#  forgejo — the activity feed, whose `content` is JSON and not prose
# --------------------------------------------------------------------------

def test_forgejo_extracts_commit_messages_from_the_json_envelope():
    """A push stores an envelope, not a sentence.

    forgejo is NARRATIVE because commit MESSAGES are deliberate text.
    Indexing `content` verbatim would embed SHA1 hashes and author emails
    and bury the one line that carries meaning.
    """
    content = json.dumps({"Commits": [
        {"Sha1": "bdd7b53", "Message": "fix(listmonk): drop unused env vars",
         "AuthorEmail": "sam@example.com"},
        {"Sha1": "0f5ce5f", "Message": "chore: scope the SMTP key"},
    ]})
    got = _describe(content, 5)
    assert got == "fix(listmonk): drop unused env vars; chore: scope the SMTP key"
    assert "Sha1" not in got and "@" not in got


def test_forgejo_falls_back_to_a_name_never_a_bare_op_type():
    # Measured: 1 of 87 live rows is a push with empty content. `content or
    # op_type` put the integer 5 into indexed text.
    assert _describe("", 5) == "pushed"
    assert _describe(None, 11) == "merged pull request"
    assert _describe("", 99) == "action 99"
    # Non-JSON content is already prose (an issue title, a comment body).
    assert _describe("Fix the flaky test", 10) == "Fix the flaky test"


# --------------------------------------------------------------------------
#  firefly — decimal(32,12), which the driver hands back in full
# --------------------------------------------------------------------------

def test_firefly_amount_drops_the_padding_zeros():
    # '55.000000000000 UAH' is what goes into the embedding otherwise.
    assert _money("55.000000000000") == "55"
    assert _money("55.500000000000") == "55.5"
    assert _money("0.010000000000") == "0.01"
    assert _money("-1234.560000000000") == "-1234.56"


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


# --------------------------------------------------------------------------
#  doctor — catch adapter/schema mismatches BEFORE a multi-week backfill
# --------------------------------------------------------------------------

def test_doctor_reports_unconfigured_sources_as_skip_not_fail(monkeypatch):
    from chronicle.doctor import SKIP, run
    for k in list(__import__("os").environ):
        if k.endswith("_DB_URL") or k.endswith("_DB_PATH"):
            monkeypatch.delenv(k, raising=False)
    rep = run(Tier.CORE)
    assert all(c.status == SKIP for c in rep.checks)
    assert rep.exit_code == 0, "an unconfigured source is not an error"


def test_doctor_accepts_a_healthy_source(wakapi_db, monkeypatch):
    from chronicle.doctor import OK, WARN, check_source
    monkeypatch.setenv("WAKAPI_DB_PATH", wakapi_db)
    monkeypatch.setenv("WAKAPI_USER", "yehor")
    got = check_source("wakapi")
    assert got.status in (OK, WARN), got.detail
    assert got.sample is not None


def test_doctor_catches_unaggregated_telemetry(tmp_path, monkeypatch):
    """The wrong-unit mistake reappearing at the source layer.

    If a TELEMETRY adapter emits raw points instead of rolled-up spans, the
    archive floods. doctor must catch that before the worker runs.
    """
    from chronicle.adapters import Density
    from chronicle.doctor import _validate
    from chronicle.adapters import SourceEvent

    raw = [SourceEvent(source="fake", source_id=str(i),
                       ts=datetime(2026, 3, 1) + timedelta(seconds=10 * i))
           for i in range(20)]
    problems = _validate("fake", raw, Density.TELEMETRY)
    assert any("not rolling up" in p for p in problems), problems


def test_doctor_catches_text_timestamps(tmp_path):
    """The SQLite bug that cost a debugging cycle."""
    from chronicle.adapters import Density, SourceEvent
    from chronicle.doctor import _validate

    rows = [SourceEvent(source="f", source_id=str(i), ts="2026-03-01 09:00:00")
            for i in range(5)]
    problems = _validate("f", rows, Density.DISCRETE)
    assert any("not datetime" in p for p in problems), problems


def test_doctor_catches_import_time_masquerading_as_event_time():
    """The immich bug: createdAt (upload) vs EXIF dateTimeOriginal (capture)."""
    from chronicle.adapters import Density, SourceEvent
    from chronicle.doctor import _validate

    # every 'historical' photo landing within one minute = import timestamp
    base = datetime(2026, 7, 1, 12, 0)
    rows = [SourceEvent(source="immich", source_id=str(i),
                        ts=base + timedelta(seconds=i))
            for i in range(15)]
    problems = _validate("immich", rows, Density.DISCRETE)
    assert any("import time" in p for p in problems), problems


def test_doctor_catches_duplicate_source_ids():
    from chronicle.adapters import Density, SourceEvent
    from chronicle.doctor import _validate

    rows = [SourceEvent(source="f", source_id="same",
                        ts=datetime(2026, 3, 1) + timedelta(hours=i))
            for i in range(4)]
    problems = _validate("f", rows, Density.DISCRETE)
    assert any("duplicate source_id" in p for p in problems), problems


def test_doctor_catches_out_of_order_events():
    from chronicle.adapters import Density, SourceEvent
    from chronicle.doctor import _validate

    rows = [SourceEvent(source="f", source_id=str(i), ts=datetime(2026, 3, 10 - i))
            for i in range(5)]
    problems = _validate("f", rows, Density.DISCRETE)
    assert any("ascending" in p for p in problems), problems


def test_doctor_catches_an_unset_thread_key_but_not_a_single_thread():
    """The bug is an adapter that never ASSIGNS thread_key, not one thread.

    telegram left all 682,099 rows on the SourceEvent default, so one fitted
    gap spanned 491 chats. forgejo legitimately yields one key for all 87 of
    its actions because the forge holds exactly one repository — warning
    about that would just be noise until a second repo showed up.
    """
    from chronicle.adapters import Density, SourceEvent
    from chronicle.doctor import _validate

    base = datetime(2026, 3, 1, tzinfo=timezone.utc)

    def rows(key):
        return [SourceEvent(source="s", source_id=str(i), text="x",
                            ts=base + timedelta(minutes=i), thread_key=key)
                for i in range(25)]

    problems = _validate("telegram", rows("default"), Density.NARRATIVE)
    assert any("default thread" in p for p in problems), problems
    assert not _validate("forgejo", rows("forgejo:homelab-gitops"),
                         Density.NARRATIVE)


def test_doctor_catches_overlong_rollups():
    """Regression: dawarich grouped stays by spatial cluster alone.

    Every visit to the same place merged into one event of 19,163 minutes —
    13.3 days, the whole span of the data. Checks 1-7 all passed it: the
    timestamps were real, ordered, unique and rolled up. Only the span was
    wrong, so the span is what this checks.
    """
    from chronicle.adapters import Density, SourceEvent
    from chronicle.doctor import _validate

    base = datetime(2026, 6, 17, tzinfo=timezone.utc)
    rows = [SourceEvent(source="dawarich", source_id="1", ts=base,
                        watermark_ts=base + timedelta(minutes=19163))]
    problems = _validate("dawarich", rows, Density.TELEMETRY)
    assert any("no time dimension" in p for p in problems), problems

    ok = [SourceEvent(source="dawarich", source_id="1", ts=base,
                      watermark_ts=base + timedelta(minutes=3312))]
    assert not _validate("dawarich", ok, Density.TELEMETRY), \
        "2.3 days is a weekend indoors, not a failed aggregation"


def test_doctor_diagnoses_known_driver_errors():
    from chronicle.doctor import _diagnose
    assert "pymysql" in _diagnose("firefly", ImportError("No module named 'pymysql'"))
    assert "coerce_ts" in _diagnose("wakapi", TypeError(
        "unsupported operand type(s) for -: 'str' and 'str'"))
    assert "schema mismatch" in _diagnose("immich", Exception('relation "assets" does not exist'))
    # A `:ro` bind mount of a WAL database fails to open at all, because WAL
    # must create a -shm file even for a mode=ro connection. telegram.db is
    # WAL, and the old hint sent you looking for a wrong path instead.
    assert "WAL" in _diagnose("telegram", Exception("unable to open database file"))


# --------------------------------------------------------------------------
#  resume watermark — the bug that silently duplicated data on every run
# --------------------------------------------------------------------------

def test_rollup_watermark_is_span_end_not_span_start(wakapi_db):
    """Regression: a second ingest produced a spurious 4th coding session.

    Rollup adapters emit ts = span START. If the worker resumes from that,
    every upstream row inside the span is re-read next run and forms a NEW
    partial span with a DIFFERENT source_id — so `ON CONFLICT DO NOTHING`
    does not catch it and the duplicate accumulates on every scheduled run.
    """
    events = list(WakapiAdapter(wakapi_db, user="yehor").fetch())
    assert events, "fixture produced nothing"
    for e in events:
        assert e.watermark_ts is not None
        assert e.watermark_ts > e.ts, \
            "a rolled-up span must advance the watermark past its own start"
        assert e.watermark_ts.isoformat() == e.payload["ended_at"]


def test_resume_from_watermark_yields_nothing_new(wakapi_db):
    ad = WakapiAdapter(wakapi_db, user="yehor")
    first = list(ad.fetch())
    high = max(e.watermark_ts for e in first)
    again = list(ad.fetch(since=high))
    assert again == [], f"resume re-emitted {len(again)} events"


def test_plain_events_default_watermark_to_ts():
    from chronicle.adapters import SourceEvent
    e = SourceEvent(source="s", source_id="1", ts=datetime(2026, 1, 1))
    assert e.watermark_ts == e.ts


def test_substantive_filter_is_narrative_only():
    """Regression: telemetry segments were all marked non-substantive and so
    were invisible to every query, because the filler-burst heuristic was
    written for Telegram and applied to everything."""
    from chronicle.worker import _substantive
    # a wakapi row: one event, 38 chars — fails the narrative heuristic
    row = ("wakapi", "p:1", datetime(2026, 6, 1), "me",
           "coded on chronicle for 48 min (Python)", None, None, None, None)
    assert _substantive([row]) is False, "heuristic itself is unchanged"
    # ...which is why worker.cmd_segment only applies it when density=narrative.
    import inspect
    from chronicle import worker
    src = inspect.getsource(worker.cmd_segment)
    assert 'density == "narrative" else True' in src


def test_dawarich_eps_is_the_dbscan_radius_not_a_plural_of_segment():
    """`eps` in the dawarich adapter is DBSCAN's epsilon, and it is a psycopg
    NAMED bind — `%(eps)s` in the SQL matched to an "eps" dict key. A rename
    pass that treats it as an abbreviation of the aggregate unit fails at
    RUNTIME with KeyError, not at import, so nothing catches it until the next
    dawarich ingest. Renamed episode -> segment on 2026-09-14; this file was
    excluded from that pass deliberately."""
    src = (Path(__file__).resolve().parent.parent
           / "chronicle" / "adapters" / "dawarich.py").read_text()
    assert "%(eps)s" in src, "dawarich lost its DBSCAN epsilon bind parameter"
    assert '"eps"' in src, "the %(eps)s bind has no matching dict key"
