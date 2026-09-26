"""The api and the worker run different encoders over the same weights.

That is only safe while the WORKER stays on the backend that wrote every
stored vector. ONNX fp32 matched it 40/40 on top-20 (docs/ADR-003), but it is
still a different numeric path, and mixing two encoders into one corpus is
the kind of drift nobody sees until retrieval quietly degrades.
"""

from pathlib import Path

COMPOSE = (Path(__file__).parent.parent / "compose.yaml").read_text()


def _service(name: str) -> str:
    return COMPOSE.split(f"\n  {name}:\n", 1)[1].split("\n  chronicle-", 1)[0]


def test_api_encodes_queries_with_onnx():
    assert "EMBED_BACKEND: onnx" in _service("chronicle-api")


def test_worker_stays_on_the_encoder_that_wrote_the_corpus():
    assert "EMBED_BACKEND" not in _service("chronicle-worker")
