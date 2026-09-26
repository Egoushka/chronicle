"""Fusion and ranking.

Measured contribution of each stage:
    hybrid BM25+dense RRF   +11.9 EM over naive baseline (43.1 -> 55.0)
    cross-encoder rerank    +1.7 EM; failure rate 2.9% -> 1.9%
    2-step loop             most of the remaining agentic gain
    5-step loop             -0.3 EM at 10.3x latency   <- do not
    learned routing         +0.2 EM, sometimes -1.8    <- do not
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .route import Intent

def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[int]], k: int = 60
) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for lst in ranked_lists:
        for rank, doc_id in enumerate(lst, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


@dataclass
class Candidate:
    session_id: int
    rrf: float
    entity_mentions: int = 0      # cheap, non-LLM (Graphiti's segment-mentions)
    node_distance: float = 1.0    # graph proximity to a named person
    importance: float = 0.0
    participant_match: bool = False
    age_days: float = 0.0


def apply_cheap_signals(cands: Sequence[Candidate], intent: Intent) -> list[Candidate]:
    """Non-LLM reranking signals — essentially free, and strong priors on a
    457-sender social graph. Both are COUNT(*) and a join."""
    out = []
    for c in cands:
        boost = 1.0
        boost *= 1.0 + 0.10 * min(c.entity_mentions, 10) / 10.0
        boost *= 1.0 + 0.15 * (1.0 - min(c.node_distance, 3.0) / 3.0)
        boost *= 1.0 + 0.10 * (c.importance / 10.0)
        if c.participant_match:
            boost *= 1.25

        if intent.apply_recency_decay:
            # Gaussian, scale 30d, offset 7d, decay 0.5 — the shape Elastic
            # recommends. Applied ONLY when routing found no temporal anchor.
            # NEVER use Solr-style recip(): 1/(age_years+1) is an 8.6x
            # penalty across a 7.6-year span.
            eff = max(0.0, c.age_days - 7.0)
            sigma_sq = -(30.0 ** 2) / (2 * np.log(0.5))
            boost *= float(np.exp(-(eff ** 2) / (2 * sigma_sq)))

        out.append(Candidate(**{**c.__dict__, "rrf": c.rrf * boost}))
    return sorted(out, key=lambda c: c.rrf, reverse=True)




def spelling_variants(lexemes, lookup, min_key: int = 3, per_key: int = 3) -> list[str]:
    """Other spellings of a question's terms that occur in the archive.

    The chats write the same name in Cyrillic and Latin, and in Russian and
    Ukrainian — `EPAM` and `Епам`, `Одеса` and `Одесса` — and the question
    rarely uses the chat's spelling. Measured on the eval lookups: EPAM
    (12 Latin vs 21 Cyrillic segments) and Odesa (61 Ukrainian vs 337
    Russian) were both unreachable for exactly this reason.

    `lookup(keys)` returns (key, word, ndoc) for archive lexemes sharing a key.
    Keys shorter than `min_key` are skipped: resolve.translit_key is lossy on
    purpose, and a 2-letter key collides with everything.
    """
    from .resolve import translit_key
    own = set(lexemes)
    keys = {translit_key(w) for w in lexemes}
    keys = {k for k in keys if len(k) >= min_key}
    if not keys:
        return []
    by_key: dict[str, list[tuple[int, str]]] = {}
    for key, word, ndoc in lookup(sorted(keys)):
        if word not in own:
            by_key.setdefault(key, []).append((ndoc, word))
    return [w for k in sorted(by_key)
            for _, w in sorted(by_key[k], reverse=True)[:per_key]]
