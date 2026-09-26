"""Intent routing. Deterministic by design — a learned router scores 54.52%."""
import pytest

from chronicle.route import route

CASES = [
    ("Сколько раз я писал Ане в 2022?",           "aggregate"),
    ("How many commits did I make last year?",    "aggregate"),
    ("When did I first mention Kubernetes?",      "first_mention"),
    ("Когда я впервые упомянул криптовалюту?",    "first_mention"),
    ("Коли я вперше почав про це говорити?",      "first_mention"),
    ("Как менялось моё мнение об инвестициях?",   "evolution"),
    ("Як змінювалось моє ставлення до роботи?",   "evolution"),
    ("How did my view on remote work evolve?",    "evolution"),
    ("What happened in summer 2024?",             "timeline"),
    ("Что происходило прошлым летом?",            "timeline"),
    ("Что мы обсуждали про квартиру?",            "lookup"),
    ("Что нового недавно?",                       "lookup"),
]


@pytest.mark.parametrize("query,expected", CASES)
def test_routing(query, expected):
    assert route(query).kind == expected


def test_recency_decay_never_fires_with_a_date_anchor():
    # Solr-style recip() imposes an 8.6x penalty on 2018 content across a
    # 7.6-year span. Archival queries must never get a recency prior.
    for q in ["Что было в 2019?", "What happened in 2020?",
              "Как дела были прошлым летом?"]:
        assert not route(q).apply_recency_decay, q


def test_recency_decay_fires_only_on_present_tense():
    assert route("Что нового недавно?").apply_recency_decay
    assert not route("Что мы обсуждали про квартиру?").apply_recency_decay


def test_every_route_is_logged_and_overridable():
    intent = route("Сколько раз я писал Ане?")
    assert intent.kind and hasattr(intent, "matched_pattern")


# --------------------------------------------------------------------------
#  window — the date range a question names
# --------------------------------------------------------------------------

def _d(y, m):
    from datetime import datetime, timezone
    return datetime(y, m, 1, tzinfo=timezone.utc)


def test_window_year_has_a_margin_both_sides():
    from chronicle.route import window
    assert window("Як я пішов з роботи восени 2024?") == (_d(2024, 8), _d(2025, 1))
    assert window("what happened in 2021 with the car") == (_d(2020, 11), _d(2022, 2))


def test_window_month_and_season_in_three_languages():
    from chronicle.route import window
    assert window("Що сталося з раковиною в січні 2026?") == (_d(2025, 12), _d(2026, 3))
    assert window("что было в ноябре 2024") == (_d(2024, 10), _d(2025, 1))
    assert window("Чому влітку 2025 поїхав з Києва?") == (_d(2025, 5), _d(2025, 10))
    assert window("rejected after the tech interview in November 2024") \
        == (_d(2024, 10), _d(2025, 1))


def test_window_winter_spans_the_new_year():
    from chronicle.route import window
    assert window("взимку 2023 я хворів") == (_d(2022, 11), _d(2023, 4))


def test_window_refuses_to_guess():
    """No year, or two different years: no window. A window that excludes the
    answer is worse than none."""
    from chronicle.route import window
    assert window("Коли я кинув курити?") is None
    assert window("як змінилась зарплата між 2019 і 2022") is None
    assert window("мій номер 12020 і код 2024") == (_d(2023, 11), _d(2025, 2))



# --------------------------------------------------------------------------
#  spelling_variants — the archive's other spellings of a question's terms
# --------------------------------------------------------------------------

def _archive(words):
    """A lookup over a fake lexeme_df: {word: ndoc}."""
    from chronicle.resolve import translit_key
    rows = [(translit_key(w), w, n) for w, n in words.items()]
    return lambda keys: [r for r in rows if r[0] in keys]


def test_variants_cross_script_and_ru_uk():
    from chronicle.rank import spelling_variants
    lookup = _archive({"epam": 12, "епам": 21, "одес": 61, "одесс": 337,
                       "одесі": 44, "квартир": 900})
    got = spelling_variants(["epam", "одес"], lookup)
    assert "епам" in got and "одесс" in got
    assert "epam" not in got and "одес" not in got, "the question's own terms are not variants"
    assert "квартир" not in got


def test_variants_skip_short_keys_and_cap_per_key():
    from chronicle.rank import spelling_variants
    lookup = _archive({"як": 5000, "ак": 10, "aa": 1,
                       "одесс": 337, "одесі": 44, "одеса": 30, "одесу": 20})
    assert spelling_variants(["як"], lookup) == [], "2-letter keys collide with everything"
    got = spelling_variants(["одес"], lookup, per_key=2)
    assert got == ["одесс", "одесі"], "most frequent spellings first, capped"
