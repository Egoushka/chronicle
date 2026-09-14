"""Embedding — BGE-M3, on box.

Every message in telegram-sync has already been sent to OpenAI via
`text-embedding-3-small`. Moving to BGE-M3 is the change that stops that, and
it is also the better model for this corpus:

    ruMTEB retrieval avg   BGE-M3 74.79  ·  mE5-large 74.04  ·  mE5-base 67.14
    MIRACL-ru nDCG@10      BGE-M3 70.16  ·  mE5-large 67.33

Note the trap in that table: the Russian-SPECIALIZED model (ru-en-RoSBERTa)
wins on classification and STS and LOSES retrieval by 8+ points. Specialized
does not mean better at retrieval.

Ukrainian has essentially no published retrieval evaluation — there is no
ukMTEB. Build a held-out set from your own messages; a few hundred
query/positive pairs will tell you more than any leaderboard.
"""

from __future__ import annotations

import logging
import os
from typing import Sequence

import numpy as np

log = logging.getLogger(__name__)

EMBED_DIM = 1024
EMBEDDER_VERSION = "bge-m3-1024-v1"


class Embedder:
    """Lazy-loading BGE-M3 wrapper.

    Loading is deferred because `chronicle-api` boots with a 180s
    start_period and the healthcheck must answer before the model is warm.
    """

    def __init__(self, model_name: str | None = None, use_fp16: bool = True,
                 max_length: int = 1024):
        self.model_name = model_name or os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
        self.use_fp16 = use_fp16
        # Segments are capped at ~250 tokens. BGE-M3 supports 8192, but
        # padding to it wastes most of the forward pass.
        self.max_length = max_length
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from FlagEmbedding import BGEM3FlagModel
            log.info("loading %s (first call is slow)", self.model_name)
            self._model = BGEM3FlagModel(self.model_name, use_fp16=self.use_fp16)
        return self._model

    def encode(self, texts: Sequence[str], batch_size: int = 12,
               with_sparse: bool = False) -> dict:
        if not texts:
            return {"dense": np.zeros((0, EMBED_DIM), dtype=np.float16), "sparse": None}
        out = self.model.encode(
            list(texts),
            batch_size=batch_size,
            max_length=self.max_length,
            return_dense=True,
            return_sparse=with_sparse,
            # Late interaction is off deliberately: no competitive RU/UK model
            # exists (jina-colbert-v2 MIRACL-ru 64.3 vs BGE-M3 dense 70.1), and
            # BGE-M3's own ColBERT head adds ~1 point on 100-word passages,
            # extrapolating to ~0 on ours. See docs/RESEARCH.md §12.4.
            return_colbert_vecs=False,
        )
        return {
            "dense": np.asarray(out["dense_vecs"], dtype=np.float16),  # -> halfvec
            "sparse": out.get("lexical_weights") if with_sparse else None,
        }

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])["dense"][0]


class Lemmatizer:
    """RU/UK lemmatization for the lexical half of hybrid retrieval.

    RusBEIR: "lemmatization using PyMorphy3 proves critical for lexical
    performance", and BM25 beats BGE-M3 by 13pp on some Russian tasks. For
    "when did I FIRST mention X" — a bare token in a 15-char message — the
    lexical path does essentially all the work.
    """

    import re as _re
    _WORD = _re.compile(r"[\wЀ-ӿ]+", _re.UNICODE)

    def __init__(self):
        import pymorphy3
        self.ru = pymorphy3.MorphAnalyzer(lang="ru")
        try:
            self.uk = pymorphy3.MorphAnalyzer(lang="uk")
        except Exception:                                  # noqa: BLE001
            log.warning("Ukrainian dictionary unavailable; RU analyzer only. "
                        "pip install pymorphy3-dicts-uk")
            self.uk = None

    def __call__(self, text: str) -> str:
        out: list[str] = []
        for tok in self._WORD.findall(text.lower()):
            if tok.isascii():
                out.append(tok)                     # English, code, usernames
                continue
            ru = self.ru.parse(tok)[0]
            best = ru
            if self.uk is not None:
                uk = self.uk.parse(tok)[0]
                # Surzhyk and code-switching make a single-language assumption
                # wrong; take whichever analyzer is more confident per token.
                if uk.score > ru.score:
                    best = uk
            out.append(best.normal_form)
        return " ".join(out)
