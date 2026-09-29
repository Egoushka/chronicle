"""Which sources went quiet, judged against each source's own rhythm.

Doctor's "nothing in 90 days" only notices a source that is already long
dead, and only when someone runs it: location and photos died unnoticed. A
fixed threshold is wrong both ways (a chat that is silent for a week is
normal, wakapi silent for a week is not), so the yardstick is the source's
own history: the longest gap between two active days in the past year.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

MIN_SILENCE_DAYS = 3      # no source is "silent" after less than this
FACTOR = 2.0              # silent once the quiet stretch is 2x its usual worst gap
DORMANT_DAYS = 365        # older than this it is retired, not an alarm
MIN_ACTIVE_DAYS = 10      # too little history to know a rhythm


@dataclass
class Freshness:
    source: str
    last_event: datetime
    days_silent: float
    usual_gap_days: int | None
    status: str           # ok | silent | dormant | unknown


def usual_gap(active_days: list[date]) -> int | None:
    """Longest gap, in days, between consecutive active days; None if too few."""
    days = sorted(set(active_days))
    if len(days) < MIN_ACTIVE_DAYS:
        return None
    return max((b - a).days for a, b in zip(days, days[1:]))


def classify(source: str, last_event: datetime, active_days: list[date],
             now: datetime | None = None) -> Freshness:
    now = now or datetime.now(timezone.utc)
    silent_for = (now - last_event).total_seconds() / 86400
    gap = usual_gap(active_days)
    if silent_for > DORMANT_DAYS:
        status = "dormant"
    elif gap is None:
        status = "unknown"
    elif silent_for > max(MIN_SILENCE_DAYS, FACTOR * gap):
        status = "silent"
    else:
        status = "ok"
    return Freshness(source, last_event, round(silent_for, 1), gap, status)
