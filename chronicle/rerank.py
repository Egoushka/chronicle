"""Listwise rerank of /recall's top results by a chat model. Off unless asked.

The 2026-09-29 diagnosis of the 7 missed lookups: three had the gold at rank
33-80 and one answer at rank 1 in only 36% of lookups (p@1). Retrieval finds
the segment and orders it badly; reading 20 segments against the question is
what a model is for. One call per query, over the fused top-N, no resident
model on the host (see ROADMAP "Will not do").

The reranker only REORDERS what retrieval returned. Anything the model omits,
repeats or invents keeps its retrieval position behind the ranked ones, and any
failure returns the input order unchanged: a broken reranker must not break
/recall.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re

from chronicle.gate import RateLimiter, _Http, _post, rpm_from_env

log = logging.getLogger("chronicle.rerank")

SNIPPET_CHARS = 700     # ~13 events a segment; the head carries the topic

SYSTEM = (
    "You rank excerpts of a person's private chats by how likely each is to "
    "contain the answer to their question. Judge by what the excerpt says, "
    "not by shared words. Excerpts are numbered. Reply as JSON: "
    '{"ranking": [<numbers, best first, most relevant ten at most>]}.')


class Reranker(_Http):
    def __init__(self, url: str, key: str, model: str, limiter: RateLimiter):
        self.model, self.limiter = model, limiter
        self._url, self._key, self._http = url.rstrip("/"), key, None
        self.version = f"rerank:{model}:{hashlib.sha1(SYSTEM.encode()).hexdigest()[:6]}"

    def order(self, query: str, texts: list[str]) -> list[int]:
        """Indexes into `texts`, best first. Raises on a transport or parse
        error; `rerank()` turns that into the input order."""
        listing = "\n\n".join(f"[{i + 1}] {t[:SNIPPET_CHARS]}" for i, t in enumerate(texts))
        body = {"model": self.model, "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user",
                              "content": f"QUESTION: {query}\n\nEXCERPTS:\n{listing}"}]}
        out = _post(self.http, "/chat/completions", body, self.limiter)
        content = out["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
        return [int(n) - 1 for n in json.loads(content)["ranking"]]


def from_env() -> Reranker | None:
    """None when no key or model is set: /recall then ignores `rerank`."""
    key = os.environ.get("RERANK_KEY") or os.environ.get("LITELLM_API_KEY", "")
    url = os.environ.get("RERANK_URL") or os.environ.get("LITELLM_BASE_URL", "")
    model = os.environ.get("RERANK_MODEL", "")
    if not (url and key and model):
        return None
    return Reranker(url, key, model, RateLimiter(rpm_from_env("RERANK_RPM", 120)))


def rerank(rr: Reranker, query: str, rows: list, text_of=lambda r: r["text"]) -> list:
    """`rows` reordered, ranked ones first. Same rows in, same rows out."""
    try:
        picked: list[int] = []
        for i in rr.order(query, [text_of(r) for r in rows]):
            if 0 <= i < len(rows) and i not in picked:
                picked.append(i)
    except Exception:
        log.exception("rerank failed; keeping retrieval order")
        return rows
    rest = [i for i in range(len(rows)) if i not in picked]
    return [rows[i] for i in picked + rest]
