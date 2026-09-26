# ADR-003 — chronicle-api encodes queries with BAAI's fp32 ONNX export

**Status:** accepted 2026-09-26 · **Supersedes:** nothing

## Context

chronicle-api averaged 3.0 GiB working set over 7 days (cAdvisor), 3.7 GiB
at the time, on a 5g cap. It is the largest stack on a 31 GiB box that dipped
to 2.9 GiB available. Its only model work is encoding ONE query string per
`/recall`, `/evolution` and `/ground` request, about 1,049 of those in 32 h.

Measured live (`/proc/<pid>/status`, cgroup `memory.stat`): one uvicorn
process, RSS 3,822 MB of which 3,793 MB anonymous, VmHWM 4,563 MB, and
`memory.peak` at the full 5 GiB cap. Shared libraries are ~12 MB; nothing is
cached from Postgres.

Two claims in the brief were wrong. The model is **not** loaded eagerly:
`Embedder()` is a lazy wrapper and the first encode loads it. And it is **not**
resident twice: `chronicle-worker` is `restart: "no"` under the `batch`
profile, so its copy exists only while a scheduled batch runs.

## Measurement

Throwaway `docker compose run --rm` containers from the live image on the box,
one variant per process, same inputs: 40 RU/UK/EN queries and 60 random
substantive segments with their STORED embeddings. Retrieval compared against
the current encoder through the real `hybrid_search` and a dense-only top-20.

| | torch fp16 (was) | torch + `malloc_trim` | **ONNX fp32** | ONNX int8 |
|---|---|---|---|---|
| RSS after load | 2,266 MB | 2,244 MB | 1,497 MB | 1,078 MB |
| RSS steady | 3,979 MB | 3,696 MB | **1,723 MB** | 1,147 MB |
| peak (VmHWM) | 4,430 MB | 4,438 MB | **1,723 MB** | 1,147 MB |
| load | 15.5 s | 15.3 s | 4.6 s | 6.8 s |
| query, mean / p95 | 0.246 / 0.282 s | 0.270 / 0.304 s | **0.052 / 0.059 s** | 0.023 / 0.027 s |
| cos to stored, mean / min | 1.000 / 1.000 | 1.000 / 1.000 | **0.99998 / 0.9986** | 0.985 / 0.979 |
| hybrid top-20 overlap | — | 100% | **100%** | 90.8% |
| hybrid top-20 identical order | — | 40/40 | **40/40** | 0/40 |
| hybrid top-1 unchanged | — | 100% | **100%** | 87.5% |
| dense top-1 unchanged | — | 100% | **100%** | 82.5% |

The load is not what fills torch's memory. It peaks at 4.4 GB while loading
fp32 and halving it, settles at 2.2 GB, and then the FIRST forward pass adds
1.65 GB that is never returned. `malloc_trim` after load recovers 30 MB, so it
is not allocator slack.

## Options

| option | resident | query | vectors | privacy |
|---|---|---|---|---|
| **ONNX fp32, dense only (chosen)** | 1.7 GB | 0.05 s | identical top-20, 40/40 | on box |
| ONNX int8, dense only | 1.1 GB | 0.02 s | top-1 moves on 1 query in 8 | on box |
| lazy load + unload after N idle min | ~0.2 GB idle, **4.4 GB peak unchanged** | 11-16 s cold, and most queries would be cold (bursty agent traffic, facts 25/34) | identical | on box |
| one shared embed service for api + worker | same ~4 GB, one fewer copy **only while a batch runs** | +HTTP hop | identical | on box |
| LiteLLM → hosted bge-m3 | ~0.2 GB | network-bound, not measured: no bge-m3 route exists in LiteLLM (only `text-embedding-3-small`) | compatible if the host serves the same weights | **every query text leaves the box**, which is the exact thing on-box embedding was adopted to stop |

int8 is rejected. A cosine of 0.985 to the stored vectors reorders every result
list, and `eval/questions.json` still holds only the template (4 questions, zero
real evidence ids), so nothing can show whether that reordering is a
regression. fp32 changes nothing, so nothing needs proving. The extra 580 MB
from int8 is not worth an unmeasurable quality risk.

## Decision

- `EMBED_BACKEND=onnx` on chronicle-api. It loads `onnx/model.onnx` from the
  pinned `BAAI/bge-m3` revision already in the `chronicle_models` volume and
  takes the normalized CLS token, which is BGE-M3's dense head.
  (The export's `sentence_embedding` output equals it at cos 1.0.)
- chronicle-worker stays on FlagEmbedding/torch. It wrote every stored vector.
  `tests/test_embed.py` fails if it is switched.
- api `mem_limit` 5g → 2560m: the 1,723 MB peak on 1024-token inputs, plus
  pymorphy3 and the pool, plus ~50%.

## Consequences

- Sparse and ColBERT heads are unavailable on the api. Neither was used.
- The api image still carries torch, because the worker shares it. Splitting
  the image would cut disk, not RAM.
- Switching the WORKER to ONNX, or to int8 anywhere, means re-embedding 51k
  segments (~9.8 h on CPU, measured 2026-08-11) and a real labelled eval first.
