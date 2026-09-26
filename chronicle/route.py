"""Deterministic intent routing. NOT a learned router.

Adaptive-RAG's learned query-complexity classifier scores 54.52% on a
3-way task. In the "Dissecting Agentic RAG" ablation an adaptive router
fired on named entities 79.2% of the time, collapsed onto BM25, and LOST
to fixed hybrid retrieval by 1.8 EM (p < 0.001).

Query classes here ARE lexically distinguishable, so parse them.
Every decision is logged and user-overridable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

INTENT_PATTERNS: dict[str, list[str]] = {
    "aggregate": [
        r"\bсколько\b", r"\bскільки\b", r"\bhow many\b", r"\bhow much\b",
        r"\bcount\b", r"\bчаще всего\b", r"\bнайчастіше\b", r"\bmost often\b",
        r"\bstatistics\b", r"\bстатистик", r"\bсредн", r"\baverage\b",
    ],
    "first_mention": [
        r"\bвпервые\b", r"\bвперше\b", r"\bпервый раз\b", r"\bперший раз\b",
        r"\bfirst\b.{0,20}\b(mention|time|said|heard)\b", r"\bearliest\b",
        r"\bwhen did i first\b", r"\bкогда я впервые\b",
    ],
    "evolution": [
        r"\bкак менял", r"\bяк змін", r"\bevolv", r"\bover the years\b",
        r"\bover time\b", r"\bизменил[аи]сь\b", r"\bпоменял", r"\bразвивал",
        r"\btrajector", r"\bhow did my\b.{0,30}\bchange\b",
    ],
    "timeline": [
        r"\bтаймлайн\b", r"\btimeline\b", r"\bchronolog", r"\bхронолог",
        r"\bwhat happened\b", r"\bчто происходило\b", r"\bщо відбувалося\b",
    ],
}

_DATE_PATTERNS = [
    r"\b(19|20)\d{2}\b",
    r"\b(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)",
    r"\b(січн|лют|березн|квітн|травн|червн|липн|серпн|вересн|жовтн|листопад|грудн)",
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b",
    r"\b(last|прошл|минул|this|эт|цьог)\s+(year|summer|winter|month|год|лет|зим|мес)",
]


@dataclass
class Intent:
    kind: str                       # aggregate|first_mention|evolution|timeline|lookup
    has_temporal_anchor: bool
    matched_pattern: str | None
    apply_recency_decay: bool       # NEVER true when a temporal anchor exists


def route(query: str) -> Intent:
    q = query.lower()

    has_anchor = any(re.search(p, q, re.IGNORECASE) for p in _DATE_PATTERNS)

    for kind, patterns in INTENT_PATTERNS.items():
        for p in patterns:
            if re.search(p, q, re.IGNORECASE):
                return Intent(kind, has_anchor, p, apply_recency_decay=False)

    # Recency decay is OFF unless the query is plainly present-tense.
    # Solr's canonical recip() gives an 8.6x penalty to your 2018 content;
    # Elastic's own docs warn decay functions swamp textual relevance.
    # An archive's interesting queries are archaeological.
    present_tense = bool(re.search(
        r"\b(сейчас|зараз|now|recent|недавно|нещодавно|latest|последн|останн)\b", q))

    return Intent("lookup", has_anchor, None,
                  apply_recency_decay=present_tense and not has_anchor)




# Month stems, January first. Stems, not words: "в січні", "у листопаді",
# "в ноябре", "November" all have to land on the same month.
_MONTHS = ["січн|январ|january", "лют|феврал|february", "березн|март|march",
           "квітн|апрел|april", "травн|ма[йя]|may", "червн|июн|june",
           "липн|июл|july", "серпн|август|august", "вересн|сентябр|september",
           "жовтн|октябр|october", "листопад|ноябр|november",
           "грудн|декабр|december"]
_SEASONS = [(r"(влітку|літ[оа]м?|лет[оа]м?|summer)", 6, 8),
            (r"(восени|осін|осен|autumn|\bfall\b)", 9, 11),
            (r"(взимку|зим|winter)", 12, 2),
            (r"(навесні|весн|spring)", 3, 5)]


def _utc(y: int, m: int) -> datetime:
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return datetime(y, m, 1, tzinfo=timezone.utc)


def window(query: str) -> tuple[datetime, datetime] | None:
    """The date range a question names, with margins — or None.

    Most lookups carry their own anchor ("наприкінці 2024", "влітку 2025",
    "в січні 2026"), and /recall ignored it: 3 of the 11 lookups that missed
    the top 20 in the first eval land inside it once the search is confined to
    the year the question names (2026-09-26, 17 -> 20 of 28).

    Only an unambiguous single year counts; two different years ("between
    2019 and 2022") return None rather than guess. Margins are deliberately
    generous — people misplace events by a month or two, and a window that
    excludes the answer is worse than no window:
        year        Nov of the year before .. Jan of the year after
        season      one month either side
        month       one month either side
    """
    q = query.lower()
    years = set(re.findall(r"\b(20[0-4]\d|19[89]\d)\b", q))
    if len(years) != 1:
        return None
    y = int(years.pop())
    for i, stems in enumerate(_MONTHS):
        if re.search(rf"\b({stems})", q):
            return _utc(y, i), _utc(y, i + 3)          # month-1 .. month+1
    for pattern, first, last in _SEASONS:
        if re.search(pattern, q):
            if first > last:                            # winter spans the year
                return _utc(y, 11 - 12), _utc(y, last + 2)
            return _utc(y, first - 1), _utc(y, last + 2)
    return _utc(y - 1, 11), _utc(y + 1, 2)
