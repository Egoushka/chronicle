"""enrich.clean — the model's reply is untrusted input to a bi-temporal table."""
from datetime import date

from chronicle.enrich import MAX_FACTS, OWNER, PROMPT, clean, fact_line

PREDICATES = {"lives_in", "plans", "likes"}


def test_unknown_predicate_drops_the_fact_instead_of_inventing_one():
    """A free-text predicate never supersedes anything, so every address ever
    stated would read as current. The vocabulary is closed (migrations/005)."""
    out = clean({"facts": [
        {"subject": "Anna", "predicate": "lives in", "object": "Lviv"},
        {"subject": "Anna", "predicate": "moved_to", "object": "Kyiv"},
    ]}, PREDICATES)
    assert [(f["predicate"], f["object"]) for f in out["facts"]] == [("lives_in", "Lviv")]


def test_owner_aliases_collapse_to_one_subject():
    out = clean({"facts": [{"subject": s, "predicate": "likes", "object": "x"}
                           for s in ("me", "Егор", "Yehor", "Anna")]}, PREDICATES)
    assert [f["subject"] for f in out["facts"]] == [OWNER, OWNER, OWNER, "Anna"]


def test_numbers_are_clamped_and_garbage_defaults():
    out = clean({"importance": 7, "sentiment": "very", "facts": [
        {"subject": "A", "predicate": "likes", "object": "b", "confidence": -3}]},
        PREDICATES)
    assert out["importance"] == 1.0
    assert out["sentiment"] == 0.0
    assert out["facts"][0]["confidence"] == 0.0


def test_lists_are_bounded_deduped_and_typed():
    out = clean({"topics": ["Work", "work", " ", *[f"t{i}" for i in range(20)]],
                 "facts": [{"subject": "A", "predicate": "likes", "object": str(i)}
                           for i in range(50)] + ["not a dict"]}, PREDICATES)
    assert out["topics"][:2] == ["work", "t0"] and len(out["topics"]) == 6
    assert len(out["facts"]) == MAX_FACTS


def test_commitments_need_a_direction_and_parse_due_dates():
    out = clean({"commitments": [
        {"text": "send the lease", "direction": "i_owe", "due": "2026-10-01"},
        {"text": "call back", "direction": "owed_to_me", "due": "next week"},
        {"text": "vague", "direction": "someone"},
        {"text": "", "direction": "i_owe"},
    ]}, PREDICATES)
    assert [(c["text"], c["due"]) for c in out["commitments"]] == [
        ("send the lease", date(2026, 10, 1)), ("call back", None)]


def test_empty_reply_is_a_valid_answer():
    out = clean({}, PREDICATES)
    assert out["facts"] == [] and out["commitments"] == [] and out["summary"] is None


def test_fact_line_reads_as_text():
    assert fact_line({"subject": "Anna", "predicate": "lives_in", "object": "Lviv"}) \
        == "Anna lives in Lviv"


def test_prompt_formats():
    # Literal JSON braces in the template must be escaped for str.format.
    PROMPT.format(predicates="a, b", chat="c", date="d", text="t")
