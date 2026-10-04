import json

from chronicle import rerank as r


class FakeResp:
    status_code = 200

    def __init__(self, content):
        self._c = content

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}

    def raise_for_status(self):
        pass


class FakeHttp:
    def __init__(self, content):
        self.content, self.posts = content, []

    def post(self, path, json):
        self.posts.append((path, json))
        if isinstance(self.content, Exception):
            raise self.content
        return FakeResp(self.content)


def _rr(content):
    rr = r.Reranker("http://x/v1", "k", "m", r.RateLimiter(0))
    rr.http = FakeHttp(content)
    return rr


ROWS = [{"text": t} for t in ("a", "b", "c", "d")]


def test_ranked_first_rest_in_retrieval_order():
    out = r.rerank(_rr(json.dumps({"ranking": [3, 1]})), "q", ROWS)
    assert [x["text"] for x in out] == ["c", "a", "b", "d"]


def test_duplicates_and_invented_numbers_are_ignored():
    out = r.rerank(_rr(json.dumps({"ranking": [2, 2, 9, 0, 4]})), "q", ROWS)
    assert [x["text"] for x in out] == ["b", "d", "a", "c"]


def test_fenced_json_is_parsed():
    out = r.rerank(_rr('```json\n{"ranking": [4]}\n```'), "q", ROWS)
    assert out[0]["text"] == "d"


def test_failure_keeps_retrieval_order():
    assert r.rerank(_rr(RuntimeError("503")), "q", ROWS) == ROWS
    assert r.rerank(_rr("not json"), "q", ROWS) == ROWS


def test_from_env_needs_model_and_key(monkeypatch):
    for k in ("RERANK_KEY", "RERANK_URL", "RERANK_MODEL", "LITELLM_API_KEY",
              "LITELLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    assert r.from_env() is None
    monkeypatch.setenv("LITELLM_API_KEY", "k")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://x/v1")
    assert r.from_env() is None
    monkeypatch.setenv("RERANK_MODEL", "m")
    assert r.from_env().model == "m"


def test_prompt_numbers_excerpts_from_one():
    rr = _rr(json.dumps({"ranking": [1]}))
    r.rerank(rr, "when?", ROWS)
    msg = rr.http.posts[0][1]["messages"][1]["content"]
    assert "[1] a" in msg and "[4] d" in msg and "QUESTION: when?" in msg
