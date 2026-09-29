"""Scoring is what decides whether the project earned its keep. Test it.

The bug these regression-test: `score` flattened every answer into one list
and took `got[0]`, so Chronicle's p@1 asked "is the first event of the top
segment the gold one" — which is false for almost every 13-event segment even
when the segment ranked first and contained the answer.
"""

from chronicle.evaluate import Question, score


def _fixed(groups):
    return lambda qn: groups


def test_p_at_1_is_segment_level_not_event_level():
    """Gold mid-segment, segment ranked first: that is a hit."""
    qn = Question("q", "lookup", evidence=["telegram:1:300"], keywords=["x"])
    top_segment = ["telegram:1:100", "telegram:1:200", "telegram:1:300"]

    res = score([qn], _fixed([top_segment, ["telegram:9:999"]]))

    assert res["lookup"].p_at_1 == 1.0, "gold is in the top-ranked segment"
    assert res["lookup"].recall == 1.0
    # The old flattened rule took got[0] == "telegram:1:100" and scored 0.
    assert top_segment[0] not in set(qn.evidence)


def test_p_at_1_misses_when_gold_is_not_in_the_top_group():
    qn = Question("q", "lookup", evidence=["telegram:1:300"], keywords=["x"])

    res = score([qn], _fixed([["telegram:2:1"], ["telegram:1:300"]]))

    assert res["lookup"].p_at_1 == 0.0
    assert res["lookup"].recall == 1.0, "still retrieved, just not ranked first"


def test_recall_is_partial_when_only_some_gold_is_found():
    qn = Question("q", "lookup", evidence=["a", "b", "c", "d"], keywords=["x"])

    res = score([qn], _fixed([["a", "b"]]))

    assert res["lookup"].recall == 0.5


def test_unlabeled_questions_are_skipped_not_scored_zero():
    qs = [Question("labeled", "lookup", evidence=["a"]),
          Question("unlabeled", "lookup", evidence=[])]

    res = score(qs, _fixed([["a"]]))

    assert res["lookup"].n == 1, "an unlabeled question measures nothing"


def test_budget_cuts_groups_at_the_event_count():
    """chronicle answers ~13 events a segment; the budget is in events, so
    both sides read the same amount."""
    from chronicle.evaluate import _within_budget

    got = _within_budget([["a", "b", "c"], ["d", "e"], ["f"]], 4)

    assert got == [["a", "b", "c"], ["d"]]
    assert _within_budget([["a"]], 0) == []
