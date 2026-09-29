from datetime import date, datetime, timedelta, timezone

from chronicle.freshness import classify, usual_gap

NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


def days(*offsets):
    return [date(2026, 9, 29) - timedelta(days=o) for o in offsets]


def test_daily_source_goes_silent_after_the_minimum():
    active = days(*range(4, 60))                     # every day up to 4 days ago
    f = classify("wakapi", NOW - timedelta(days=4), active, NOW)
    assert f.usual_gap_days == 1 and f.status == "silent"


def test_bursty_source_is_judged_by_its_own_gap():
    active = days(*range(30, 300, 10))               # every 10 days
    quiet = classify("chat", NOW - timedelta(days=15), active, NOW)
    gone = classify("chat", NOW - timedelta(days=30), active, NOW)
    assert quiet.status == "ok" and gone.status == "silent"


def test_old_source_is_dormant_not_alarming():
    assert classify("owntracks", NOW - timedelta(days=400), days(*range(500, 600)),
                    NOW).status == "dormant"


def test_too_little_history_is_unknown():
    assert usual_gap(days(1, 2, 3)) is None
    assert classify("new", NOW - timedelta(days=30), days(31, 32), NOW).status == "unknown"
