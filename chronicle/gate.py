"""Pre-extraction gate — one cheap yes/no before the expensive enrichment call.

Enrichment asks a chat model for summary, topics, facts and commitments. On
2026-09-29 that was 36,635 calls, $16.41; 27,956 of them (76%) returned no fact
and no commitment. A gate scores each segment first (p in [0, 1]: would the
extractor keep anything?) and enrichment skips the segments below a threshold.

Backends, chosen by GATE_BACKEND:

    none   no gate; every segment is extracted (default, today's behaviour)
    jev    TypeSafe's Jev, one `noul` question, through LiteLLM's /typesafe
           pass-through (docs.litellm.ai/docs/pass_through/typesafe). Optional:
           nothing else in chronicle needs it.
    chat   any LiteLLM chat model, asked for {"p": 0-100}

Measured 2026-09-30 on 600 segments enriched the day before, with the
extractor's own result as the label: jev at p > 0.42 keeps 97% of facts and
commitments while skipping 21% of calls; at p > 0.57, 95% and 32%. AUC 0.77,
~0.3 s a call. The facts lost are mostly `opinion` and `learned`. Claude Haiku
scored AUC 0.71 at 8 s a call; five questions combined by regression did no
better than the one.

The threshold is a recall/cost trade and belongs to the deployment, so
GATE_SKIP=0 (default) is shadow mode: score and store, still extract. Compare
`gate_p` against what extraction produced, then set GATE_SKIP=1.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Protocol

log = logging.getLogger("chronicle.gate")

INSTRUCTIONS = (
    "Does this excerpt of private chats state or clearly imply at least one "
    "durable fact about a person's life (work, plans, health, family, "
    "relationships, places, possessions, preferences, events), or a promise "
    "someone made to do something later?")
CRITERIA = {
    "true": "At least one lasting fact about someone's life, or a promise to "
            "do something later",
    "false": "Only small talk, greetings, reactions, jokes, links, or "
             "logistics of the moment"}

MAX_CHARS = 8000        # same cut as enrich.Client.extract


class RateLimiter:
    """At most `rpm` calls a minute across all threads; 0 = unlimited.

    Spaces calls evenly rather than bursting and waiting: on 2026-09-29 the
    worker ran ~895 enrich calls a minute, took its $10 LiteLLM key to $17.54
    and the box's $50 budget to 96%. Each caller reserves the next slot under
    the lock and sleeps outside it.
    """

    def __init__(self, rpm: float):
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        if slot > now:
            time.sleep(slot - now)


def rpm_from_env(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


class Backend(Protocol):
    version: str

    def score(self, text: str) -> float: ...


def _post(http, path: str, body: dict, limiter: RateLimiter) -> dict:
    """POST with one retry for the transient class, like enrich.Client."""
    for attempt in (1, 2):
        limiter.wait()
        r = http.post(path, json=body)
        if r.status_code in (429, 500, 502, 503, 504) and attempt == 1:
            time.sleep(2)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


class _Http:
    """httpx client built on first use, so importing this module and building a
    Gate needs nothing beyond the standard library (`make test` installs no
    HTTP client). Tests assign `.http` directly."""

    _http = None

    @property
    def http(self):
        if self._http is None:
            import httpx
            self._http = httpx.Client(base_url=getattr(self, "_url", ""), timeout=30,
                                      headers={"Authorization": f"Bearer {self._key}"})
        return self._http

    @http.setter
    def http(self, value):
        self._http = value


class Jev(_Http):
    def __init__(self, url: str, key: str, model: str, limiter: RateLimiter):
        self.url, self.model, self.limiter = url, model, limiter
        self._key, self._http = key, None
        # The question is part of the score: editing it must invalidate
        # stored gate_p, so it goes into the version.
        q = hashlib.sha1((INSTRUCTIONS + json.dumps(CRITERIA, sort_keys=True))
                         .encode()).hexdigest()[:6]
        self.version = f"jev:{model}:noul-{q}"

    def score(self, text: str) -> float:
        body = {"state": text[:MAX_CHARS], "model": self.model,
                "questions": {"keep": {"type": "noul",
                                       "instructions": INSTRUCTIONS,
                                       "criteria": CRITERIA}}}
        out = _post(self.http, self.url, body, self.limiter)
        return float(out["answers"]["keep"]["noul"])


class Chat(_Http):
    SYSTEM = (f"You gate a memory extractor. {INSTRUCTIONS} Answer with p, "
              f"your probability 0-100 that it does. Yes means: "
              f"{CRITERIA['true']}. No means: {CRITERIA['false']}. "
              'Reply as JSON: {"p": <0-100>}.')

    def __init__(self, url: str, key: str, model: str, limiter: RateLimiter):
        self.model, self.limiter = model, limiter
        self._url, self._key, self._http = url.rstrip("/"), key, None
        q = hashlib.sha1(self.SYSTEM.encode()).hexdigest()[:6]
        self.version = f"chat:{model}:{q}"

    def score(self, text: str) -> float:
        body = {"model": self.model, "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": self.SYSTEM},
                             {"role": "user",
                              "content": f"EXCERPT:\n{text[:MAX_CHARS]}"}]}
        out = _post(self.http, "/chat/completions", body, self.limiter)
        content = out["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
        return float(json.loads(content)["p"]) / 100


class Gate:
    def __init__(self, backend: Backend, threshold: float, skip: bool):
        self.backend, self.threshold, self.skip = backend, threshold, skip

    @property
    def version(self) -> str:
        return self.backend.version

    def keeps(self, p: float) -> bool:
        return p >= self.threshold

    @classmethod
    def from_env(cls) -> "Gate | None":
        """None when GATE_BACKEND is none/unset, or misconfigured (logged):
        a broken gate must not stop enrichment, only leave it ungated."""
        name = os.environ.get("GATE_BACKEND", "none").strip().lower()
        if name in ("", "none"):
            return None
        if name not in ("jev", "chat"):
            log.error("GATE_BACKEND=%r is not none|jev|chat; gate off", name)
            return None
        key = os.environ.get("GATE_KEY", "")
        base = os.environ.get("LITELLM_BASE_URL", "").rstrip("/")
        limiter = RateLimiter(rpm_from_env("GATE_RPM", 300))
        if name == "jev":
            # LITELLM_BASE_URL ends in /v1 (the OpenAI path); the pass-through
            # lives at the proxy root.
            root = base[:-3] if base.endswith("/v1") else base
            url = os.environ.get("GATE_URL") or (
                f"{root}/typesafe/v1/systemone" if root else "")
            model = os.environ.get("GATE_MODEL") or "jev-latest"
        else:
            url = os.environ.get("GATE_URL") or base
            model = os.environ.get("GATE_MODEL", "")
        if not (url and key and model):
            log.error("GATE_BACKEND=%s needs GATE_KEY, GATE_URL (or "
                      "LITELLM_BASE_URL) and GATE_MODEL; gate off", name)
            return None
        threshold = float(os.environ.get("GATE_THRESHOLD") or 0.42)
        skip = os.environ.get("GATE_SKIP", "0") == "1"
        backend = Jev(url, key, model, limiter) if name == "jev" \
            else Chat(url, key, model, limiter)
        return cls(backend, threshold, skip)
