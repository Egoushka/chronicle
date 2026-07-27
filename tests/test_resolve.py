"""Cross-script entity resolution.

The naive ISO-9 approach produces FOUR keys for one person
(ehor/iehor/yehor/egor). These tests exist because that failed in review and
the fix was not obvious — see docs/ADR-002-entity-resolution.md.
"""
import pytest

from chronicle.resolve import resolution_candidates, skeleton_key, translit_key

VARIANT_GROUPS = [
    ["Егор", "Єгор", "Yehor", "Egor", "Ehor", "Iehor"],
    ["Грушевский", "Грушевський", "Hrushevskyi", "Grushevskiy", "Hrushevsky"],
    ["Киев", "Київ", "Kyiv", "Kiev"],
    ["Харьков", "Харків", "Kharkiv", "Kharkov", "Harkiv"],
    ["Львов", "Львів", "Lviv", "Lvov"],
]

# Common first names with short skeletons — these must NOT be merged.
DISTINCT = ["Олена", "Тарас", "Оксана", "Андрій", "Ірина",
            "Дмитро", "Максим", "Сергій", "Зоряна"]


@pytest.mark.parametrize("variants", VARIANT_GROUPS)
def test_variants_collapse_to_one_skeleton(variants):
    assert len({skeleton_key(v) for v in variants}) == 1, \
        {v: skeleton_key(v) for v in variants}


def test_person_names_collapse_at_the_phonetic_tier():
    # Tier 1 alone handles people; only place names need the skeleton.
    assert len({translit_key(v) for v in VARIANT_GROUPS[0]}) == 1


def test_distinct_names_stay_distinct():
    keys = [skeleton_key(n) for n in DISTINCT]
    assert len(set(keys)) == len(DISTINCT), dict(zip(DISTINCT, keys))


def test_short_name_guard_blocks_degenerate_skeletons():
    # "Аня" -> "n". A 1-char skeleton collides with everything.
    phonetic = {translit_key("Аня"): [7]}
    got = resolution_candidates("Аня", phonetic, {"n": [99]})
    assert got == [(7, "phonetic")], "1-char skeleton must not be used"


def test_two_char_skeleton_is_allowed():
    # "Київ" -> "kv" is exactly the RU/UK cognate case the tier exists for,
    # so the guard threshold is 2, not 3.
    got = resolution_candidates("Київ", {}, {skeleton_key("Киев"): [10]})
    assert got == [(10, "skeleton")]


def test_blocking_by_chat_prevents_quadratic_resolution():
    # Unblocked resolution dies between 10^4 and 10^5 items; the corpus has
    # 6.8x10^5. Blocking is correctness, not optimization.
    phonetic = {translit_key("Егор"): [1, 2, 3]}
    got = resolution_candidates("Yehor", phonetic, {}, chat_id=100,
                                chat_scope={100: {1, 2}})
    assert [eid for eid, _ in got] == [1, 2]
