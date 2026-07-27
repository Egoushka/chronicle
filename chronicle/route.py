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


