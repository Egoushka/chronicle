"""Bi-temporal facts with DETERMINISTIC conflict resolution.

arXiv:2606.01435 — "Don't Ask the LLM to Track Freshness". Two named
failure modes of LLM-as-conflict-resolver: prior-override (the LLM emits
the stale fact its training data prefers) and serial-comparison drift
(as pools grow it loses track of which version is latest). Measured:
LLM adjudication falls 75% -> 61% from 64K to 262K context.

The fix is max() in Python. Telegram timestamps make it free.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

@dataclass
class Fact:
    subject: str
    predicate: str
    object_text: str
    t_valid: datetime
    version: int                       # epoch millis of the source message
    confidence: float
    source_message_ids: list[int]
    t_invalid: datetime | None = None


def resolve_current(facts: Sequence[Fact], single_valued: bool = True) -> Fact | None:
    """max(version) in code. NEVER in a prompt."""
    live = [f for f in facts if f.t_invalid is None]
    if not live:
        return None
    if not single_valued:
        return max(live, key=lambda f: f.version)
    return max(live, key=lambda f: (f.version, f.confidence))


def invalidate_superseded(facts: list[Fact]) -> list[Fact]:
    """Close the validity interval of each superseded fact. Nothing is
    deleted — 'what did I believe in 2021?' stays answerable, which is most
    of the value of a personal archive."""
    ordered = sorted(facts, key=lambda f: f.version)
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev.t_invalid is None:
            prev.t_invalid = nxt.t_valid
    return ordered


