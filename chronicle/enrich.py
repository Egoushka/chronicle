"""Enrichment — summary, topics, facts, commitments. The last stage, on purpose.

Retrieval works without any of this. What it adds:

    topics + facts  -> CONCATENATED onto embed_text, never substituted for it.
                       LongMemEval: key = value + facts is +9.4% recall; the
                       condensed forms ALONE do not improve recall at all.
    facts           -> the bi-temporal `fact` table, where resolve_fact_conflicts()
                       picks the current value with max(version). The model
                       never decides which of two facts is newer.
    commitments     -> what `open_commitments` / v_forgotten_commitments read.
    summary         -> shown next to a hit. Not indexed on its own: summaries
                       are the WORST memory unit measured (SeCom 53.87-56.25
                       against 71.57 for segments).

The model is a cheap CLOUD model through LiteLLM (decided 2026-09-26). No
local model fits the box — whisperx was retired the same day for holding
3.6 GiB — and telegram-sync already sends voice notes to Groq, so this is the
same trade applied to text. It is also why only NARRATIVE, substantive
segments are sent: a wakapi rollup has nothing to extract, and a burst of
`ок` is not worth a call.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime

log = logging.getLogger("chronicle.enrich")

EXTRACTOR_VERSION = "enrich-2026.09-v1"

#: Caps on what one segment may contribute. A segment is <=30 messages, so a
#: model returning 40 "facts" is hallucinating or restating every line.
MAX_TOPICS, MAX_FACTS, MAX_COMMITMENTS = 6, 8, 4

#: Yehor's own messages carry these sender names (telegram-sync writes "me"
#: for outgoing 1:1 messages). Facts about him share one entity.
OWNER = "Yehor"
_OWNER_ALIASES = {"me", "i", "yehor", "yehor hrushevskyi", "егор", "єгор",
                  "я", "егор грушевский", "єгор грушевський"}

PROMPT = """You read one excerpt of Yehor's private Telegram chats and extract \
structured memory. Lines are "sender: text". The sender "me" is Yehor.

Return ONE JSON object with exactly these keys:
  "summary":      1-2 sentences, in the excerpt's main language, what happened.
  "topics":       up to 6 short lowercase English topic labels.
  "importance":   0.0-1.0, how much this would matter to Yehor a year later.
  "sentiment":    -1.0 (negative) to 1.0 (positive), the overall tone.
  "facts":        durable facts stated or clearly implied, each
                  {{"subject": person name ("Yehor" for him), "predicate": one of
                  [{predicates}], "object": short value, "confidence": 0.0-1.0}}.
                  Only facts about people's lives, never about the chat itself.
  "commitments":  promises to do something later, each {{"text": what was
                  promised, "direction": "i_owe" if Yehor promised,
                  "owed_to_me" if someone promised Yehor, "due": "YYYY-MM-DD"
                  or null, "confidence": 0.0-1.0}}.

Empty lists are the right answer for small talk. Do not invent.

Chat: {chat}
Date: {date}

{text}"""


# ---------------------------------------------------------------------------
#  validation — the model's output is untrusted input
# ---------------------------------------------------------------------------

def _num(v, lo: float, hi: float, default: float) -> float:
    try:
        return min(hi, max(lo, float(v)))
    except (TypeError, ValueError):
        return default


def _str(v, limit: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:limit]


def owner_or(name: str) -> str:
    return OWNER if name.strip().lower() in _OWNER_ALIASES else name.strip()


def clean(raw: dict, predicates: set[str]) -> dict:
    """Coerce a model reply into the shape the writer trusts.

    Everything is bounded, typed and clamped; a predicate outside the closed
    vocabulary drops the fact rather than creating a predicate — a free-text
    predicate is a fact that never supersedes anything (migrations/005).
    """
    topics = []
    for t in raw.get("topics") or []:
        t = _str(t, 40).lower()
        if t and t not in topics:
            topics.append(t)

    facts = []
    for f in raw.get("facts") or []:
        if not isinstance(f, dict):
            continue
        pred = _str(f.get("predicate"), 40).lower().replace(" ", "_")
        subj, obj = _str(f.get("subject"), 80), _str(f.get("object"), 200)
        if pred in predicates and subj and obj:
            facts.append({"subject": owner_or(subj), "predicate": pred, "object": obj,
                          "confidence": _num(f.get("confidence"), 0, 1, 0.5)})

    commitments = []
    for c in raw.get("commitments") or []:
        if not isinstance(c, dict) or not _str(c.get("text"), 300):
            continue
        direction = c.get("direction")
        if direction not in ("i_owe", "owed_to_me"):
            continue
        try:
            due = date.fromisoformat(str(c.get("due")))
        except ValueError:
            due = None
        commitments.append({"text": _str(c["text"], 300), "direction": direction,
                            "due": due,
                            "confidence": _num(c.get("confidence"), 0, 1, 0.5)})

    return {"summary": _str(raw.get("summary"), 600) or None,
            "topics": topics[:MAX_TOPICS],
            "importance": _num(raw.get("importance"), 0, 1, 0.0),
            "sentiment": _num(raw.get("sentiment"), -1, 1, 0.0),
            "facts": facts[:MAX_FACTS],
            "commitments": commitments[:MAX_COMMITMENTS]}


def fact_line(f: dict) -> str:
    """How a fact reads inside embed_text."""
    return f"{f['subject']} {f['predicate'].replace('_', ' ')} {f['object']}"


# ---------------------------------------------------------------------------
#  the model call
# ---------------------------------------------------------------------------

class Client:
    """One OpenAI-compatible chat endpoint. LiteLLM in production, a stub in
    scripts/worker-itest.py."""

    def __init__(self, base_url: str, api_key: str, model: str):
        import httpx
        self.model = model
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=90,
                                 headers={"Authorization": f"Bearer {api_key}"})

    @classmethod
    def from_env(cls) -> "Client | None":
        model = os.environ.get("ENRICH_MODEL", "")
        key = os.environ.get("LITELLM_API_KEY", "")
        url = os.environ.get("ENRICH_URL") or os.environ.get("LITELLM_BASE_URL", "")
        return cls(url, key, model) if model and key and url else None

    def extract(self, chat: str, started_at: datetime, text: str,
                predicates: list[str]) -> dict:
        prompt = PROMPT.format(predicates=", ".join(predicates), chat=chat,
                               date=f"{started_at:%Y-%m-%d %A}", text=text[:8000])
        body = {"model": self.model, "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}]}
        for attempt in (1, 2):
            r = self.http.post("/chat/completions", json=body)
            # One retry for the transient class only; a 4xx other than 429 is
            # a bug or a bad key and retrying it just doubles the bill.
            if r.status_code in (429, 500, 502, 503, 504) and attempt == 1:
                continue
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            # Some providers wrap JSON mode output in a ```json fence anyway.
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
            return json.loads(content)
        raise RuntimeError("unreachable")
