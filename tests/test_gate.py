import time

import pytest

from chronicle import gate as g


class FakeResp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeHttp:
    def __init__(self, *resps):
        self.resps, self.posts = list(resps), []

    def post(self, path, json):
        self.posts.append((path, json))
        return self.resps.pop(0)


def _jev(http):
    j = g.Jev("http://x/typesafe/v1/systemone", "k", "jev-latest", g.RateLimiter(0))
    j.http = http
    return j


def _env(monkeypatch, **kw):
    for k in ("GATE_BACKEND", "GATE_URL", "GATE_KEY", "GATE_MODEL", "GATE_THRESHOLD",
              "GATE_SKIP", "GATE_RPM", "LITELLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_gate_is_off_by_default_and_when_misconfigured(monkeypatch):
    _env(monkeypatch)
    assert g.Gate.from_env() is None
    _env(monkeypatch, GATE_BACKEND="jev")             # no key, no url
    assert g.Gate.from_env() is None
    _env(monkeypatch, GATE_BACKEND="bogus", GATE_KEY="k")
    assert g.Gate.from_env() is None


def test_jev_url_is_the_proxy_root_not_the_openai_path(monkeypatch):
    _env(monkeypatch, GATE_BACKEND="jev", GATE_KEY="k",
         LITELLM_BASE_URL="http://litellm:4000/v1")
    gate = g.Gate.from_env()
    assert gate.backend.url == "http://litellm:4000/typesafe/v1/systemone"
    assert gate.threshold == 0.42 and gate.skip is False        # shadow by default
    assert gate.version.startswith("jev:jev-latest:noul-")


def test_skip_and_threshold_come_from_the_environment(monkeypatch):
    _env(monkeypatch, GATE_BACKEND="jev", GATE_KEY="k", GATE_URL="http://x",
         GATE_THRESHOLD="0.57", GATE_SKIP="1")
    gate = g.Gate.from_env()
    assert gate.skip and gate.keeps(0.57) and not gate.keeps(0.56)


def test_jev_request_shape_and_answer():
    http = FakeHttp(FakeResp(200, {"answers": {"keep": {"noul": 0.61}}}))
    assert _jev(http).score("x" * 20000) == 0.61
    _, body = http.posts[0]
    assert body["model"] == "jev-latest" and len(body["state"]) == g.MAX_CHARS
    assert body["questions"]["keep"]["type"] == "noul"


def test_transient_error_retries_once_then_raises(monkeypatch):
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    ok = FakeResp(200, {"answers": {"keep": {"noul": 0.5}}})
    assert _jev(FakeHttp(FakeResp(429, {}), ok)).score("t") == 0.5
    with pytest.raises(RuntimeError):
        _jev(FakeHttp(FakeResp(401, {}))).score("t")            # 4xx: no retry


def test_chat_backend_reads_p_as_percent():
    c = g.Chat("http://x/v1", "k", "m", g.RateLimiter(0))
    c.http = FakeHttp(FakeResp(200, {"choices": [{"message": {"content": '```json\n{"p": 42}\n```'}}]}))
    assert c.score("t") == 0.42


def test_version_changes_with_the_model():
    a = g.Jev("u", "k", "jev-latest", g.RateLimiter(0)).version
    assert a != g.Jev("u", "k", "jev-2", g.RateLimiter(0)).version


def test_rate_limiter_spaces_calls():
    lim = g.RateLimiter(600)                            # 0.1 s apart
    start = time.monotonic()
    for _ in range(4):
        lim.wait()
    assert time.monotonic() - start >= 0.29
    start = time.monotonic()
    for _ in range(50):
        g.RateLimiter(0).wait()
    assert time.monotonic() - start < 0.05
