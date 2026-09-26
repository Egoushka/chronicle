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

# The revision every stored vector was produced from — the /models cache on the
# box. BAAI ships an official fp32 ONNX export of the same weights in it, so
# the api's encoder is first-party files, not a third-party port.
BGE_M3_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


class Embedder:
    """Lazy-loading BGE-M3 wrapper.

    Loading is deferred because `chronicle-api` boots with a 180s
    start_period and the healthcheck must answer before the model is warm.

    Two backends over the SAME weights and the same vector space:

      flag  FlagEmbedding/torch fp16. The worker: it wrote every stored vector
            and keeps writing them, so the corpus stays one encoder.
      onnx  BAAI's fp32 ONNX export, dense head only. The api: it encodes one
            query per request, and torch held 3.98 GB resident (4.43 GB peak)
            for that; this holds 1.72 GB and returns byte-identical top-20s.
            int8 was measured and REJECTED — see docs/ADR-003.
    """

    def __init__(self, model_name: str | None = None, use_fp16: bool = True,
                 max_length: int = 1024, backend: str | None = None):
        self.model_name = model_name or os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
        self.use_fp16 = use_fp16
        # Segments are capped at ~250 tokens. BGE-M3 supports 8192, but
        # padding to it wastes most of the forward pass.
        self.max_length = max_length
        self.backend = backend or os.environ.get("EMBED_BACKEND", "flag")
        self._model = None

    @property
    def model(self):
        if self._model is None:
            log.info("loading %s via %s (first call is slow)", self.model_name, self.backend)
            if self.backend == "onnx":
                self._model = _OnnxDense(self.model_name, self.max_length)
            else:
                from FlagEmbedding import BGEM3FlagModel
                self._model = BGEM3FlagModel(self.model_name, use_fp16=self.use_fp16)
        return self._model

    def encode(self, texts: Sequence[str], batch_size: int = 12,
               with_sparse: bool = False) -> dict:
        if not texts:
            return {"dense": np.zeros((0, EMBED_DIM), dtype=np.float16), "sparse": None}
        if self.backend == "onnx":
            if with_sparse:
                raise ValueError("the onnx backend is dense-only; use EMBED_BACKEND=flag")
            return {"dense": np.stack([self.model(t) for t in texts]), "sparse": None}
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


class _OnnxDense:
    """BGE-M3's dense head, which is the normalized CLS token — nothing more.

    One text per run, unpadded: the api encodes a single query, and skipping
    padding means no attention-mask edge cases to get subtly wrong.
    """
    # ponytail: no batching; the worker stays on torch for bulk encoding.

    def __init__(self, model_name: str, max_length: int):
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer

        # Already in the chronicle_models volume: the first deploy's
        # snapshot_download fetched the whole repo, onnx/ included.
        snap = snapshot_download(model_name, revision=BGE_M3_REVISION,
                                 allow_patterns=["onnx/*"])
        self.tok = Tokenizer.from_file(f"{snap}/onnx/tokenizer.json")
        self.tok.enable_truncation(max_length)
        so = ort.SessionOptions()
        # onnxruntime ignores OMP_NUM_THREADS and defaults to every core; the
        # same oversubscription compose.yaml measured for torch applies here.
        so.intra_op_num_threads = int(os.environ.get("OMP_NUM_THREADS", "0"))
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(f"{snap}/onnx/model.onnx", so,
                                         providers=["CPUExecutionProvider"])

    def __call__(self, text: str) -> np.ndarray:
        ids = np.array([self.tok.encode(text).ids], dtype=np.int64)
        (hidden,) = self.sess.run(["token_embeddings"],
                                  {"input_ids": ids, "attention_mask": np.ones_like(ids)})
        cls = hidden[0, 0]
        return (cls / np.linalg.norm(cls)).astype(np.float16)


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

