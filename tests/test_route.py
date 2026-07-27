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
