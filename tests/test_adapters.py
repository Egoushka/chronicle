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

    wakapi stores '2026-03-01 09:00:00+00:00' (space); telegram's resume
    column, synced_at, stores '2026-03-01T09:00:00.000000+00:00' (T, and a
    fraction). ' ' is 0x20 and 'T' is 0x54, so using
    one source's bound on the other makes `since` compare against every row
    the wrong way and resume silently re-reads or skips the archive.
    """
    from chronicle.adapters.telegram import _bound as tg_bound
    from chronicle.adapters.wakapi import _bound as wk_bound

    when = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    assert wk_bound(when) == ("2026-03-01 09:00:00+00:00", 1772355600000)
    assert tg_bound(when) == "2026-03-01T09:00:00.000000+00:00"
    assert wk_bound(None) == (None, None) and tg_bound(None) is None
    # naive input is UTC, not local — the archive spans 7.6 years of DST
    assert wk_bound(datetime(2026, 3, 1, 9, 0)) == wk_bound(when)


def test_wakapi_resumes_over_integer_millisecond_heartbeats(tmp_path):
    """wakapi 2.18 stores `time` as INTEGER epoch millis, not TEXT.

    SQLite orders every integer below every text value, so a TEXT bound made
    `time > :since` false for all 18,903 live rows: ingest sat at its June
    watermark for three months while heartbeats kept arriving.
    """
    db = tmp_path / "wakapi.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE heartbeats (user_id TEXT, time TIMESTAMP, "
                 "project TEXT, language TEXT, entity TEXT, branch TEXT)")
    base = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    conn.executemany("INSERT INTO heartbeats VALUES (?,?,?,?,?,?)", [
        ("yehor", int((base + timedelta(hours=h, minutes=2 * i)).timestamp() * 1000),
         "chronicle", "Python", "worker.py", "main")
        for h in (0, 3) for i in range(10)])
    conn.commit()
    conn.close()

    ad = WakapiAdapter(str(db), user="yehor")
    assert len(list(ad.fetch())) == 2
    resumed = list(ad.fetch(since=base + timedelta(hours=1)))
    assert [e.ts for e in resumed] == [base + timedelta(hours=3)], \
        "an integer-stored heartbeat compared against a TEXT bound"
    assert resumed[0].ts.tzinfo is not None, "epoch ints must decode as UTC"


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
    # `chats` and `chat_tags` live outside v_messages, which is why a bot
    # filter has to join. Same columns as the live telegram.db.
    conn.execute("""CREATE TABLE chats (
        chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT, username TEXT,
        included INTEGER, updated_at TEXT)""")
    conn.execute("""CREATE TABLE chat_tags (
        chat_id INTEGER NOT NULL, tag TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (chat_id, tag))""")
    base = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    rows = []
    # 111 human. 222 human. 333 a bot by username (Telegram requires every bot
    # username to end in "bot"). 444 a bot with NO bot-shaped username, reachable
    # only through telegram-sync's own `chat:bot` tag — BotFather is the real
    # instance of this. 555 has no `chats` row at all: 33 such orphan chat_ids
    # carry 1,165 messages in the live database and an INNER JOIN drops them.
    conn.executemany("INSERT INTO chats VALUES (?,?,?,?,?,?)", [
        (111, "chat111", "user", "anna", 1, ""),
        (222, "chat222", "user", "talbot_the_human", 1, ""),
        (333, "chat333", "user", "jarvis_bot", 1, ""),
        (444, "chat444", "user", "BotFather", 1, ""),
    ])
    conn.execute("INSERT INTO chat_tags (chat_id, tag, source) VALUES (444, 'chat:bot', 'auto')")
    for chat in (111, 222, 333, 444, 555):
        for i in range(10):
            ts = (base + timedelta(minutes=i)).isoformat()
            # synced_at in its live shape: always a fraction (see _bound).
            synced = (base + timedelta(minutes=i)).isoformat(timespec="microseconds")
            rows.append((i, chat, f"chat{chat}", "user", 1, "Yehor",
                         f"msg {i}", ts, i % 2, None, synced, None))
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
    assert got == "2026-03-01T09:00:00.000000+00:00"
    # sqlite3's own adapter would render '2026-03-01 09:00:00'. ' ' < 'T', so
    # that bound sorts before every stored row and `since` stops filtering.
    assert "T" in got and got.endswith("+00:00")
    # And isoformat() alone drops the fraction at microsecond 0, which puts
    # '+' against a stored '.': a whole second of rows on the wrong side.
    assert len(got) == len("2026-03-01T09:00:00.123456+00:00")


def test_telegram_since_filter_actually_filters(telegram_db, monkeypatch):
    monkeypatch.setattr("chronicle.adapters.telegram.SYNC_LAG", timedelta(0))
    ad = TelegramAdapter(telegram_db, exclude_bot_chats=False)
    everything = list(ad.fetch())
    assert len(everything) == 50

    high = max(e.watermark_ts for e in everything)
    assert list(ad.fetch(since=high)) == [], \
        "resume re-read the archive — the TEXT bound is not comparing"

    midpoint = sorted(e.watermark_ts for e in everything)[25]
    assert 0 < len(list(ad.fetch(since=midpoint))) < 50


def test_telegram_resume_overlaps_by_sync_lag(telegram_db):
    """A row stamped before the watermark but committed after it is re-read.

    telegram-sync stamps synced_at before its batch commits, so a strict
    `> watermark` can skip a row forever. Re-reading costs nothing: the
    worker's upsert is a no-op when the text is unchanged.
    """
    from chronicle.adapters.telegram import SYNC_LAG
    ad = TelegramAdapter(telegram_db, exclude_bot_chats=False)
    high = max(e.watermark_ts for e in ad.fetch())
    assert list(ad.fetch(since=high)), "no overlap: a late-committing row is lost"
    assert list(ad.fetch(since=high + SYNC_LAG)) == []


def test_telegram_resumes_on_write_time_not_message_date(telegram_db):
    """A transcript landing on an OLD voice note must be picked up.

    telegram-sync's upsert rewrites `text` and `synced_at` together. Resuming
    on the message date skipped every such row: 4,544 voice notes were
    transcribed after the first ingest and none ever reached chronicle.
    """
    ad = TelegramAdapter(telegram_db, exclude_bot_chats=False)
    high = max(e.watermark_ts for e in ad.fetch())

    later = (high + timedelta(days=30)).isoformat(timespec="microseconds")
    conn = sqlite3.connect(telegram_db)
    conn.execute("UPDATE messages SET text = '[voice] привіт', synced_at = ?, "
                 "media_type = 'voice' WHERE chat_id = 111 AND id = 0", (later,))
    conn.commit()
    conn.close()

    changed = list(ad.fetch(since=high + timedelta(days=1)))
    assert [(e.source_id, e.text) for e in changed] == [("111:0", "[voice] привіт")]
    assert changed[0].ts < high, "ts must stay the message date"
    assert changed[0].watermark_ts > high, "the watermark must be the write time"


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
    events = list(TelegramAdapter(telegram_db, exclude_bot_chats=False).fetch())
    assert {e.thread_key for e in events} == {
        "telegram:111", "telegram:222", "telegram:333",
        "telegram:444", "telegram:555"}


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
    # ...which is why the worker only applies it when density=narrative.
    from chronicle.worker import _segment_fields
    substantive = 7          # index in the insert tuple
    assert _segment_fields([row], "wakapi:p", "telemetry")[substantive] is True
    assert _segment_fields([row], "wakapi:p", "narrative")[substantive] is False


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


def test_telegram_excludes_bot_chats_by_default(telegram_db):
    """A bot DM is a chat_type='user' chat, so `personal_only` never kept bots
    out — chat_type is 'user' for 100% of the 690,177 live rows.

    Without this filter an assistant's own Telegram chat flows telegram-sync ->
    chronicle -> back to the assistant through chronicle's MCP, and it reads
    its own output as external memory about Yehor. sources.py NOT_SOURCES
    already blocks tg-assistant, agent-runner and hindsight at the STACK level;
    this is the same rule one level down, at the ROW.
    """
    threads = {e.thread_key for e in TelegramAdapter(telegram_db).fetch()}
    assert "telegram:333" not in threads, "bot-shaped username was not excluded"
    assert "telegram:444" not in threads, "`chat:bot`-tagged chat was not excluded"
    assert {"telegram:111", "telegram:222"} <= threads, "human chats were dropped"


def test_telegram_bot_exclusion_needs_both_signals(telegram_db):
    """Neither signal is a superset of the other, measured on the live archive:
    92 chats match the username rule, 29 carry the `chat:bot` tag, and the
    overlap is partial — the tag reaches BotFather and Crypto Bot, which have
    no bot-shaped username at all."""
    ids = {int(t.split(":")[1]) for t in
           {e.thread_key for e in TelegramAdapter(telegram_db).fetch()}}
    assert 333 not in ids, "username rule contributes nothing"
    assert 444 not in ids, "chat:bot tag contributes nothing"


def test_telegram_keeps_chats_with_no_chats_row(telegram_db):
    """LEFT JOIN, never INNER. 33 chat_ids in the live `messages` table have no
    `chats` row and carry 1,165 messages between them; an inner join deletes
    them silently, which is data loss wearing a filter's clothes."""
    threads = {e.thread_key for e in TelegramAdapter(telegram_db).fetch()}
    assert "telegram:555" in threads, \
        "orphan chat_id dropped — the join went INNER"


def test_telegram_explicit_chat_id_exclusion(telegram_db):
    """The escape hatch, for what the rule misses or gets wrong. Prefer the
    username rule where it works: a chat_id changes when a bot is recreated,
    a username does not."""
    ad = TelegramAdapter(telegram_db, exclude_chat_ids=(111,))
    threads = {e.thread_key for e in ad.fetch()}
    assert "telegram:111" not in threads
    assert "telegram:222" in threads


def test_telegram_exclude_chat_ids_are_coerced_to_int(telegram_db):
    """These are inlined into the SQL rather than bound, because a
    variable-length IN list cannot be written once across pyformat and qmark.
    int() is the entire reason that is safe."""
    ad = TelegramAdapter(telegram_db, exclude_chat_ids=("111",))
    assert ad.exclude_chat_ids == (111,)
    with pytest.raises(ValueError):
        TelegramAdapter(telegram_db, exclude_chat_ids=("111 OR 1=1",))


# --------------------------------------------------------------------------
#  excluded_thread_keys — the purge's half of the exclusion rule
# --------------------------------------------------------------------------

def test_excluded_thread_keys_is_exactly_the_complement_of_fetch(telegram_db):
    """The invariant the purge rests on, and the one that rots silently.

    Filtering is fetch-time, so tightening a rule leaves everything the old
    rule already indexed retrievable forever. `chronicle.purge` deletes the
    difference — which is only correct while this method selects exactly the
    chats `fetch` refuses. Two hand-written SQL statements over the same tables
    is precisely the shape that drifts, so assert it rather than trusting it.
    """
    ad = TelegramAdapter(telegram_db, exclude_chat_ids=(111,))
    kept = {e.thread_key for e in ad.fetch()}
    excluded = ad.excluded_thread_keys()

    all_chats = {f"telegram:{c}" for c in (111, 222, 333, 444, 555)}
    assert kept | excluded == all_chats, "some chat is in neither half"
    assert kept & excluded == set(), "a chat is both fetched and purged"
    assert excluded == {"telegram:111", "telegram:333", "telegram:444"}


def test_excluded_thread_keys_unions_both_bot_signals(telegram_db):
    """333 is bot-shaped by username, 444 only by the `chat:bot` tag. Either
    signal alone leaves live rows behind: 92 chats match the username rule and
    29 carry the tag, and the union is 5,806 messages."""
    excluded = TelegramAdapter(telegram_db).excluded_thread_keys()
    assert excluded == {"telegram:333", "telegram:444"}


def test_excluded_thread_keys_is_empty_when_nothing_is_excluded(telegram_db):
    """An adapter that filters nothing must purge nothing. The failure mode
    this guards is a purge that deletes the whole corpus because the rule
    inverted."""
    ad = TelegramAdapter(telegram_db, exclude_bot_chats=False)
    assert ad.excluded_thread_keys() == set()


def test_every_adapter_answers_excluded_thread_keys():
    """Base-class default is the empty set, so a new adapter cannot crash the
    purge — but one that filters and never overrides this leaves its old rows
    indexed forever, which is the bug the whole target exists to fix."""
    from chronicle.adapters import ADAPTERS
    from chronicle.adapters.base import Adapter
    for name, cls in ADAPTERS.items():
        assert hasattr(cls, "excluded_thread_keys"), name
    assert Adapter.excluded_thread_keys(object()) == set()


# --------------------------------------------------------------------------
#  API adapters
# --------------------------------------------------------------------------

def test_api_windows_resume_from_an_aware_watermark():
    """`source.last_ingested_at` is timestamptz, so the SECOND run hands the
    adapter an aware `since`. Naive defaults made `start < end` raise, i.e.
    every API adapter failed the first time it resumed."""
    from chronicle.adapters.api_sources import LastfmAdapter
    ad = LastfmAdapter(fetch_page=lambda **kw: [])
    since = datetime(2026, 9, 1, tzinfo=timezone.utc)
    spans = list(ad._windows(since, datetime(2026, 12, 15)))    # naive until: UTC
    assert spans[0][0] == since and spans[-1][1].tzinfo is not None
    assert list(ad.fetch(since=since)) == []


def test_lastfm_pages_walks_pages_and_skips_now_playing():
    from chronicle.adapters.api_sources import LastfmAdapter, lastfm_pages

    def track(uts, artist):
        return {"artist": {"#text": artist}, "name": "t", "date": {"uts": str(uts)}}

    base = int(datetime(2026, 9, 1, 20, tzinfo=timezone.utc).timestamp())
    pages = {1: [{"artist": {"#text": "now"}, "name": "playing"},      # no date
                 track(base, "Okean Elzy"), track(base + 200, "Okean Elzy")],
             2: [track(base + 400, "DakhaBrakha")]}
    seen = []

    class Http:
        def get(self, url, params):
            seen.append(params)
            body = {"recenttracks": {"track": pages[params["page"]],
                                     "@attr": {"totalPages": "2"}}}
            return type("R", (), {"raise_for_status": lambda s: None,
                                  "json": lambda s: body})()

    fetch = lastfm_pages("k", "u", http=Http())
    got = fetch(start=datetime(2026, 9, 1, tzinfo=timezone.utc),
                stop=datetime(2026, 9, 2, tzinfo=timezone.utc))
    assert [p["page"] for p in seen] == [1, 2]
    assert [t["artist"] for t in got] == ["Okean Elzy", "Okean Elzy", "DakhaBrakha"]
    assert all(t["played_at"].tzinfo is not None for t in got)

    # ...and the three scrobbles roll up into one listening session.
    [session] = LastfmAdapter(fetch_page=lambda **kw: got)._rollup(got)
    assert session.payload["tracks"] == 3 and session.payload["top_artist"] == "Okean Elzy"
    assert session.watermark_ts > session.ts, "rollups resume from the span END"


def test_karakeep_resumes_in_seconds_and_reads_text_bookmarks(tmp_path):
    """karakeep stores createdAt in epoch SECONDS; the bound was millis, so a
    resume matched nothing. And a text bookmark's body lives only in
    bookmarkTexts — 19 of 28 live bookmarks were being indexed as ''."""
    from chronicle.adapters.karakeep import KarakeepAdapter
    db = tmp_path / "db.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE bookmarks (id TEXT, createdAt INTEGER, title TEXT, "
                 "note TEXT, type TEXT)")
    conn.execute("CREATE TABLE bookmarkLinks (id TEXT, url TEXT, title TEXT, description TEXT)")
    conn.execute("CREATE TABLE bookmarkTexts (id TEXT, text TEXT, sourceUrl TEXT)")
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    conn.executemany("INSERT INTO bookmarks VALUES (?,?,?,?,?)", [
        ("a", int(base.timestamp()), None, None, "link"),
        ("b", int((base + timedelta(days=2)).timestamp()), None, None, "text")])
    conn.execute("INSERT INTO bookmarkLinks VALUES ('a', 'https://x', 'A page', NULL)")
    conn.execute("INSERT INTO bookmarkTexts VALUES ('b', 'idea: segment by reply chains', NULL)")
    conn.commit()
    conn.close()

    ad = KarakeepAdapter(str(db))
    assert [e.text for e in ad.fetch()] == ["A page", "idea: segment by reply chains"]
    assert [e.source_id for e in ad.fetch(since=base + timedelta(days=1))] == ["b"]


def test_wakapi_interleaved_projects_do_not_fragment(tmp_path):
    """Three Claude Code sessions in three repos, heartbeats interleaved on
    every tick. Closing on each project switch made 2,420 of 2,898 live
    sessions one heartbeat long; per-project sessions make three."""
    db = tmp_path / "wakapi.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE heartbeats (user_id TEXT, time TIMESTAMP, "
                 "project TEXT, language TEXT, entity TEXT, branch TEXT)")
    base = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    conn.executemany("INSERT INTO heartbeats VALUES (?,?,?,?,?,?)", [
        ("yehor", int((base + timedelta(minutes=i)).timestamp() * 1000),
         ("AcmeBE", "chronicle", "homelab-gitops")[i % 3], "Python", "f.py", "main")
        for i in range(60)])
    conn.commit()
    conn.close()

    events = list(WakapiAdapter(str(db), user="yehor").fetch())
    assert len(events) == 3, [e.text for e in events]
    assert all(e.payload["minutes"] >= 57 for e in events)
    marks = [e.watermark_ts for e in events]
    assert marks == sorted(marks), "must stream in watermark order for resume"


def test_telegram_outgoing_messages_have_one_author(telegram_db):
    """telegram-sync names Yehor "me" on one path and by display name on the
    other; the adapter must not carry the split into the index."""
    conn = sqlite3.connect(telegram_db)
    conn.execute("UPDATE messages SET sender_name = 'Yehor Hrushevskyi' WHERE id = 1")
    conn.commit()
    conn.close()
    sent = [e for e in TelegramAdapter(telegram_db, exclude_bot_chats=False).fetch()
            if e.payload["direction"] == "sent"]
    assert sent and {e.actor for e in sent} == {"me"}


def test_two_long_messages_are_substantive():
    """Every 2-message segment used to be hidden from /recall, however long."""
    from chronicle.worker import _substantive
    ts = datetime(2026, 9, 1)
    row = lambda text: ("telegram", "1:1", ts, "Anna", text, None, None, None, None)
    assert _substantive([row("Завтра о девʼятій зустрічаємось біля вокзалу, квитки вже купила"),
                         row("Добре. Візьму каву на двох")]) is True
    assert _substantive([row("ок"), row("ага")]) is False
    assert _substantive([row("ок")]) is False
