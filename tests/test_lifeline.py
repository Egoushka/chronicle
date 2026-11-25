"""Lifeline: the oura and mail adapters on invented data, and the citation contract."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from chronicle import lifeline
from chronicle.adapters.mail import MailAdapter
from chronicle.adapters.oura import OuraAdapter
from chronicle.sources import BY_SOURCE

UTC = timezone.utc


# --------------------------------------------------------------------------
#  oura
# --------------------------------------------------------------------------

def _oura(daily, hr):
    ad = OuraAdapter.__new__(OuraAdapter)          # skip the DSN: no database in the unit tier
    calls = iter([daily, hr])
    ad._stream = lambda sql, params: iter(next(calls))
    return ad


def test_oura_one_compact_event_per_day_per_kind():
    day = date(2026, 3, 2)
    ad = _oura([(day, 81, 79, 88, 54, 52, 0.1, 9412, 540, 7 * 3600 + 12 * 60)],
               [(day, 71.4, 46, 142)])
    evs = list(ad.fetch())
    assert [e.kind for e in evs] == ["sleep", "readiness", "activity", "heart"]
    assert evs[0].text == "Sleep 7h12, score 81, HRV 54"
    assert evs[1].text == "Readiness 79, resting HR 52, temp +0.1"
    assert evs[2].text == "Activity 88, 9,412 steps, 540 active kcal"
    assert evs[3].text == "Heart rate avg 71, min 46, max 142 bpm"
    assert [e.ts for e in evs] == sorted(e.ts for e in evs), "resume depends on ascending ts"
    assert len({e.source_id for e in evs}) == 4 and all(e.thread_key for e in evs)


def test_oura_skips_missing_kinds_and_honours_since():
    d1, d2 = date(2026, 3, 2), date(2026, 3, 3)
    ad = _oura([(d1, 80, None, None, None, None, None, None, None, None),
                (d2, 70, 60, None, 40, None, None, None, None, None)], [])
    assert [e.source_id for e in ad.fetch()] == [
        "sleep:2026-03-02", "sleep:2026-03-03", "readiness:2026-03-03"]
    ad = _oura([(d1, 80, None, None, None, None, None, None, None, None),
                (d2, 70, 60, None, 40, None, None, None, None, None)], [])
    got = [e.source_id for e in ad.fetch(since=datetime(2026, 3, 3, 0, 0, tzinfo=UTC))]
    assert got == ["sleep:2026-03-03", "readiness:2026-03-03"]


def test_oura_connection_is_read_only():
    pytest.importorskip("psycopg")   # the unit tier installs no database driver
    ad = OuraAdapter("postgresql://u:p@db:5432/oura")
    assert "default_transaction_read_only" in ad.dsn


# --------------------------------------------------------------------------
#  mail
# --------------------------------------------------------------------------

def _msg(root, name, folder, headers, body, account="acct-1"):
    d = root / account / folder / "cur"
    d.mkdir(parents=True, exist_ok=True)
    lines = "".join(f"{k}: {v}\n" for k, v in headers.items())
    (d / name).write_text(lines + "\n" + body, encoding="utf-8")


@pytest.fixture
def maildir(tmp_path):
    base = {"From": "Ann Example <ann@example.org>", "To": "me@example.org",
            "Subject": "Lunch on Friday?", "Message-ID": "<1@example.org>",
            "Date": "Mon, 02 Mar 2026 10:00:00 +0000"}
    _msg(tmp_path, "1772445600.1_1.host,U=1:2,S", "INBOX", base,
         "Are you free at noon?\nThe usual place.\n\nOn Sun, Bob wrote:\n> old quoted text\n")
    _msg(tmp_path, "1772445700.2_1.host,U=2:2,S", "INBOX",
         {**base, "Message-ID": "<2@example.org>", "List-Unsubscribe": "<mailto:x@example.org>",
          "Subject": "Weekly deals"}, "Buy things")
    _msg(tmp_path, "1772445800.3_1.host,U=3:2,S", "INBOX",
         {**base, "Message-ID": "<3@example.org>", "Auto-Submitted": "auto-generated"}, "robot")
    _msg(tmp_path, "1772445900.4_1.host,U=4:2,S", "Bulk",
         {**base, "Message-ID": "<4@example.org>", "Subject": "Other folder"}, "not read")
    return tmp_path


def test_mail_emits_human_mail_only_with_a_short_snippet(maildir):
    evs = list(MailAdapter(maildir).fetch(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(evs) == 1
    ev = evs[0]
    assert ev.text == "From Ann Example: Lunch on Friday? - Are you free at noon? The usual place."
    assert "quoted" not in ev.text
    assert ev.ts == datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
    assert ev.actor == "ann@example.org" and ev.thread_key == "mail:acct-1:INBOX"
    assert ev.watermark_ts == datetime.fromtimestamp(1772445600, UTC)


def test_mail_resumes_by_arrival_and_excludes_senders(maildir):
    ad = MailAdapter(maildir)
    after = datetime.fromtimestamp(1772445600, UTC)
    assert list(ad.fetch(since=after)) == [], "the only passing message arrived at the watermark"
    assert list(MailAdapter(maildir, exclude_senders=("example.org",)).fetch(
        since=datetime(2026, 1, 1, tzinfo=UTC))) == []


def test_mail_policy_and_no_sample_echo():
    assert BY_SOURCE["mail"] and BY_SOURCE["oura"]
    assert MailAdapter.show_sample is False


# --------------------------------------------------------------------------
#  lifeline_ask
# --------------------------------------------------------------------------

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
ROWS = {
    "oura": [(1, datetime(2026, 3, 3, 7, 0, tzinfo=UTC), "Sleep 5h10, score 61, HRV 40"),
             (2, datetime(2026, 3, 4, 7, 0, tzinfo=UTC), "Sleep 8h00, score 90, HRV 70")],
    "wakapi": [(3, datetime(2026, 3, 4, 9, 0, tzinfo=UTC), "coded on app for 300 min (Python)")],
    "telegram": [(4, datetime(2026, 3, 3, 15, 0, tzinfo=UTC), "we talked about the move")],
    "mail": [(5, datetime(2026, 3, 3, 16, 0, tzinfo=UTC), "From Ann: Lunch - noon?")],
}


def fake_query(sql, params):
    return ROWS[params[0]]


def fake_llm(answer_ids, calls=None, days=("2026-03-03",)):
    def llm(system, user):
        if calls is not None:
            calls.append(system[:20])
        if "calendar days" in system:
            return json.dumps({"days": list(days)})
        return json.dumps({"answer": "Slept least on 3 March.", "citations": answer_ids})
    return llm


def ask(llm, q="What did I discuss on Telegram and in mail on the day my sleep score was lowest?",
        **kw):
    return lifeline.ask(q, query=fake_query, llm=llm, now=NOW, **kw)


def test_answer_carries_citations_from_retrieved_events_only():
    out = ask(fake_llm(["E1", "E4", "E5"]))
    assert {c["source"] for c in out["citations"]} == {"oura", "telegram", "mail"}
    assert [c["event_id"] for c in out["citations"]] == [1, 4, 5]
    assert out["citations"][0]["snippet"].startswith("Sleep 5h10")


def test_invented_event_id_is_rejected():
    with pytest.raises(lifeline.LifelineError, match="not retrieved"):
        ask(fake_llm(["E1", "E999"]))


def test_no_citation_is_an_error_not_a_guess():
    with pytest.raises(lifeline.LifelineError, match="no citations"):
        ask(fake_llm([]))


def test_planner_runs_once_and_only_for_structured_plus_narrative():
    calls: list[str] = []
    ask(fake_llm(["E1"], calls))
    assert len(calls) == 2, "plan + answer: the loop is capped at two"
    calls.clear()
    lifeline.ask("How did I sleep this week?", query=fake_query, llm=fake_llm(["E1"], calls),
                 now=NOW)
    assert len(calls) == 1


def test_a_failing_planner_falls_back_to_the_whole_window():
    def llm(system, user):
        if "calendar days" in system:
            raise RuntimeError("down")
        return json.dumps({"answer": "ok", "citations": ["E1"]})
    assert ask(llm)["citations"][0]["event_id"] == 1


def test_window_and_source_routing():
    lo, hi = lifeline.parse_window("my sleep last week", None, None, NOW)
    assert (lo.date(), hi.date()) == (date(2026, 3, 2), date(2026, 3, 9))
    lo, hi = lifeline.parse_window("this month", None, None, NOW)
    assert lo.date() == date(2026, 3, 1)
    lo, hi = lifeline.parse_window("anything", None, None, NOW)
    assert hi - lo == timedelta(days=30)
    explicit = datetime(2026, 1, 1, tzinfo=UTC)
    assert lifeline.parse_window("last week", explicit, None, NOW)[0] == explicit
    assert lifeline.pick_sources("how did I sleep?") == ["oura"]
    assert lifeline.pick_sources("coding and mail") == ["wakapi", "mail"]
    assert lifeline.pick_sources("something vague") == list(lifeline.ALL_SOURCES)


def test_empty_evidence_and_bad_json_are_errors():
    with pytest.raises(lifeline.LifelineError):
        lifeline.ask("how did I sleep?", query=lambda s, p: [], llm=fake_llm(["E1"]), now=NOW)
    with pytest.raises(lifeline.LifelineError, match="JSON"):
        ask(lambda s, u: "not json")
