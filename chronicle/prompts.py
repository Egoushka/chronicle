"""Chat prompts from Langfuse, with the code's own text as the fallback.

A prompt is a template plus values: the template lives in Langfuse (versions,
labels), the code passes the values, and the first call of each version logs
`prompt=name@version` so a cost or quality change can be tied to a prompt
change. Langfuse unreachable, keys unset, prompt missing or not the expected
shape: the embedded default is used and the version reads "default".

Templates use {{variable}} and are substituted in ONE pass, so a value that
itself contains {{x}} (a chat message can) is never expanded. Static text goes
in the system message and per-call values in the user message: the provider's
prompt cache only matches a prefix.

Stdlib only (the same module shape as the homelab services' prompts.py).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request

log = logging.getLogger("chronicle.prompts")

TTL = 300       # seconds a fetched template is reused
FAIL_TTL = 60   # seconds before Langfuse is tried again after a failure
_VAR = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
_cache: dict[str, tuple[float, list[dict], str]] = {}
_logged: dict[str, str] = {}


def _fetch(name: str) -> tuple[list[dict], str]:
    host = os.environ["LANGFUSE_HOST"].rstrip("/")
    label = os.environ.get("PROMPT_LABEL", "production")
    cred = f'{os.environ["LANGFUSE_PUBLIC_KEY"]}:{os.environ["LANGFUSE_SECRET_KEY"]}'
    url = (f"{host}/api/public/v2/prompts/{urllib.parse.quote(name, safe='')}"
           f"?label={urllib.parse.quote(label)}")
    req = urllib.request.Request(
        url, headers={"Authorization": "Basic " + base64.b64encode(cred.encode()).decode()})
    with urllib.request.urlopen(req, timeout=5) as r:
        d = json.load(r)
    msgs = d.get("prompt")
    if d.get("type") != "chat" or not isinstance(msgs, list) or not all(
            isinstance(m, dict) and isinstance(m.get("content"), str) for m in msgs):
        raise ValueError("not a chat prompt")
    return [{"role": m["role"], "content": m["content"]} for m in msgs], str(d.get("version", "?"))


def get(name: str, default: list[dict]) -> tuple[list[dict], str]:
    """(messages, version) for `name`: Langfuse's labelled version, else `default`."""
    now = time.time()
    hit = _cache.get(name)
    if hit and hit[0] > now:
        return hit[1], hit[2]
    configured = all(os.environ.get(k) for k in
                     ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"))
    if configured:
        try:
            msgs, ver = _fetch(name)
            # Same roles in the same order as the default, or the caller's
            # variables would land in the wrong message.
            if [m["role"] for m in msgs] != [m["role"] for m in default]:
                raise ValueError("roles differ from the embedded default")
            _cache[name] = (now + TTL, msgs, ver)
            return msgs, ver
        except Exception as e:
            log.warning("prompt %s: Langfuse fetch failed (%s: %s); using embedded default",
                        name, type(e).__name__, e)
    _cache[name] = (now + (FAIL_TTL if configured else TTL), default, "default")
    return default, "default"


def render(name: str, default: list[dict], **values: str) -> list[dict]:
    """The chat messages for `name` with {{variables}} filled. A variable the
    caller did not pass stays visible rather than becoming empty."""
    msgs, ver = get(name, default)
    if _logged.get(name) != ver:
        _logged[name] = ver
        log.info("prompt=%s@%s", name, ver)

    def fill(s: str) -> str:
        return _VAR.sub(lambda m: str(values.get(m.group(1), m.group(0))), s)

    return [{"role": m["role"], "content": fill(m["content"])} for m in msgs]
