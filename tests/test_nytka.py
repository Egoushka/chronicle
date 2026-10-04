"""Nytka adapter tests: the parts that do not need a PostgreSQL.

`scripts/nytka-itest.py` runs the same adapter against a real one. These pin
the rules that are about people rather than SQL: whose words are muted, whose
conversation is gone, and that nothing prints an utterance.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from chronicle.adapters import ADAPTERS, Density
from chronicle.adapters.nytka import (SYNC_LAG, NytkaAdapter, is_muted,
                                      parse_mute_windows)
from chronicle.sources import BY_SOURCE, Tier

KYIV = ZoneInfo("Europe/Kyiv")
CONV = "01a0ec61-a5ae-7e85-b296-3e14927bc796"
CONV2 = "01a0ec61-a5ae-7e85-b296-3e14927bc797"
UTC = timezone.utc


def at(y, mo, d, h, mi=0, tz=KYIV):
    return datetime(y, mo, d, h, mi, tzinfo=tz)


# --------------------------------------------------------------------------
#  mute windows — the server's own reading of its setting
# --------------------------------------------------------------------------

WEEKDAY_STANDUP = parse_mute_windows('[{"days":[1,2,3,4,5],"start":"09:30","end":"10:00"}]')
NIGHT = parse_mute_windows('[{"days":[5],"start":"22:00","end":"06:00"}]')


def test_a_window_mutes_its_stretch_on_its_days_in_the_owners_zone():
    # 2026-10-05 is a Monday. 09:45 Kyiv is 06:45 UTC (EEST, +3).
    assert is_muted(at(2026, 10, 5, 9, 45), WEEKDAY_STANDUP, KYIV)
    assert is_muted(datetime(2026, 10, 5, 6, 45, tzinfo=UTC), WEEKDAY_STANDUP, KYIV)
    # the same clock reading read as UTC would be a different stretch
    assert not is_muted(datetime(2026, 10, 5, 9, 45, tzinfo=UTC), WEEKDAY_STANDUP, KYIV)


def test_a_window_is_half_open_and_day_bound():
    assert is_muted(at(2026, 10, 5, 9, 30), WEEKDAY_STANDUP, KYIV)
    assert not is_muted(at(2026, 10, 5, 10, 0), WEEKDAY_STANDUP, KYIV)
    assert not is_muted(at(2026, 10, 3, 9, 45), WEEKDAY_STANDUP, KYIV), "Saturday"


def test_an_end_at_or_before_the_start_crosses_midnight_on_the_start_day():
    # days name the day a window STARTS on: Friday 22:00 -> Saturday 06:00.
    assert is_muted(at(2026, 10, 2, 23, 0), NIGHT, KYIV)        # Fri 23:00
    assert is_muted(at(2026, 10, 3, 5, 59), NIGHT, KYIV)        # Sat 05:59
    assert not is_muted(at(2026, 10, 3, 6, 0), NIGHT, KYIV)
    assert not is_muted(at(2026, 10, 4, 5, 0), NIGHT, KYIV), "Sunday morning follows Saturday"
    assert not is_muted(at(2026, 10, 2, 5, 0), NIGHT, KYIV), "Friday morning follows Thursday"


def test_no_windows_mutes_nothing():
    assert not is_muted(at(2026, 10, 5, 9, 45), parse_mute_windows(None), KYIV)
    assert not is_muted(at(2026, 10, 5, 9, 45), parse_mute_windows("  "), KYIV)
    assert not is_muted(at(2026, 10, 5, 9, 45), parse_mute_windows("[]"), KYIV)


@pytest.mark.parametrize("bad", ["not json", '{"days":[1]}', '[{"days":[8],"start":"09:00","end":"10:00"}]',
                                 '[{"days":[],"start":"09:00","end":"10:00"}]',
                                 '[{"days":[1],"start":"9am","end":"10:00"}]', '[{"days":[1]}]'])
def test_unreadable_windows_stop_the_source_instead_of_letting_speech_in(bad):
    with pytest.raises(ValueError) as exc:
        parse_mute_windows(bad)
    assert bad not in str(exc.value), "the error must not echo the setting"


# --------------------------------------------------------------------------
#  fetch — fed by canned rows, so no PostgreSQL
# --------------------------------------------------------------------------

def make_adapter(rows, settings=(), **kw):
    """A NytkaAdapter whose _stream answers from `rows` and `settings`."""
    seen = []

    class Fake(NytkaAdapter):
        def _stream(self, sql, params):
            seen.append((sql, params))
            if "FROM settings" in sql:
                return iter(settings)
            return iter(rows)

    ad = Fake("postgresql://unused", **kw)
    ad.seen = seen
    return ad


def row(seg_id, started, text="hello there", is_user=False, speaker="SPEAKER_01",
        speaker_id="v1", person=None, conv=CONV, origin="nytka",
        updated=datetime(2026, 10, 2, 12, 0, tzinfo=UTC)):
    return (seg_id, conv, origin, updated, started, text, is_user, speaker, speaker_id, person)


def test_events_are_keyed_by_segment_id_and_threaded_by_conversation():
    ad = make_adapter([row(7, at(2026, 10, 5, 12, 0), "  hi  ")])
    (ev,) = list(ad.fetch())
    assert ev.dedupe_key() == "nytka:7"
    assert ev.thread_key == f"nytka:{CONV}"
    assert ev.text == "hi"
    assert ev.kind == "utterance"
    assert ev.watermark_ts == datetime(2026, 10, 2, 12, 0, tzinfo=UTC), \
        "resume must follow the conversation's updated_at, not the utterance time"
    assert ev.payload["origin"] == "nytka" and ev.payload["chat_title"] == "Nytka"


def test_the_wearer_is_me_a_named_voice_keeps_its_name_an_unnamed_one_its_label():
    ad = make_adapter([
        row(1, at(2026, 10, 5, 12, 0), is_user=True, speaker="SPEAKER_00"),
        row(2, at(2026, 10, 5, 12, 1), person="Anna", speaker="SPEAKER_01"),
        row(3, at(2026, 10, 5, 12, 2), speaker="SPEAKER_02"),
        row(4, at(2026, 10, 5, 12, 3), speaker=None, speaker_id=None, is_user=None),
    ])
    assert [e.actor for e in ad.fetch()] == ["me", "Anna", "SPEAKER_02", None]


def test_utterances_inside_a_mute_window_are_not_read():
    settings = [("mute.windows", '[{"days":[1],"start":"09:30","end":"10:00"}]'),
                ("user.timeZone", "Europe/Kyiv")]
    ad = make_adapter([row(1, at(2026, 10, 5, 9, 45)),      # Monday, inside
                       row(2, at(2026, 10, 5, 10, 5)),      # Monday, after
                       row(3, at(2026, 10, 6, 9, 45))],     # Tuesday
                      settings=settings)
    assert [e.source_id for e in ad.fetch()] == ["2", "3"]


def test_unreadable_mute_windows_fail_the_fetch_not_the_filter():
    ad = make_adapter([row(1, at(2026, 10, 5, 12, 0))],
                      settings=[("mute.windows", "{broken")])
    with pytest.raises(ValueError):
        list(ad.fetch())


def test_resume_rereads_an_overlap_and_naive_bounds_are_utc():
    ad = make_adapter([])
    since = datetime(2026, 10, 2, 12, 0)            # naive, as a caller might pass
    list(ad.fetch(since=since))
    _, params = ad.seen[-1]
    assert params["since"] == since.replace(tzinfo=UTC) - SYNC_LAG
    assert params["since"].tzinfo is not None
    assert params["until"] is None and params["excluded"] == []


def test_only_closed_conversations_and_non_empty_text_are_asked_for():
    ad = make_adapter([])
    list(ad.fetch())
    sql = ad.seen[-1][0]
    assert "c.status = 'closed'" in sql and "btrim(s.text) <> ''" in sql
    assert "ORDER BY c.updated_at" in sql, "the worker resumes on this order"


def test_an_excluded_conversation_is_filtered_in_sql_and_named_for_purge():
    ad = make_adapter([], exclude_conversation_ids=(CONV.upper(),))
    list(ad.fetch())
    assert ad.seen[-1][1]["excluded"] == [CONV], "bound as normalised uuids"
    assert ad.excluded_thread_keys() == {f"nytka:{CONV}"}


def test_a_mistyped_exclusion_fails_at_construction():
    with pytest.raises(ValueError):
        NytkaAdapter("postgresql://unused", exclude_conversation_ids=("not-a-uuid",))


# --------------------------------------------------------------------------
#  erasure — deleted upstream means conversation AND segments gone
# --------------------------------------------------------------------------

def erasing(indexed, live_conversations, live_segments, **kw):
    class Fake(NytkaAdapter):
        def _stream(self, sql, params):
            if "FROM conversations" in sql:
                return iter([(c,) for c in live_conversations if c in params["ids"]])
            if "FROM segments" in sql:
                return iter([(str(s),) for s in live_segments if s in params["ids"]])
            raise AssertionError(sql)
    return Fake("postgresql://unused", indexed=lambda: indexed, **kw)


def test_a_conversation_deleted_upstream_is_excluded():
    ad = erasing({f"nytka:{CONV}": ["1", "2"], f"nytka:{CONV2}": ["3"]},
                 live_conversations=[CONV2], live_segments=[3])
    assert ad.excluded_thread_keys() == {f"nytka:{CONV}"}


def test_a_merged_away_conversation_is_kept_while_its_segments_live_on():
    """Late audio bridges two conversations: the survivor takes the other's
    segments and the other row is deleted. Chronicle still holds those events
    under the old thread key, and purging it would erase speech that exists."""
    ad = erasing({f"nytka:{CONV}": ["1", "2"]}, live_conversations=[], live_segments=[1, 2])
    assert ad.excluded_thread_keys() == set()


def test_a_deleted_survivor_takes_the_merged_threads_with_it():
    ad = erasing({f"nytka:{CONV}": ["1"], f"nytka:{CONV2}": ["2"]},
                 live_conversations=[], live_segments=[])
    assert ad.excluded_thread_keys() == {f"nytka:{CONV}", f"nytka:{CONV2}"}


def test_nothing_is_excluded_when_chronicle_holds_nothing():
    assert erasing({}, [], []).excluded_thread_keys() == set()


def test_without_a_chronicle_lookup_only_the_explicit_list_applies():
    ad = make_adapter([], exclude_conversation_ids=(CONV,))
    assert ad.excluded_thread_keys() == {f"nytka:{CONV}"}


# --------------------------------------------------------------------------
#  policy — where Nytka sits, and that nothing prints or promotes it
# --------------------------------------------------------------------------

def test_nytka_is_the_last_tier_narrative_and_has_no_hindsight_bank():
    p = BY_SOURCE["nytka"]
    assert p.tier is Tier.AMBIENT and p.density is Density.NARRATIVE
    assert p.bank is None, "a bank would route its facts into Hindsight"
    assert ADAPTERS["nytka"].density is Density.NARRATIVE


def test_doctor_never_prints_an_utterance(monkeypatch):
    from chronicle.doctor import check_source
    ad = make_adapter([row(1, at(2026, 10, 5, 12, 0), "a secret sentence")])
    monkeypatch.setattr("chronicle.doctor.build", lambda source: ad)
    check = check_source("nytka")
    assert check.sample is not None
    assert "secret" not in str(check.sample)
    assert ADAPTERS["nytka"].show_sample is False
