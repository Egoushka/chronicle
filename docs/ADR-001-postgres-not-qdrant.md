# ADR-001 — One PostgreSQL, not Postgres + Qdrant

**Status:** accepted · **Date:** 2026-07-25

## Context

`telegram-sync` already writes vectors to Qdrant (collection
`telegram_personal`, 1536-dim, `text-embedding-3-small`). Chronicle could keep
using it.

## Decision

Chronicle stores vectors in its own PostgreSQL via pgvector `halfvec(1024)`,
with **no ANN index**. Qdrant stays for `agent-runner`'s working memory.

## Why

**ANN solves a problem that doesn't exist here.** After aggregation there are
~50k segments for one user. `halfvec(1024) × 50k ≈ 123 MB` — it fits in
`shared_buffers` and exact cosine over it is single-digit milliseconds.

**Filtered ANN fails exactly where it's needed.** Qdrant's own filterable-HNSW
analysis derives a percolation threshold `p_c = 1/⟨k⟩` below which the graph
fragments and greedy search fails. A one-month date filter selects ~1.1% of the
corpus — deep in that regime. ChronoQA measured the same thing empirically from
the other side: a naive temporal filter scored **0.4903 R@5 vs native RAG's
0.5458**. Filtering *reduced* recall. Exact scan makes every filter exact and
free.

**Silent recall loss is the worst failure mode for an archive.** You never learn
what you didn't find. Qdrant's docs warn that without a payload index it cannot
estimate cardinality, "causing extremely slow search times **or low accuracy
results**."

**Two systems is one too many.** The relational data and the vectors describe
the same rows. Split, you get no transactional consistency, no joins without an
application round-trip, two backup regimes, and permanent reindex-drift risk.

**Independent validation:** `khoj`, already running in this homelab, uses
`pgvector/pgvector:pg16`.

## Trade-offs

- Lose Qdrant's tuned Query API and sparse-vector ergonomics; RRF is
  implemented here instead (~20 lines, in `002_retrieval.sql`).
- Lose multivector/late-interaction — but Qdrant disables HNSW for multivectors
  anyway, and there is no competitive RU/UK late-interaction model
  (jina-colbert-v2 MIRACL-ru 64.3 vs BGE-M3 dense 70.1).
- Migration cost. Mitigated by the fact that the indexing unit changes from
  message to segment, so everything is re-embedded regardless. **This is the
  cheapest moment this decision will ever be.**

## Where it fails

Past ~1M segments exact scan stops being free. The fix is one line — pgvector
supports HNSW — and it is a reversible decision. At ~90k new Telegram messages
a year that point arrives well after 2035.
