"""Segmentation tests, calibrated against the real corpus distribution.

Measured 2026-07-25 from telegram-sync: 681,331 messages, 65.0% under 20
chars, 94.0% under 60, 1.5% over 200, 7.9% with reply_to.
"""
import random
from datetime import datetime, timedelta

import pytest

from chronicle.segment import Event, fit_gap_threshold, segment_chat

FILLER = ["ок", "ага", "+", "да", "😂", "ну", "хз", "👍", "угу", "+1"]
MID = ["давай завтра встретимся", "нужно проверить логи",
       "поїхали в кафе о сьомій", "let me check the deploy"]
LONG = ["Слушай, я подумал про эту архитектуру. Если вынести это в отдельный "
        "сервис, получим независимый деплой, но добавим сетевую задержку и "
        "ещё одну точку отказа. Не уверен что оно того стоит."]


def make_corpus(seed=42, bursts=300):
    """Bursty stream matching the measured length distribution."""
    random.seed(seed)
    msgs, t, mid = [], datetime(2021, 1, 1, 9, 0), 1
    for _ in range(bursts):
        for _ in range(random.randint(2, 25)):
            r = random.random()
            text = (random.choice(FILLER) if r < 0.65
                    else random.choice(MID) if r < 0.94
                    else random.choice(LONG))
            msgs.append(Event(message_id=mid, chat_id=1,
                              sender_id=random.choice([1, 2]),
                              sender_name="me", ts=t, text=text))
            mid += 1
            t += timedelta(seconds=random.randint(5, 180))     # within burst
        t += timedelta(seconds=random.choice([3600, 7200, 28800, 86400]))
    return msgs


def test_length_distribution_matches_real_corpus():
    msgs = make_corpus()
    short = sum(1 for m in msgs if len(m.text) < 20) / len(msgs)
    assert 0.60 < short < 0.75, f"fixture drifted from the real 65%: {short:.1%}"


def test_gap_fit_finds_valley_above_median_below_between_burst():
    msgs = make_corpus()
    fit = fit_gap_threshold([m.ts for m in msgs], chat_id=1)
    assert fit.n_samples > 1000
    # The valley must sit ABOVE within-burst gaps and BELOW the shortest
    # between-burst gap (1 h), or it splits/merges the wrong things.
    assert fit.p50 < fit.threshold_seconds < 3600


def test_segmentation_compresses_and_leaves_no_singletons():
    msgs = make_corpus()
    fit = fit_gap_threshold([m.ts for m in msgs], chat_id=1)
    eps = segment_chat(msgs, gap_seconds=fit.threshold_seconds)

    ratio = len(msgs) / len(eps)
    # ~14x measured. Extrapolates to ~48,700 episodes for the real 681,331.
    assert 8 < ratio < 25, f"compression {ratio:.1f}x is outside the expected band"

    sizes = [len(e.messages) for e in eps]
    # A 1-event episode is per-message indexing reintroduced through the back
    # door — the exact mistake this whole design exists to avoid.
    assert min(sizes) > 1, "singleton episodes must be merged"
    assert max(sizes) <= 60, "message cap violated"


def test_token_cap_is_soft_but_bounded():
    msgs = make_corpus()
    eps = segment_chat(msgs, gap_seconds=1800, max_tokens=250)
    # The cap is checked BEFORE appending, so an episode can exceed it by one
    # event. Bounded overshoot is fine; unbounded is not.
    assert max(e.approx_tokens for e in eps) < 400


def test_reply_edge_suppresses_a_split():
    base = datetime(2021, 1, 1, 9, 0)
    msgs = [
        Event(1, 1, 1, "me", base, "вопрос про квартиру"),
        # 45 min later — past a 30 min gap, so this WOULD split...
        Event(2, 1, 2, "Аня", base + timedelta(minutes=45),
              "ответ", reply_to_id=1),
        Event(3, 1, 1, "me", base + timedelta(minutes=46), "понял"),
    ]
    eps = segment_chat(msgs, gap_seconds=1800, merge_below=1)
    # ...but the explicit reply edge says it is the same conversation.
    assert len(eps) == 1, "reply edge should suppress the time-gap split"


def test_substantive_filter_rejects_pure_filler():
    base = datetime(2021, 1, 1, 9, 0)
    filler = [Event(i, 1, 1, "me", base + timedelta(seconds=i * 10), "ок")
              for i in range(10)]
    eps = segment_chat(filler, gap_seconds=1800)
    assert not any(e.is_substantive() for e in eps), \
        "a burst of 'ок' is not a memory"
