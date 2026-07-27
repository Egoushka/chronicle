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
    entity_mentions: int = 0      # cheap, non-LLM (Graphiti's episode-mentions)
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


