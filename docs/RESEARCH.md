# Personal Telegram Intelligence System — Research & Architecture

**Date:** 25 July 2026
**Corpus:** measured live from your `telegram-sync` MCP, not assumed
**Constraints:** hybrid privacy boundary · self-hosted Hetzner · zero marginal API budget for backfill

---

## 1. Executive summary

I queried your live system before writing anything. Six numbers reframe the entire problem:

| Measurement | Value |
|---|---|
| Messages | **681,331** |
| Chats / distinct senders | **487 / 457** |
| Span | 2018-12-30 → 2026-07-25 (**7.6 years**, ~245 msg/day) |
| Messages **< 20 chars** (incl. 45,375 media-only) | **442,954 — 65.0%** |
| Messages **< 60 chars** | **640,367 — 94.0%** |
| Messages **> 200 chars** | **10,484 — 1.5%** |
| Messages with `reply_to_id` | **54,041 — 7.9%** |
| Voice + video notes / photos / documents | **18,638 / 12,100 / 2,113** |

**The headline finding: your architecture's problem is not Qdrant, not the embedding model, and not the retrieval strategy. It is that you embedded the wrong unit.**

You have ~442,000 vectors representing strings like `ок`, `ага`, `+1`, `да`, `😂`. These are not retrieval units — they are index noise. They carry no proposition, share no lexical surface with any query, and crowd every ANN neighbourhood they land in. Every downstream weakness you're feeling traces back to this one decision, and no amount of tuning the layers above it will fix it.

The literature is unanimous and quantified. Microsoft Research's SeCom (ICLR 2025) measured retrieval quality by memory unit on conversational data: turn-level scored 65.58, session-level 63.16, **segment-level 71.57**, and summaries *worst of all* at 53.87–56.25. LongMemEval's ablation found augmenting retrieval keys with extracted facts gave **+9.4% recall and +5.4% QA accuracy**. LoCoMo's own granularity table ranks LLM-extracted observations (38.0) above raw turns (35.8) above session summaries (31.5). **No published system embeds individual short utterances.** Yours does, 442,000 times.

Those studies operated at ~30 tokens per turn. Yours are ~5–10. The effect is not just larger — it is qualitative. A 30-token turn usually contains a complete proposition. A 20-character Telegram message is a fragment, a reaction, or a continuation.

### The seven decisions that matter

1. **Aggregate before you index.** Segment into time-gap sessions per chat. 681k unusable units become **~50,000–70,000 usable ones**. This single change is worth more than everything else in this document combined.
2. **Index a composite string, retrieve the raw text.** Embed `[deterministic header] + [session text] + [extracted facts]`; store raw session text as the payload. Two independent benchmarks confirm facts must be *concatenated with*, never *substituted for*, the original.
3. **Treat time as SQL, not as retrieval.** "When did I first mention X" is `SELECT MIN(ts) WHERE matches(text, X)` — an argmin, not a top-k. Four independent benchmarks show 82–94% accuracy on fact lookup collapsing to **15–35% on ordering and aggregation**. Both of your headline example queries sit on the wrong side of that cliff.
4. **Collapse Qdrant into PostgreSQL.** For one user at ~60k session vectors, ANN solves a problem you don't have. Exact scan makes every date filter exact and free — sidestepping the HNSW percolation failure that bites precisely at the 1%-cardinality date ranges you'll query most.
5. **Bi-temporal facts, invalidated not deleted.** Steal Graphiti's `t_valid`/`t_invalid` separated from `t_created`/`t_expired`. Over 7.6 years this is the difference between an archive and a snapshot. Implement as Postgres tables; you do not need Neo4j.
6. **Resolve contradictions in code, never in a prompt.** BM25 retrieve → LLM extracts candidates *without comparing* → Python `max(timestamp)`. Measured **+21pp at long context** over LLM adjudication, at ~$0.0001/query. Telegram timestamps make this free.
7. **Reject the memory pyramid you proposed.** It is a lossy compression chain where every layer is derived by summarization from the one below. Drift compounds over 7.6 years, and summarization is the *worst*-scoring granularity in the literature. Replace it with a star schema: one immutable episodic spine, many versioned projections that all cite evidence.

### What to build first

Build **`ripgrep` over your archive** and measure it. Letta scored 74.0% on LoCoMo using nothing but a filesystem and `grep` — beating Mem0's 68.5%. On Mem0's own benchmark table, the full-context baseline (72.90) beats Mem0 (66.88) and Mem0-graph (68.44). Nothing in this document earns its complexity unless it beats grep on *your* queries by a wide margin. Establish that number in week one.

### The honest caveats

- **There is no multilingual agent-memory benchmark.** Every benchmark reviewed — LoCoMo, LongMemEval, BEAM, MemoryAgentBench, MemoryArena, DMR — is English-only. You have no baselines. You also have no competition.
- **There is no published work on sub-20-character utterance memory.** Zero papers.
- **Nothing is benchmarked past 10M tokens.** Your archive is ~8M. You are at the edge of the published evidence, not inside it.
- **Ukrainian has essentially no retrieval evaluation.** There is no ukMTEB. You will have to build your own held-out set.

### Constraint tension, resolved

You chose *hybrid privacy* and *compute I already own*. Those pull against each other: "hybrid" implies cloud LLM for high-value summarization; "own compute" forbids it. The resolution baked into this design:

- **Backfill is 100% local.** Local embeddings (BGE-M3), local ASR (faster-whisper), local LLM for session enrichment. Zero marginal cost, weeks of wall-clock on idle hardware.
- **Query time may use cloud.** A few hundred calls/month against already-aggregated session summaries — negligible cost, and the unit sent is a summary, not raw intimate messages.
- **Pseudonymization at the boundary**, applied only to the query-time path.

---

## 2. Literature review

### 2.1 Read this before any benchmark number

The published leaderboard for conversational memory is not usable as a purchasing guide.

**LoCoMo — the benchmark ~80% of these systems report — is measurably broken.** An independent audit ([locomo-audit](https://github.com/dial481/locomo-audit), corroborated by [Penfield Labs](https://penfieldlabs.substack.com/p/we-audited-locomo-64-of-the-answer)) establishes:

| Defect | Measurement |
|---|---|
| Wrong golden answers | **99 / 1,540 (6.4%)** → theoretical ceiling **93.57%** |
| Judge leniency (gpt-4o-mini) | Accepts **62.81%** of deliberately wrong-but-topical answers |
| Speaker misattribution | 24+ questions attribute statements to the wrong speaker |
| Category 5 (adversarial) | **446 questions — 22.5% of the dataset — evaluated by no published result** |
| Statistical power | **56%** of adjacent per-category comparisons are indistinguishable at 95% CI |

The judge-leniency figure is the damaging one. The metric nearly every system optimizes rewards *vague answers that name the right topic*. A system that retrieves the right conversation but extracts nothing specific scores well — precisely the failure mode you cannot tolerate over a personal archive.

**Consequence: treat any LoCoMo score above ~90% as an artifact.** EverMemOS reports 95.96% single-hop against a 95.72% category ceiling — mathematically impossible without credit for wrong answers.

**The Mem0 ↔ Zep dispute, resolved.** Mem0's paper benchmarks Zep at 65.99%. Zep's [rebuttal](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/) claims 84%, alleging three misconfigurations. Mem0's co-founder files [an issue](https://github.com/getzep/zep-papers/issues/5) showing Zep counted Category 5 in the numerator but not the denominator, inflating by ~25.56pp. Zep's maintainer acknowledges the arithmetic error and revises to 75.14% ± 0.17. Mem0 re-runs under matched conditions: 58.44% ± 0.20.

**Zep's LoCoMo score has been published as 65.99, 84, 75.14, and 58.44 — a 25.6-point spread on an identical benchmark depending on who ran it.** Both critiques are credible; neither vendor is trustworthy on the other's numbers. This is structural, not malicious: only a system's authors know it well enough to configure it correctly, and all have incentive not to.

### 2.2 The finding that should change your architecture

**On Mem0's own benchmark table, full-context beats Mem0.**

| System | LoCoMo Overall (LLM-Judge) |
|---|---|
| **Full-context baseline** | **72.90** |
| Mem0-graph | 68.44 ± 0.17 |
| Mem0 | 66.88 ± 0.15 |

Zep's rebuttal independently confirms the same pattern. Zep's own DMR table shows full-conversation gpt-4o-mini at 98.0% vs Zep's 98.2% — noise. And Letta scored **74.0% using a filesystem and `grep`**.

The reason is scale: LoCoMo averages ~9K tokens. Everything fits in context. **The benchmark that defines this field operates in the regime where memory architecture is irrelevant.**

### 2.3 BEAM — the corrective, and the number that matters most to you

BEAM ([arXiv:2510.27246](https://arxiv.org/pdf/2510.27246v2), ICLR 2026) is the first benchmark at meaningful scale — 100 conversations, 2,000 validated probes, at 100K / 500K / 1M / **10M** tokens.

| Context size | Long-context baseline accuracy |
|---|---|
| 100K | ~0.28 |
| 1M | ~0.26 |
| **10M** | **~0.13** |

Their LIGHT system delivers **+44–49% relative at 100K but +107–156% relative at 10M**.

**Your archive is ~8M tokens.** (681,331 messages × ~30 chars ≈ 20.4M characters; Cyrillic tokenizes at ~2.5 chars/token for mixed content.) You are at the BEAM-10M frontier, not the LoCoMo frontier. Every leaderboard ranking you might consult was computed in the regime where the measured thing doesn't matter. Mem0's own BEAM numbers decay from 64.1 at 1M to 48.6 at 10M.

Corroborating: **MemoryBench** — "none of the advanced memory-based LLM systems can consistently outperform RAG baselines that simply use all task context." **MemoryArena** — models near-perfect on LoCoMo fall to 40–60% on interdependent multi-session tasks. **AMemGym** — off-policy and on-policy rankings disagree by up to three positions.

### 2.4 Granularity — the convergent evidence

This is the most actionable body of evidence in the review. Four independent studies, different authors, same conclusion.

**SeCom (ICLR 2025, Microsoft Research)** — LoCoMo GPT4Score by memory unit:

| Granularity | Score | Tokens |
|---|---|---|
| Zero history | 24.86 | 0 |
| Full history | 54.15 | 13,330 |
| Summary (SumMem) | 53.87 | 4,108 |
| Summary (RecurSum) | 56.25 | 400 |
| Session-level (BM25) | 63.16 | 3,619 |
| Turn-level (BM25) | 65.58 | 3,657 |
| **Segment-level (SeCom)** | **71.57** | 3,731 |

Their diagnosis of turn-level: individual turns "lack keywords from the current query, resulting in **false negatives**." Of session-level: irrelevant same-session topics "distract the retrieval module." **Summarization is worst of all — worse than doing nothing structural.**

SeCom's second finding: applying **LLMLingua-2 compression at 75%** to segments *improves* retrieval — BM25 recall 77.5% → 92.5%, MPNet 84% → 96%. Removing denoising costs −9.46 GPT4Score. For a corpus as dense in filler as yours, that effect should be *larger*.

**Critique to hold:** SeCom's gain is largely "use GPT-4 as your segmenter." GPT-4-Seg → 71.57; Mistral-7B-Seg → 66.37; **RoBERTa-Seg → 61.84, below the turn-level baseline.** The paper markets lightweight segmentation while its own numbers show it net-negative at that scale.

**LongMemEval** ([arXiv:2410.10813](https://arxiv.org/pdf/2410.10813), ICLR 2025) — the key-expansion ablation:

| Value granularity | Key | Recall@5 | QA acc (GPT-4o) |
|---|---|---|---|
| Session | K = V | 0.706 | 0.670 |
| Session | **K = V + fact** | **0.732** | **0.714** |
| Round | K = V | 0.582 | 0.615 |
| Round | **K = V + fact** | **0.644** | 0.657 |

Verbatim: "expanding the keys with extracted user facts improves both memory recall (**9.4% higher recall@k**) and downstream question answering (**5.4% higher accuracy**)." And critically: "using these condensed forms *alone* does not enhance the memory recall performance."

**Facts must be concatenated with the original text, never replace it.** This holds at both granularities (+0.026 and +0.062 recall) and is the robust finding in that table. *(Skeptical flag: Recall@5 is not comparable across granularities — 5 sessions return more text than 5 rounds, so sessions win by construction. The paper's prose favours rounds while its table favours sessions. Read the takeaway as "between a turn-pair and a session," and treat the exact ordering as unresolved.)*

**LoCoMo's own retrieval-granularity ablation** — observations (LLM-extracted assertions) top-25 = **38.0 F1**; dialog turns top-25 = 35.8; session summaries top-10 = 31.5.

**Convergence:** extracted facts > raw turns > session summaries. Summaries lose information; raw turns lack context; atomic assertions carry both.

### 2.5 Chunking — the skeptical result that does *not* apply to you

"Is Semantic Chunking Worth the Computational Cost?" ([arXiv:2410.13070](https://www.arxiv.org/pdf/2410.13070)) is the famous negative result: semantic chunking "did not consistently justify the additional computational cost."

**But read where it *did* win.** Its only consistent gains were on *stitched* corpora — unrelated documents concatenated:

| Dataset | Fixed-size | Breakpoint (semantic) |
|---|---|---|
| Miracl* (stitched) | 69.45 | **81.89** (+12.4) |
| NQ* (stitched) | 43.79 | **63.93** (+20.1) |
| HotpotQA (natural) | **90.59** | 87.37 |
| MSMARCO (natural) | **93.58** | 92.23 |

**A 7.6-year Telegram log is structurally a stitched corpus.** Message 40,000 and 40,001 may be a work thread and a dinner plan four hours apart. There is no document-level coherence to preserve — the concatenation *is* the artifact. The paper's scope condition (naturally coherent documents) is violated, and its own results predict that time/topic-aware segmentation helps substantially here.

This is the single most important nuance in the chunking literature for your case: **the famous skeptical result about semantic chunking does not transfer to chat.**

Also from that paper, and worth more than its headline: **embedding model quality mattered more than chunking strategy.**

**Chroma's chunking study** ([trychroma.com](https://www.trychroma.com/research/evaluating-chunking)) concludes chunk *size* matters a lot (precision 8.0% at ~103 tokens vs 1.5% at ~660 tokens). *Skeptical read Chroma doesn't state: their winning chunker produced ~103-token chunks while the baseline produced 200-token chunks. Precision is inversely related to chunk size by construction — the headline win is substantially a size confound.* The un-confounded takeaway: **smaller chunks help precision; naive off-the-shelf semantic chunkers (~660 tokens) are worst on every metric.**

### 2.6 Contextual retrieval

Anthropic's [contextual retrieval](https://www.anthropic.com/news/contextual-retrieval) — prepend an LLM-generated situating blurb before embedding. Top-20 retrieval failure rate, baseline 5.7%:

| Method | Failure rate | Reduction |
|---|---|---|
| Contextual embeddings | 3.7% | −35% |
| + Contextual BM25 | 2.9% | **−49%** |
| + Reranking | 1.9% | **−67%** |

Cost: $1.02 per million document tokens with prompt caching.

**The largest single incremental jump is adding BM25 (−35% → −49%), which is free and local.** For your corpus — proper nouns, usernames, URLs, code snippets, Cyrillic morphology — lexical matching is disproportionately valuable.

**My recommendation: use the deterministic header variant.** Prepend `[chat: X] [with: Y] [date: YYYY-MM]` to every session chunk. This captures most of the situating value at zero LLM cost, zero privacy exposure, and — crucially — injects *the metadata your queries will actually contain*. "What did Anna say about the apartment in 2021" matches a header directly.

*Caveats on Anthropic's numbers: internal eval, no public dataset, and the figure is the best configuration averaged across domains.*

**Late chunking** (Jina, [arXiv:2409.04701](https://arxiv.org/pdf/2409.04701)) gives +3.63% relative with no LLM call — attractive, but its documented failure mode is Needle/Passkey: "short relevant information placed into a document of unrelated text." **That failure mode is your corpus.** Do not apply it across the message stream. It becomes defensible only *within* an already-segmented session, at which point there is little context left to add.

### 2.7 Temporal — the cliff

The best-supported claim in this entire review. Four independent benchmarks, different authors, different corpora, same 50–70 point gap.

| Benchmark | Lookup / detail | Ordering / aggregation |
|---|---|---|
| **TCELongBench** (ACL 2024, 88,821 QA pairs) | 82.4–91.9% | **15.3–29.6%** |
| **Test of Time** (ICLR 2025) | 91.94% (`EventAtWhatTime`) | **31.66%** (`Timeline`) |
| **ToT-Arithmetic** | 86.20% (timezone) | **15.40%** (duration) |
| **ChronoQA** (5,176 QA pairs) | 0.7064 single-doc R@5 | **0.2693 multi-doc** |

Test of Time's 60-point gap is on the *same facts, same models, same context*. TCELongBench found RAG at parity with a 128K-context model on detail (84.0% vs 82.4–91.9%) and at 15–19% on ordering. **Throwing more context at ordering does not fix it. Neither does better retrieval.**

Two further results you must not miss:

**TempRAGEval** ([EMNLP 2025 Findings](https://aclanthology.org/2025.findings-emnlp.167.pdf)): under temporal perturbation, top-1 answer recall fell **85.8% → 54.7%** and evidence recall **45.0% → 20.3%**. Conclusion: existing systems rely on **surface-level date string matching, not temporal reasoning.** Dense retrievers "handle" dates by lexical coincidence.

**ChronoQA**: the naive "Temporal Filter" baseline scored **below** native RAG — 0.4903 vs 0.5458 R@5. Bolting a date-range metadata filter onto vector search **degraded recall by 10–16% relative.** Meanwhile **query decomposition was the biggest win: +68% relative on multi-document questions** (0.2693 → 0.4518).

*Test of Time also demonstrated why real-world temporal benchmarks overstate ability: Gemini answered a Chelsea FC coaching question correctly but failed when entity names were anonymized — the "temporal reasoning" was parametric recall.*

**Design implication, stated plainly:** treat the timestamp as a first-class relational column with a B-tree index and SQL predicates, not as a vector-adjacent metadata blob. Every operation on the right-hand column above is a database operation — `ORDER BY`, `MIN`, `DATE_DIFF`, `GROUP BY` — not a retrieval operation.

### 2.8 Recency decay — do not apply it

Solr's canonical recency idiom is `recip(ms(NOW, date), 3.16e-11, 1, 1)`, which collapses to `1/(age_years + 1)`. On your span:

| Message age | Boost |
|---|---|
| today | 1.00 |
| 2 years | 0.33 |
| **7.6 years (your oldest)** | **0.116** |

**An 8.6× multiplicative penalty on 2018 content.** Elastic's own documentation carries a worked example where decay functions caused a video with 100 views to outrank content with thousands — aggressive function scoring **swamps textual relevance**.

Your interesting queries are archaeological, not recency-seeking. **Default recency decay OFF.** Gate it behind intent detection and apply gauss decay (`scale≈30d, offset≈7d`) only when a query carries no temporal anchor.

*Operational trap worth knowing: Vespa's `age()` defaults to 10B seconds (~317 years) for missing timestamps, silently zeroing freshness. Across 7.6 years and 487 chats, timestamp normalization bugs are near-certain; this default hides them.*

### 2.9 Agentic retrieval — the restraint evidence

Three independent lines converge, and they argue for less machinery, not more.

**"Dissecting Agentic RAG"** ([arXiv:2606.21553](https://arxiv.org/html/2606.21553)) — component ablation with a **local 7B model**, 5,000 HotpotQA questions. Closest published setup to a self-hosted Hetzner box.

| Change | Δ EM | Significance |
|---|---|---|
| Iterations 5 → 2 | **−0.3** | negligible |
| Iterations 5 → 1 | −7.1 | large |
| Remove query decomposition | −1.4 | p = 0.004 |
| Remove cross-encoder reranking | −1.7 | p < 0.001 |
| Dense-only vs adaptive routing | 53.0 vs 53.2 | **−0.2** |

**Headline: fixed hybrid retrieval (RRF) outperformed the full adaptive pipeline by +1.8 EM and +1.9 F1 (p < 0.001).** The adaptive router "fired on named entities in 79.2% of cases, routing most queries to BM25 and losing complementary dense signals." Latency: baseline 546 ms, full agentic **5,642 ms (10.3×)**.

Authors' verbatim conclusion: *"Most of the gain comes from running a short retrieval loop, not from adaptive routing or from many iterations."*

**Adaptive-RAG's query-complexity classifier scores 54.52% on a 3-way task** — barely above a majority-class baseline. **Do not train a query router.**

**"Beyond Static Retrieval"** ([arXiv:2509.25530](https://arxiv.org/html/2509.25530)) independently: "excessive rounds produce diminishing returns; **two iterations often suffice**." Iteration helps on multi-hop bridge questions, hurts on single-hop and comparison questions.

**What actually pays, in descending cost-effectiveness:**

| Technique | Gain | Cost | Verdict |
|---|---|---|---|
| Hybrid BM25 + dense, RRF | **+11.9 EM** over naive (43.1→55.0) | ~2× retrieval | **First** |
| Cross-encoder rerank | +1.7 EM; failure 2.9%→1.9% | negligible latency | **Second** |
| Contextual enrichment | failure 5.7%→2.9% (−49%) | one-time | **Third** |
| Short loop (2 steps) | most of the agentic gain | 2–5× latency | **Fourth, capped** |
| Query decomposition | +1.4 EM; **+68% rel. multi-doc** | ~2× latency | **Analytical queries only** |
| Deep loops (5+) | −0.3 EM | 10× latency | **Don't** |
| Learned adaptive routing | +0.2 EM, sometimes −1.8 | training + latency | **Don't** |

**Two systemic biases to hold.** First, every paper compares against a naive baseline of its own construction — weaker than "hybrid BM25+dense with RRF and a cross-encoder," which is the actual production alternative. Second, **cost reporting is near-universally absent in exactly the papers whose entire proposition is spending more compute.** Search-R1, R1-Searcher, IRCoT, TimeR4, DyG-RAG and TA-RAG report no latency, no call counts, no dollar figures. The three papers that *do* report cost — Adaptive-RAG, CRAG, Dissecting Agentic RAG — are the three arguing most strongly for restraint. That correlation is not a coincidence.

### 2.10 Conflict resolution — the cheapest large win available to you

**"Don't Ask the LLM to Track Freshness: A Deterministic Recipe for Memory Conflict Resolution"** ([arXiv:2606.01435](https://arxiv.org/html/2606.01435v1)). Small paper, sharp result, highest-confidence engineering recommendation in this document.

Two named failure modes of LLM-as-conflict-resolver:
- **Prior-override** — with strong training priors, the LLM ignores an explicit "newer wins" instruction and emits the stale fact.
- **Serial-comparison drift** — as candidate pools grow, the LLM loses track of which version is latest.

| Approach | Accuracy |
|---|---|
| LLM-judgment baseline @64K | 75% |
| LLM-judgment baseline @262K | **61%** (−14 pts) |
| Deterministic pipeline, gpt-4o-mini | **78.0%** (+21pp at longest context) |
| Deterministic pipeline, gpt-4o | **94.8%** |

Published FactConsolidation single-hop baselines: **HippoRAG-2 54%, BM25 48%, Zep/Graphiti 7%.**

The pipeline is trivially simple: BM25 retrieve → LLM extracts *matching candidates only, with no version comparison* → **Python `max(serial)`**. Cost ~$0.0001/query.

Crucially, they isolate the cause: restricting to questions where retrieval *succeeded*, baseline accuracy still fell 78% → 66%. **The failure is post-retrieval reasoning under scale, not retrieval.**

**Prescription: put monotonic timestamps on facts and resolve with `max()` in code, never in a prompt.**

### 2.11 Episodic memory — the theoretical frame

Pink et al., "Episodic Memory is the Missing Piece for Long-Term LLM Agents" ([arXiv:2502.06975](https://arxiv.org/abs/2502.06975), MPI-SWS / Intel Labs / UT Austin) defines five properties: long-term storage, explicit reasoning *about* stored information, single-shot learning, **instance-specific memories**, and **contextual relations** binding *when, where and why*.

Their Table 2 scores every existing approach and **nothing satisfies all five.** RAG fails property 5 outright — it stores text "typically without much metadata or contextual detail." GraphRAG "encode[s] a limited number of relationship types."

**The core critique, which applies directly to Mem0-style designs:** the field builds *semantic* memory (deduplicated facts) and calls it episodic. A store containing `user_prefers_tea` has thrown away the episode — who said it, when, in what conversation, in response to what, in which language, with what tone. **For a personal archive, that context *is* the value.** This is the strongest theoretical argument for an episode-subgraph design over a fact-extraction design.

Their open research question RQ2 — "how to segment continuous input into discrete episodes, and when to store them" — is exactly your 92%-without-`reply_to_id` problem, and it is named as unsolved.

### 2.12 Forgetting and decay

**Generative Agents** (Stanford, [arXiv:2304.03442](https://arxiv.org/abs/2304.03442)) — the formula everyone cites and few read carefully:

```
score = α_recency·recency + α_importance·importance + α_relevance·relevance
```

- **All three α are set to 1.** No tuning, no ablation. The famous formula is an unweighted sum.
- **Recency decays at 0.995 per sandbox hour since the memory was *last retrieved*** — not since creation. Retrieval acts as reinforcement, an implicit spaced-repetition effect. Most-missed detail, most worth stealing.
- Importance is LLM-scored 1–10 at creation ("1 is brushing teeth, 10 is a break-up").
- **All three are min-max scaled over the candidate set** — scores are pool-relative and not comparable across queries. Behaviour changes as the store grows. Fragile and under-discussed.
- Reflection triggers when summed importance of recent events exceeds **150** — an *absolute* threshold. A firehose of 20-char messages triggers reflection constantly. You must rescale to message volume or trigger on segments.

**FadeMem** ([arXiv:2601.18642](https://arxiv.org/html/2601.18642)) generalizes to a stretched exponential with importance-modulated rate. Two ideas worth stealing: **β ≠ 1** (real forgetting is not a clean exponential) and the **diminishing-returns consolidation term** `exp(−n_i/N)`, which prevents a frequently-accessed memory from becoming permanently un-forgettable. Reported 55% storage reduction at 82.1% critical-fact retention.

**FSFM** ([arXiv:2604.20300](https://arxiv.org/html/2604.20300v1)) is the only forgetting work with real production data — China Mobile's assistant, **3.36M records**. Score = `0.4·quality + 0.3·value + 0.2·temporal + 0.1·(−risk)` under a knapsack constraint. 30% storage cut, 70.4% important-data retention.

**Honest assessment: every decay formula in the literature uses hand-picked constants with no ablation against a learned retention model.** MemoryBank validated over 10 days. **Nobody has fit a decay curve to real multi-year data. If you fit one to 7.6 years of your own messages, you would be ahead of the literature.**

### 2.13 Deletion is harder than it looks

**"Agentic Unlearning"** ([arXiv:2602.17692](https://arxiv.org/html/2602.17692v1)) names **"backflow"**: deleting a memory from the store doesn't delete it, because the model regenerates it parametrically; deleting from parameters doesn't help, because retrieval reintroduces it. Their method uses dependency-aware deletion via blocklists and reference counting. Vector index reconstruction is O(N·d).

**For a privacy-first archive this is the essential lesson:** if summaries, reflections and graph edges are built on top of raw episodes, real deletion requires a dependency graph, or you leave shards behind. **Reference-count derived artifacts from day one. Retrofitting is very expensive.**

**SSGM** ([arXiv:2603.11768](https://arxiv.org/html/2603.11768v1)) — framework paper with no empirical results, but it contributes the best risk taxonomy (semantic drift, memory hallucination, temporal obsolescence, index bloat, memory poisoning) and the most defensible architectural recommendation in the 2026 literature: **dual-track storage — an append-only immutable episodic log plus a mutable derived layer** — so drift is always reversible.

### 2.14 What every benchmark fails to measure

1. **Scale past 10M tokens.** BEAM's 10M is one linear conversation, not 487 separately-indexed chats requiring cross-chat entity resolution.
2. **Any language but English.** Confirmed across all reviewed benchmarks. **No multilingual agent-memory benchmark exists as of July 2026.**
3. **Forgetting.** Only MemoryAgentBench tests it explicitly.
4. **Contradiction resolution at scale.** BEAM's authors call it "a challenging open problem."
5. **Ingestion cost.** No benchmark reports hours or dollars to build the memory.
6. **Very short utterances.** LoCoMo averages ~30 tokens/turn. Yours are ~5–10. Nothing tests this regime.
7. **Deletion.** No benchmark tests whether deleting actually removes.
8. **Variance.** Only Mem0 consistently reports multi-seed results. Everything else cited here is single-run.

---

## 3. Product landscape

**Confidence note, stated plainly:** the research budget for this session was exhausted before I could verify current pricing and feature sets by search. Everything in this section is from training data current to ~May 2026 and **pricing especially should be re-checked before you act on it.** Architecture descriptions are more stable than prices. I have marked low-confidence items.

### 3.1 The direct analogues

| Product | Architecture (where known) | Strengths | Weaknesses | Pricing *(verify)* | Steal |
|---|---|---|---|---|---|
| **Rewind / Limitless** | Continuous screen + audio capture, local-first index, on-device ASR, cloud LLM for QA. Pivoted from Mac screen-recording to the Limitless Pendant wearable. | The only product that genuinely solved *capture*. Local-first storage was a real differentiator. Timeline UI is excellent. | Capture ≠ understanding. Retrieval was largely keyword + recency. The pivot to hardware effectively abandoned the desktop archive product. | ~$20–30/mo; Pendant ~$399 | **The timeline-as-primary-UI.** For an archive, chronology is the natural navigation axis, not a search box. |
| **Personal.ai** | Per-user "Memory Stack," fine-tuned/adapted personal language model, "Memory Blocks" with attribution scores. | Genuinely committed to the personal-model thesis. Attribution UI showing which memories produced an answer. | The personal-model premise is expensive and mostly unnecessary — retrieval over your data gets ~all the value. Slow to load. Unclear moat vs RAG. | ~$40/mo | **Attribution-first UI.** Every answer shows its source messages. Non-negotiable for a memory system you must trust. |
| **Mem (mem.ai)** | Vector search over notes, "Mem X" auto-organization, LLM-generated collections. | Auto-tagging that actually worked. Smart Search was ahead of its time. | Notes, not conversations. No temporal reasoning. Repeated strategy pivots. | ~$10–15/mo | **Automatic collection formation** — cluster related items without asking the user to file them. |
| **Khoj** | Open-source. Postgres + pgvector, hybrid search, multi-modal ingestion (Markdown, PDF, GitHub, Notion), local or cloud LLM, Obsidian/Emacs plugins. | **The closest OSS analogue to what you want.** Self-hostable, AGPL, actively maintained, sane architecture. | Document-centric, not conversation-centric. No session segmentation, no temporal reasoning, no bi-temporal facts. | Free self-hosted; cloud ~$10/mo | **Its exact stack choice: Postgres + pgvector, no separate vector DB.** Validates §5's recommendation. Also its multi-client surface (Obsidian, Emacs, web, Telegram bot). |
| **Reflect / Tana / Capacities** | Networked-note apps with LLM layers bolted on; backlink graphs as the primary structure. | Backlinks are a genuinely good manual knowledge structure. | Require manual curation — the opposite of your problem, which is 681k messages nobody will ever curate. | ~$10–20/mo | **Bidirectional links surfaced at read time**, not just write time. |
| **Monica / Clay / Dex** (personal CRM) | Contact-centric relational DB, interaction logs, reminder engine. Clay adds automatic enrichment from email/calendar. | The **person-as-primary-entity** model is right and underused in AI memory systems. Relationship-maintenance nudges are a real feature. | No semantic understanding of conversation content. Manual entry burden (Monica) or shallow enrichment (Clay). | Monica free/self-host; Clay ~$10–20/mo | **The per-person dossier as a first-class object**, auto-maintained. With 457 senders you should have 457 auto-generated dossiers. |
| **Day One / Journaling apps** | Encrypted entry store, on-this-day resurfacing, media attachment. | **"On this day" is the single highest-value memory feature ever shipped** — near-zero engineering, enormous felt value. | No cross-entry reasoning. | ~$3–5/mo | **Memory resurfacing on a schedule.** Cheap to build, disproportionately valuable. |
| **Dot (New Computer)** | Long-term memory assistant, proactive surfacing. Shut down mid-2025. | Best-in-class demonstration of proactive memory. | Died. The consumer market for a memory assistant is unproven — worth noting before you over-invest in polish. | n/a | **Proactive surfacing without a query.** Also a cautionary tale about scope. |
| **Granola / Fireflies / Otter** (conversation intelligence) | ASR + diarization + LLM summarization + action-item extraction, per-meeting. | **Action-item extraction is the killer feature** and it maps directly onto "find forgotten commitments." | Per-meeting scope; no longitudinal reasoning across years. | $10–30/mo | **Commitment extraction as a first-class output**, with a status lifecycle (open / done / abandoned). |

### 3.2 What nobody has built

The gap is specific and it is where your project's value lives:

1. **Longitudinal reasoning over informal chat.** Every conversation-intelligence product is per-meeting. Every memory product is per-note. Nobody does "how did my thinking about X change over seven years" over IM.
2. **Bi-temporal personal facts.** No consumer product distinguishes "when this was true" from "when I learned it." Every one of them treats your profile as a snapshot.
3. **Multilingual / code-switched personal memory.** Not a single product handles RU/UK/EN mixing. This is a genuine unmet need for ~300M people.
4. **Contradiction and abandonment detection.** No product tells you "you said you'd do X and never did," or "your 2021 position on Y contradicts your 2024 position." These are the highest-value questions a personal archive can answer and nobody answers them.

### 3.3 Ideas worth stealing, ranked

1. **Attribution on every answer** (Personal.ai) — non-negotiable.
2. **Timeline as primary navigation** (Rewind) — chronology beats a search box for archives.
3. **Auto-maintained person dossiers** (Clay/Monica) — 457 of them, free.
4. **On-this-day resurfacing** (Day One) — highest value per line of code in this entire document.
5. **Commitment extraction with lifecycle** (Granola) — turns an archive into an accountability system.
6. **Postgres + pgvector, no separate vector DB** (Khoj) — independent validation of §5.

---

## 4. Open-source comparison

Ranked by what you should actually adopt or read. Star counts and dates are as of the research pass (July 2026); treat as approximate.

### Tier 1 — read the code, steal the design, don't necessarily adopt

| Rank | Project | License / ★ | Maturity | Architecture | Adopt? | Steal |
|---|---|---|---|---|---|---|
| **1** | **Graphiti** (getzep) | Apache-2.0 / 29k | High. v0.29.2, 2026-06-08. Neo4j, FalkorDB, Neptune backends. Works with Ollama/vLLM. | Three-tier subgraph: immutable **episode** layer → **semantic entity** layer → **community** layer. Bi-temporal edges. Search = cosine + BM25 + BFS, composed with RRF / MMR / episode-mentions / node-distance rerankers. | **Design yes, code no.** Needs Neo4j and an LLM call per entity resolution. | **The bi-temporal model, verbatim. Invalidate-never-delete. The episode subgraph as non-lossy ground truth. Episode-mentions and node-distance rerankers** — cheap, non-LLM, ideal for a 457-sender social graph. |
| **2** | **HippoRAG 2** (OSU-NLP) | MIT / 3.6k | High. **Last commit 2026-07-24.** The only project whose paper, code and maintenance all line up. | Schema-free OpenIE triples → phrase nodes; synonym edges at cosine > 0.8; passage nodes via context edges. Query → triple match → LLM recognition filter → **Personalized PageRank** (damping 0.5). | **Consider for V2.** MIT, self-hostable, 9.9 GB GPU, 1.2s/query. | **PPR over a phrase+passage graph.** Scored **54% on FactConsolidation vs Zep's 7%** — best conflict handling of any graph system. |
| **3** | **Letta** | Apache-2.0 / 23.8k | Medium — **mid-migration**; the main repo now describes itself as "legacy Letta server." | Four tiers: message buffer → core memory (editable **blocks** with hard size limits, compiled via Jinja at inference) → recall memory → archival memory. Blocks shareable across agents. | **No** — agent framework first, memory second. Heavier than you need. | **Memory blocks with hard character limits.** **Sleep-time compute** (5× test-time compute reduction, +13–18% accuracy). And **the grep baseline you must beat.** |
| **4** | **Cognee** | Apache-2.0 / 26.6k | High. **Last commit 2026-07-25.** | ECL pipeline: `add` → `cognify` (LLM entity/relation extraction into KG + vector store) → `memify`. Pluggable Neo4j/Kuzu + Qdrant/LanceDB. | **Best off-the-shelf option if you want to buy rather than build.** | Strongest published BEAM-10M number (0.67). *But: their HotpotQA eval is n=24 against a 7,405-question dev set — 95% CI ≈ ±0.17, wider than every gap claimed. Their EM 0.04→0.687 CoT ablation is an answer-formatting artifact, not a capability gain.* |
| **5** | **Khoj** | AGPL-3.0 / ~30k | High, actively maintained. | **Postgres + pgvector**, hybrid search, multi-client (Obsidian, Emacs, web, WhatsApp/Telegram bots), local or cloud LLM. | **Closest thing to a reference implementation for your deployment shape.** | Its stack choice validates §5. Its multi-client surface is the right delivery model — you want this in Telegram, not a web app. |

### Tier 2 — useful ideas, do not adopt wholesale

| Project | License / ★ | Verdict |
|---|---|---|
| **Mem0** | Apache-2.0 / 60.7k | Most popular in the space. **Steal the `ADD/UPDATE/DELETE/NOOP` tool-call vocabulary over a top-*s* candidate set** — right primitive, genuinely cheap. **Do not adopt for its numbers:** the README states the headline scores "reflect Mem0's managed platform, which includes proprietary optimizations not available in the open-source SDK." Self-hosted performance is undisclosed. Per-message-pair extraction means **340k+ LLM calls** at your scale. |
| **MemOS** (MemTensor) | Apache-2.0 / 9.5k | **Activation memory (KV-cache injection) gives up to 94.2% TTFT reduction** — genuinely interesting and orthogonal to everything else. But MemOS **loses F1 to Memobase (45.27 vs 50.18) while winning LLM-Judge** — the signature of verbose answers exploiting a lenient judge. |
| **A-MEM** | MIT / 1.0k | **Steal the note schema** (keywords + tags + contextual description embedded *alongside* content) — it manufactures retrievable surface area that a 20-char message lacks. **Do not steal the evolution mechanism:** 15.00 hours of offline construction at LoCoMo scale — super-linear complexity that does not terminate at 681k. Also −19 points on adversarial vs plain RAG. |
| **RAPTOR** | MIT | Recursive clustering + summarization tree. Conceptually attractive for hierarchy. In practice you get most of the benefit from **time-partitioned map-reduce**, which is free — see §10. |
| **Microsoft GraphRAG / LightRAG / nano-graphrag** | MIT | Diagnosis right, cure wrong. See §9. LightRAG and nano-graphrag are the cheap variants worth reading if you insist on graph. |
| **txtai / R2R / Morphik / Onyx** | Apache-2.0 | Competent general RAG frameworks. None handle conversation segmentation, temporal reasoning or bi-temporal facts — the three things that are actually hard here. |

### Tier 3 — do not use

| Project | Why |
|---|---|
| **EverMemOS** | LoCoMo single-hop 95.96 **exceeds the 95.72 category ceiling**. Advertised 2,298 tokens/question contradicts **their own Table 8 (6,669)** — a 2.9× misstatement. A third-party reproduction got **38.38%**; the maintainer replied "everything works as expected" with no technical explanation. |
| **MemoryOS** | **32.372 s/turn user latency.** The survey calls strict hierarchical paging "impractical for interactive settings." README publishes only relative gains with no absolute numbers. |
| **Memory-R1** | **Repo contains no code** — "🚧 Status: Code coming soon," 11 months after the paper. |
| **MemoryBank** | Last commit 2023-05-24. Validated over 10 days. Cite as prior art for the Ebbinghaus curve; do not deploy. |
| **Telegram-specific analyzers** (tg-archive, telegram-export visualizers, various "chat wrapped" tools) | Uniformly shallow — message counts, word clouds, hour-of-day histograms. **You already have all of this in your `v_*` views.** None do retrieval or reasoning. Nothing to adopt. |

### The maintenance signal

Only three projects in this survey have their paper, their code, and their commit history in alignment: **HippoRAG 2** (2026-07-24), **Cognee** (2026-07-25), and **Graphiti** (2026-06-08). Weight that heavily. The field has a serious problem with papers whose repos are empty and READMEs whose numbers don't come from the shipped code.

---

## 5. Architecture comparison — and the case against your current stack

### 5.1 What you have now

From probing your live system: PostgreSQL with `v_messages` / `v_chat_stats` / `v_monthly` / `v_tagged` analytical views, plus Qdrant embedding **every message in personal 1:1 chats**, exposed through an MCP server with `telegram_query` (SQL), `telegram_search_semantic` (vector), `telegram_get_messages`, and a tagging system.

**This is a competent first implementation and the relational spine is genuinely good.** The views are the right idea, `telegram_query` over read-only views is exactly the right escape hatch, and the tagging system is a real asset. The critique below is about the indexing layer, not the whole system.

### 5.2 Critique 1 — the indexing unit (severity: fatal)

**442,954 of your vectors represent strings under 20 characters.** This is not a tuning problem, it is a category error.

The mechanism: a dense embedding of `ок` occupies a dense, high-population region of embedding space shared with tens of thousands of near-identical tokens. Any query whose embedding lands near that region retrieves an arbitrary sample of them. Meanwhile the messages that *do* carry propositions — the 10,484 over 200 characters — are outnumbered 42:1 and get buried.

SeCom quantified the general effect at 30 tokens/turn: turn-level 65.58 vs segment-level 71.57. **At 5–10 tokens the gap is larger, and the diagnosis is explicit** — individual turns "lack keywords from the current query, resulting in false negatives."

**Fix:** aggregate into time-gap sessions. 681,331 → **~50,000–70,000** units. Index size drops from ~2.8 GB (fp32 d=1024) to ~140–280 MB. Retrieval quality goes up while the index shrinks 10×.

### 5.3 Critique 2 — Qdrant is solving a problem you don't have (severity: moderate)

Three specific arguments, and one honest counter-argument.

**(a) ANN is unnecessary at your scale.** After aggregation you have ~60k vectors for one user. Exact brute-force scan over 60k × 1024-dim int8 (~60 MB) is *microseconds* of memory-bandwidth-bound work. Even at the raw 681k message level, ~700 MB scans in a few hundred milliseconds. You are paying HNSW's complexity, memory overhead and index-maintenance cost to accelerate something that was already fast.

**(b) Filtered ANN fails precisely where you need it most.** [Qdrant's own filterable-HNSW analysis](https://qdrant.tech/articles/filterable-hnsw/) explains the mechanism via percolation theory: for a random graph of average degree ⟨k⟩, there is a critical threshold **p_c = 1/⟨k⟩** below which the graph fragments into disconnected components and greedy search fails. Their experiments confirm "a clear threshold when the search begins to fail."

A filter like `ts BETWEEN '2019-03-01' AND '2019-03-31'` selects ~7,600 of 681,331 messages — **1.1% cardinality. Deep in the fragmentation regime.** And ChronoQA measured this empirically from the other direction: the naive temporal-filter baseline scored **0.4903 R@5 vs native RAG's 0.5458** — filtering *reduced* recall.

Qdrant mitigates with payload indexes and a full-scan threshold (default 10 KB), and it special-cases numeric ranges into bucketed structures. But you have to know to configure this, and **Qdrant's own docs warn that without a payload index it "will not be able to estimate cardinality... causing extremely slow search times or low accuracy results."** Silent recall loss is the worst failure mode a personal archive can have — you never learn what you didn't find.

**(c) Two systems is one too many.** Your relational data and your vectors describe the same rows. Split across two stores you get: no transactional consistency between them, no joins between vector results and relational metadata without an application-layer round-trip, two backup regimes, two upgrade paths, and a permanent reindex-drift risk. On a single-user homelab that's pure overhead.

**The honest counter-argument, which I'll state fully:** Qdrant's Query API with `prefetch` + fusion (RRF/DBSF) is genuinely good, its sparse-vector and IDF/BM25 support is first-class, and it is already working in your stack. Migration is not free, and "it works" has real value. **If you were not about to rebuild the indexing pipeline anyway, I would tell you to keep it.** But you *are* — the unit of indexing has to change, which means re-embedding everything regardless. That is the cheapest moment this migration will ever be.

**One thing Qdrant is genuinely bad at for you:** multivectors. Qdrant's docs state plainly, *"HNSW graphs don't work with MaxSim either way, so it should be disabled."* In Qdrant, multivectors are a reranking primitive, not a searchable index. If you ever want late interaction, you get brute force or a mandatory two-stage pipeline where the dense stage does all the real work.

### 5.4 Critique 3 — the memory hierarchy you proposed (severity: significant)

Your proposed hierarchy:

```
Raw Messages → Conversation → Daily → Weekly → Monthly → Life Events
→ Long-term Knowledge → Identity → Values → Preferences → Personality → Behavioral Patterns
```

**Three problems.**

**(a) It is a lossy compression chain.** Each level is derived by summarization from the level below. Every summarization pass discards detail, and the loss compounds. Over 7.6 years and repeated consolidation passes this is the documented failure mode: *"after enough passes, the agent 'remembers' a sanitized, generic version of history."* Letta's recursive summarization is the named source.

And the empirical result is unambiguous: **summarization is the worst-performing retrieval granularity in the literature** — 53.87–56.25 vs 71.57 for segments (SeCom), 31.5 vs 38.0 for observations (LoCoMo).

**(b) Daily / weekly / monthly are the wrong axis.** They are calendar buckets, not semantic ones. A three-week obsession doesn't align to a week boundary. A conversation that changed your mind doesn't align to a day. You'd be summarizing across topic boundaries and within them arbitrarily. **Episodes and topic-threads are the natural units; calendar rollups are a *view*, computable on demand from the episodic layer, not a stored layer.**

**(c) Identity / Values / Personality are not memory.** They are *inferences* about you. Storing them as a distilled layer means: they go stale silently, they cannot be audited, they cannot cite evidence, and — worst — they become self-reinforcing. A stored belief that "Yehor is risk-averse" will bias every future retrieval that touches it. **These should be computed on demand from evidence, versioned, and always accompanied by their citations.**

**The replacement — a star schema, not a pyramid:**

```
                       ┌─────────────────────────┐
                       │  IMMUTABLE EPISODIC LOG │   ← append-only, never rewritten
                       │  messages + sessions    │      the only source of truth
                       └───────────┬─────────────┘
                                   │  (every projection references specific
                                   │   message_ids and is reference-counted)
        ┌──────────────┬───────────┼───────────┬──────────────┐
        ▼              ▼           ▼           ▼              ▼
   ┌─────────┐  ┌───────────┐ ┌────────┐ ┌──────────┐ ┌─────────────┐
   │  FACTS  │  │  ENTITIES │ │ THREADS│ │COMMITMENTS│ │  PROFILES   │
   │bi-temporal│ │ people,   │ │ topic  │ │ open/done│ │ per-person, │
   │t_valid /  │ │ projects, │ │ arcs   │ │/abandoned│ │ self, recomp-│
   │t_invalid  │ │ places    │ │ over   │ │          │ │ uted, cited │
   └─────────┘  └───────────┘ │ time   │ └──────────┘ └─────────────┘
                              └────────┘
   ── all projections are DERIVED, VERSIONED, DROPPABLE and REBUILDABLE ──
   ── calendar rollups (daily/weekly/monthly) are SQL VIEWS, not tables ──
```

Properties this buys you that the pyramid doesn't:

- **Reprocessability.** You will change your extraction schema repeatedly. Drop a projection, rebuild it. The pyramid can't do this — level N+1 was built from level N's summary, and the detail is gone.
- **Drift correction.** Every projection cites message IDs. Drift is detectable and reversible.
- **Dependency-aware deletion.** Reference counting means deleting a message provably removes its derived shards. This is the "backflow" problem, and it is very expensive to retrofit.
- **Auditability.** "Why do you think I care about X?" always has an answer with citations.

### 5.5 Critique 4 — retrieval strategy (severity: significant)

Your `telegram_search_semantic` is single-stage dense-only. The evidence says this is the weakest configuration available:

| Configuration | Evidence |
|---|---|
| Dense only (yours) | baseline |
| + BM25, RRF fusion | **+11.9 EM** over naive baseline (43.1→55.0); Anthropic: failure 3.7%→2.9% |
| + cross-encoder rerank | +1.7 EM; failure 2.9%→1.9% (**−67% cumulative**) |
| + RU/UK lemmatization | RusBEIR: "lemmatization using PyMorphy3 proves critical for lexical performance" — **BM25 beats BGE-M3 by 13pp** on some Russian tasks |

**For your corpus lexical matching is disproportionately valuable** — usernames, proper nouns, project names, URLs, code, slang, and the bare-token first mentions that answer "when did I first hear about X." A dense embedding of a 15-character message is close to noise; BM25 over lemmatized text is not.

**And the missing piece entirely: no SQL path for temporal and aggregate questions.** Right now "when did I first mention X" goes through semantic search, which structurally cannot answer it — top-k returns the *k most similar*, and similarity is uncorrelated with recency. There is no k at which "earliest" becomes reachable.

### 5.6 Architecture options compared

| Option | Complexity | Retrieval quality | Ops burden | Verdict |
|---|---|---|---|---|
| **A. Status quo** — per-message vectors in Qdrant, dense-only | Low | Poor — 65% of index is noise | 2 systems | **Reject.** Wrong indexing unit. |
| **B. Status quo + hybrid + rerank** | Low-med | Fair — fixes retrieval, not the unit | 2 systems | Tempting, cheap, but treats the symptom. The unit is still wrong. |
| **C. Session aggregation + hybrid + rerank, keep Qdrant** | Medium | **Good** | 2 systems | **The pragmatic choice if migration cost is unacceptable.** Gets ~90% of the value. |
| **D. Session aggregation + hybrid + rerank + bi-temporal facts, all in Postgres** | Medium-high | **Very good** | **1 system** | **Recommended.** See §6. |
| **E. D + knowledge graph (Neo4j/Graphiti)** | High | Marginally better on multi-hop | 3 systems | **Reject** for V1. See §9. |
| **F. Adopt Cognee or Graphiti wholesale** | Low to build, high to control | Unknown on RU/UK | 2–3 systems | Reasonable if you'd rather buy than build. You lose the ability to fix multilingual failures. |

**If you take one thing from this section: C is 90% of D at 30% of the effort. D is right because you're rebuilding anyway.**

---

## 6. Recommended architecture

### 6.1 The whole system

```
┌──────────────────────────────────────────────────────────────────────────┐
│  INGESTION                                                                │
│  Telethon sync ──► raw_message (append-only, immutable)                   │
│      │                                                                     │
│      ├─► voice/video_note ──► faster-whisper large-v3-turbo ──► transcript │
│      ├─► photo ──────────────► PaddleOCR (Cyrillic) ──────────► ocr_text   │
│      │                        └► Qwen2.5-VL caption (optional) ► caption   │
│      └─► document ───────────► text extract ─────────────────► doc_text    │
└──────────────────────────┬───────────────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────────────┐
│  SEGMENTATION  (the single highest-value stage)                           │
│  per-chat adaptive time-gap threshold, fitted from the gap distribution   │
│  + sender-switch signal + the 7.9% reply edges as anchors                 │
│  + hard caps: ≤30 messages, ≤250 tokens; merge singletons into neighbours │
│  681,331 messages ──────────────────────► ~50,000–70,000 sessions         │
└──────────────────────────┬───────────────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────────────┐
│  ENRICHMENT  (local LLM, batch, overnight, resumable)                     │
│  per session:  summary · topics · entities · facts · commitments          │
│                sentiment · language mix · importance score                │
│  ALL outputs carry source message_ids and a refcount                      │
└──────────────────────────┬───────────────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────────────┐
│  INDEXING  — single PostgreSQL instance                                   │
│                                                                            │
│  embed_text = "[chat:X][with:Y][2021-03] " + session_text + " || " + facts │
│                                                                            │
│  ├─ pgvector halfvec(1024)  BGE-M3 dense      ← exact scan, no ANN index   │
│  ├─ tsvector (russian+simple) + pg_trgm       ← BM25-ish lexical           │
│  ├─ B-tree (ts), (chat_id, ts), (sender_id)   ← temporal + aggregate       │
│  └─ GIN on entity/topic/label arrays          ← faceted filtering          │
└──────────────────────────┬───────────────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────────────┐
│  QUERY  — deterministic intent routing (NOT a learned router)             │
│                                                                            │
│  "how many / сколько"      ──► SQL aggregate, show the generated SQL      │
│  "first / впервые / when"  ──► unbounded match set → ORDER BY ts → verify  │
│  "evolve / как менялось"   ──► time-stratified map-reduce over 30 bins    │
│  explicit date range       ──► SQL predicate FIRST, then rank within      │
│  everything else           ──► hybrid dense+BM25 → RRF → rerank → answer   │
│                                                                            │
│  loop cap: 2 iterations.  Every answer cites message_ids.                 │
└──────────────────────────────────────────────────────────────────────────┘
```

### 6.2 Why each choice, and where it fails

| Choice | Why it is superior | Trade-off it introduces | Where it fails |
|---|---|---|---|
| **Session segmentation by adaptive time gap** | Free, deterministic, 100% reliable as a *boundary* signal. Converts 681k noise units into ~60k signal units. SeCom: +6 pts at 30 tok/turn, larger below. | Loses the ability to retrieve a single message directly — mitigated by keeping the message table fully indexed and searchable in parallel. | Long continuous conversations with multiple topics and no pauses. Also chats where you and the other person message at wildly different rhythms. Mitigate with the ≤30-message cap. |
| **Fit the gap threshold per chat, not globally** | Your gap distribution is certainly bimodal (within-burst seconds→minutes vs between-burst hours→days) and the valley differs per relationship. **Nobody has published a principled threshold for personal IM** — the 30-min web convention and MSC's 1–7h bracket the range. Measuring your own data beats every paper here. | Per-chat state to maintain. | Low-volume chats where you can't fit a distribution. Fall back to the global median. |
| **Deterministic contextual headers, not LLM-generated** | Captures most of Anthropic's −35% failure reduction at zero cost and zero privacy exposure — and injects *exactly the metadata your queries contain* ("what did Anna say in 2021"). | Less rich than a generated blurb. | Queries about content with no metadata hook. The fact-augmentation column covers this. |
| **PostgreSQL only, exact vector scan** | One system. Transactional consistency. Free exact filters. No percolation failure. ~60k vectors scans in single-digit ms. | Loses Qdrant's tuned Query API and sparse-vector ergonomics. You implement RRF yourself (~20 lines). | If the corpus grows past ~5M chunks, add an HNSW index then — pgvector supports it. This is a reversible decision. |
| **Hybrid dense + lexical, RRF** | +11.9 EM over naive in the closest published ablation. Anthropic's largest single jump. Disproportionately valuable on RU/UK proper nouns and bare-token first mentions. | Two retrieval paths to maintain and tune. | Neither path helps if segmentation was wrong. Segmentation dominates. |
| **RU/UK lemmatization (pymorphy3 + Ukrainian analyzer)** | RusBEIR: "lemmatization proves critical"; **BM25 beats BGE-M3 by 13pp** on some Russian tasks. | Preprocessing complexity; pymorphy3 is imperfect on surzhyk and slang. | Transliterated Cyrillic ("privet"), heavy slang, and typos. Add a trigram index as a safety net. |
| **Bi-temporal facts in Postgres** | The only published mechanism that correctly answers "where did X live in 2019 *and* in 2026" — both true. Telegram timestamps make `t_valid` grounded rather than inferred. | Extra table, extra write path, more query complexity. | Fact extraction quality on 15-char messages is poor. Extract facts from **sessions**, never from messages. |
| **Deterministic conflict resolution (`max()` in code)** | +21pp at long context vs LLM adjudication. ~$0.0001/query. | Requires monotonic version stamps on every fact. | Facts whose recency ordering isn't the right answer ("I used to prefer X" is still true as history). Bi-temporal validity intervals handle this — that's why both mechanisms are needed. |
| **Deterministic intent routing, not learned** | Adaptive-RAG's learned classifier: **54.52%** on a 3-way task. A router *hurt* retrieval by 1.8 EM in ablation. Your query classes are lexically distinguishable. | Brittle to phrasings you didn't anticipate. | Multilingual trigger words — you need RU/UK/EN trigger lists. Make routing user-visible and overridable. |
| **2-iteration loop cap** | 5→2 iterations costs −0.3 EM at 1/5 the latency. "Two iterations often suffice." | Genuinely multi-hop questions may need 3. | Deep chains. Accept it; the cost curve is brutal (10.3× latency for −0.3 EM). |
| **Local everything for backfill** | Meets both your constraints. ~$0 marginal. | Weeks of wall-clock. Lower extraction quality than a frontier model. | Complex fact extraction from code-switched slang. Budget for a second pass when you get better local models — the star schema makes this cheap. |

### 6.3 Compute budget for the backfill

Order-of-magnitude, on 8–16 vCPU CPU-only. GPU numbers in parentheses assume one consumer card.

| Stage | Volume | Throughput | Wall clock |
|---|---|---|---|
| Segmentation | 681k messages | pure SQL/Python | **minutes** |
| BGE-M3 embedding (session level) | ~60k chunks | ~60 chunk/s CPU | **~20 min** (GPU: ~2 min) |
| BGE-M3 embedding (all messages, if you keep it) | 681k | ~60/s | ~3.2 h |
| faster-whisper large-v3-turbo | 18,638 voice/video notes, mostly <60 s | ~0.5–2× realtime CPU | **~5–10 days CPU** (GPU: ~6–12 h) |
| PaddleOCR Cyrillic | 12,100 photos | ~2–5 img/s CPU | **~1–2 h** |
| VLM captioning (optional) | 12,100 photos | ~0.2 img/s CPU | ~17 h CPU (GPU: ~1 h) |
| Local LLM session enrichment (7–8B, Q4) | ~60k sessions × ~600 tok | ~15–30 tok/s CPU | **~2–4 weeks CPU** (GPU: ~1–2 days) |

**The two long poles are ASR and LLM enrichment, and both are embarrassingly parallel and resumable.** Design the pipeline as a work queue with per-item status so it survives restarts. If you can borrow a GPU for 48 hours, spend it on LLM enrichment first (largest quality delta) and ASR second.

**Ordering advice:** ship segmentation + hybrid retrieval *before* enrichment finishes. Enrichment is an additive column. You get most of the value in week one and the rest arrives incrementally.

---

## 7. Database schema

Full runnable DDL is in the accompanying `TelegramBrain_Schema_v1.sql`. Design rationale below.

### 7.1 What should be a table, and why

Your question — raw messages vs chunks vs conversations vs sessions vs topics vs events vs summaries — has a specific answer: **raw messages and sessions are tables; everything else is a projection.**

| Candidate | Verdict | Reasoning |
|---|---|---|
| **Raw messages** | **Table, immutable, append-only** | The only ground truth. Never rewritten. Enables every future reprocessing. Non-negotiable — this is Graphiti's episode subgraph and SSGM's "dual-track" recommendation. |
| **Sessions** | **Table, derived, versioned** | The retrieval unit. Regenerated when you change the segmentation algorithm — which you will. Version the segmenter so you can A/B. |
| **Chunks** | **Not a separate concept** | For you, session == chunk. Sub-splitting a 250-token session gains nothing and reintroduces the fragmentation problem. |
| **Conversations** (multi-session arcs) | **Table, but cheap** — a thin grouping over sessions | A "conversation" spanning days is a thread, not a session. Model as `thread` with a session membership table. |
| **Topics** | **Table (dimension) + M:N link** | Topics are a controlled vocabulary you want to filter and aggregate on. First-class. |
| **Events / life milestones** | **Table, sparse, high-value** | A few hundred rows over 7.6 years. Manually curatable, LLM-proposable. Disproportionate value per row. |
| **Summaries** | **Column on session, not a layer** | Store the session summary alongside the session. Do *not* build daily/weekly/monthly summary tables — those are `GROUP BY` views. |
| **Facts** | **Table, bi-temporal** | The semantic layer. See below. |
| **Daily/weekly/monthly rollups** | **Materialized views** | Refresh on schedule. Zero drift risk because they're recomputed from source. |

### 7.2 The bi-temporal fact table — the design that matters

```sql
CREATE TABLE fact (
  fact_id       BIGSERIAL PRIMARY KEY,
  subject_id    BIGINT REFERENCES entity(entity_id),
  predicate     TEXT NOT NULL,          -- controlled vocabulary
  object_text   TEXT,
  object_id     BIGINT REFERENCES entity(entity_id),

  -- Timeline T: when the fact was true IN THE WORLD
  t_valid       TIMESTAMPTZ NOT NULL,
  t_invalid     TIMESTAMPTZ,            -- NULL = still true

  -- Timeline T': when the SYSTEM learned/retracted it
  t_created     TIMESTAMPTZ NOT NULL DEFAULT now(),
  t_expired     TIMESTAMPTZ,            -- NULL = not retracted

  confidence    REAL,
  source_session_id BIGINT REFERENCES session(session_id),
  source_message_ids BIGINT[] NOT NULL, -- evidence, always
  extractor_version TEXT NOT NULL,
  refcount      INT NOT NULL DEFAULT 0
);
```

**Why two timelines.** "Where does Anna live?" has a different true answer in 2019 and 2026, and both are correct. A single-timestamp model forces you to choose. `t_valid`/`t_invalid` records the world; `t_created`/`t_expired` records your knowledge of it. This is Graphiti's contribution and it is the highest-value transfer in this entire document. **Telegram gives you exact message timestamps, so `t_valid` is grounded rather than inferred** — a real advantage over the systems this pattern came from.

**Invalidate, never delete.** When a contradicting fact arrives, set the old row's `t_invalid` to the new row's `t_valid`. Nothing is destroyed. "What did I believe in 2021?" remains answerable — and that is most of the value of a personal archive.

**The critical caveat.** Zep/Graphiti scored **7% on FactConsolidation single-hop conflict resolution** — the worst of any system tested, against HippoRAG-2 at 54% and plain BM25 at 48%. **A bi-temporal graph is a good *representation* of contradiction, not a *solution* to it.** Steal the schema; do the resolution in code with `max()` per §2.10.

### 7.3 Session table sketch

```sql
CREATE TABLE session (
  session_id      BIGSERIAL PRIMARY KEY,
  chat_id         BIGINT NOT NULL,
  started_at      TIMESTAMPTZ NOT NULL,
  ended_at        TIMESTAMPTZ NOT NULL,
  message_count   INT NOT NULL,
  participant_ids BIGINT[] NOT NULL,

  raw_text        TEXT NOT NULL,   -- what you RETURN
  embed_text      TEXT NOT NULL,   -- what you EMBED (header + raw + facts)

  summary         TEXT,            -- local LLM
  topics          TEXT[],
  lang_mix        JSONB,           -- {"ru":0.6,"uk":0.3,"en":0.1}
  importance      REAL,
  sentiment       REAL,

  embedding       halfvec(1024),
  ts_lemma        tsvector GENERATED ALWAYS AS (...) STORED,

  segmenter_version TEXT NOT NULL,
  enriched_at     TIMESTAMPTZ
);
```

**The `raw_text` / `embed_text` split is load-bearing.** LongMemEval is explicit: facts must be concatenated with the original, and "using these condensed forms alone does not enhance memory recall." You embed the enriched composite; you return the raw conversation. Conflating these is the most common implementation error in this pattern.

### 7.4 Trade-offs of this schema

**Costs:** more tables than a naive design; a rebuild pipeline you must maintain; refcount bookkeeping on every projection write; storage roughly 1.6× the raw message table.

**Where it fails:** if your segmenter is bad, everything downstream inherits the damage — which is why segmenter versioning and cheap rebuilds are in the design. And fact extraction from code-switched slang will produce garbage rows; `confidence` plus `extractor_version` let you filter and re-run without losing history.

---

## 8. Vector index design

### 8.1 If you keep Qdrant

One collection, session-grained, named vectors:

```python
create_collection(
  collection_name="tg_sessions",
  vectors={
    "dense": VectorParams(size=1024, distance=Distance.COSINE,
                          datatype=Datatype.FLOAT16,
                          quantization_config=ScalarQuantization(
                              scalar=ScalarQuantizationConfig(
                                  type=ScalarType.INT8, quantile=0.99,
                                  always_ram=True)))
  },
  sparse_vectors={
    "lexical": SparseVectorParams(modifier=Modifier.IDF)   # BM25-style
  },
  hnsw_config=HnswConfigDiff(m=16, ef_construct=200,
                             full_scan_threshold=20000),   # ← force brute force
  optimizers_config=OptimizersConfigDiff(memmap_threshold=200000),
)
```

Then **index every payload field you filter on** — without a payload index Qdrant cannot estimate cardinality and, per its own docs, gives "extremely slow search times **or low accuracy results**":

```python
for field, schema in [("chat_id", "integer"), ("started_at", "datetime"),
                      ("participant_ids", "integer"), ("topics", "keyword"),
                      ("year", "integer"), ("lang", "keyword")]:
    create_payload_index("tg_sessions", field, schema)
```

**Set `full_scan_threshold` high (20,000).** At ~60k points this forces exact search for essentially every filtered query, which is what you want: exact recall, no percolation risk, and at this scale it's still milliseconds.

Query with prefetch + RRF:

```python
query_points(
  collection_name="tg_sessions",
  prefetch=[
    Prefetch(query=dense_vec,  using="dense",   limit=100, filter=flt),
    Prefetch(query=sparse_vec, using="lexical", limit=100, filter=flt),
  ],
  query=FusionQuery(fusion=Fusion.RRF),
  limit=20,   # Anthropic: top-20 > top-10 > top-5
)
```

**Do not create a second collection for raw messages unless you have measured a need.** If you do, keep it separate and query it only for the "first mention" path, where high recall over bare tokens matters.

**Do not use multivectors.** Qdrant disables HNSW for them; they are a reranking primitive only. And there is no competitive RU/UK late-interaction model — jina-colbert-v2 scores **MIRACL-ru 64.3 vs BGE-M3 dense 70.1**. Late interaction loses here on quality, not storage.

### 8.2 If you migrate to pgvector (recommended)

```sql
ALTER TABLE session ADD COLUMN embedding halfvec(1024);

-- Deliberately NO ANN index at this scale. Exact scan over ~60k rows.
-- Add this only if the corpus grows past ~1M chunks:
-- CREATE INDEX ON session USING hnsw (embedding halfvec_cosine_ops)
--   WITH (m=16, ef_construction=200);

CREATE INDEX session_ts_idx        ON session (started_at);
CREATE INDEX session_chat_ts_idx   ON session (chat_id, started_at);
CREATE INDEX session_lemma_idx     ON session USING GIN (ts_lemma);
CREATE INDEX session_trgm_idx      ON session USING GIN (raw_text gin_trgm_ops);
CREATE INDEX session_topics_idx    ON session USING GIN (topics);
```

`halfvec` halves storage at negligible quality cost. **~60k × 1024 × 2 bytes ≈ 123 MB — fits in `shared_buffers`.** Exact cosine over that is single-digit milliseconds, and every `WHERE started_at BETWEEN ...` predicate is exact and free.

### 8.3 Storage math for both paths

| Configuration | Vectors | Size |
|---|---|---|
| Session-level, fp32 d=1024 | 60,000 | 245 MB |
| **Session-level, halfvec/fp16 d=1024** | **60,000** | **123 MB** |
| Session-level, int8 d=1024 | 60,000 | 61 MB |
| Per-message (current), fp32 d=1024 | 681,331 | 2.79 GB |
| Per-message, int8 d=1024 | 681,331 | 698 MB |

**Aggregation shrinks your index ~11× while improving retrieval quality.** That is the rare case where the cheap option is also the better one.

---

## 9. Knowledge graph — mostly no, with three exceptions

### 9.1 The case against full GraphRAG for your corpus

Microsoft GraphRAG's diagnosis is right: vector RAG "does not support sensemaking queries... that require global understanding of the entire dataset." Its cure is wrong for you, on four counts.

**(a) Cost.** GraphRAG's per-query context consumption on a **1.7M-token** news corpus: C0 (root communities) 39.8K tokens; C3 (leaf) 1.14M; TS (source text) 1.71M. **Your archive is ~8M tokens — roughly 5× larger.** A naive port of static global search is ~5–10M context tokens *per question*. On a self-hosted model that is not a query, it is an overnight job.

**(b) Entity extraction fails on your data.** GraphRAG extracts entities per chunk with an LLM. **There is frequently no entity in a 15-character message.** 65% of your corpus is in that regime. You'd burn enormous compute to extract almost nothing.

**(c) The evidence base is the weakest in this review.** GraphRAG's evaluation is LLM-as-judge with **zero human validation**; secondary validation via claim extraction reached only 69–78% alignment. The authors chose the metrics — "comprehensiveness," "diversity," "empowerment" — which structurally favour longer, more discursive answers, exactly what map-reduce over community summaries produces. **And GraphRAG loses on directness, the one intuitive metric, by 60–65%.**

**(d) You already have the partition key GraphRAG has to discover.** Community detection exists to find groups of related content. **You have `time` and `chat_id` for free.** Partition by (quarter) or (quarter × chat) and run the identical map-reduce. You get the global-synthesis benefit at roughly **1/25th the cost** and with zero graph construction. Estimated: ~30 quarterly bins × top-20 × ~40 tokens ≈ 24K evidence tokens + 30 summaries + one synthesis ≈ **30–35 LLM calls, ~40K tokens per evolution query.** Minutes on your box.

Also: "Periodic community refreshes remain necessary" — community detection is a global recompute over a growing graph with no incremental story.

### 9.2 What should be a node — the direct answers

| Should this be a node? | Answer | Reasoning |
|---|---|---|
| **Conversations / sessions** | **No** | They're rows with timestamps. A graph adds nothing a `(chat_id, ts)` index doesn't give you. |
| **People** | **Yes — but as a Postgres table** | You have 457 of them with `sender_id` as ground truth. The person is the single most important entity type in a personal archive. But "person" is a dimension table, not a graph node — you almost never traverse more than one hop. |
| **Ideas** | **No** | Too fuzzy to resolve reliably. Model as topics with embeddings and cluster them. |
| **Projects** | **Yes — table** | Small, high-value, human-curatable set. |
| **Facts** | **Yes — table, bi-temporal** | Already covered in §7.2. |

### 9.3 The three things worth taking from graph research

1. **Bi-temporal edges** (Graphiti) — §7.2. Highest-value idea in this document. **As Postgres tables.**
2. **Episode-mentions and node-distance rerankers** (Graphiti) — cheap, non-LLM ranking signals. "How often does this entity appear" and "how close is this to the person I asked about" are strong priors on a 457-sender social graph, and both are `COUNT(*)` and a join.
3. **Personalized PageRank over an entity graph** (HippoRAG 2) — the best-evidenced graph retrieval mechanism, and the best conflict handling measured (54% FC-SH vs Zep's 7%). *Caveat: its headline "7% improvement" is recall@5, not QA — downstream QA gain is +2.8. And its graph was dominated by 1,125,951 synonym edges vs 140,830 real edges, an 8× ratio that will explode on a 457-entity multilingual corpus.* **Worth a V2 experiment, not a V1 dependency.**

### 9.4 Neo4j? PostgreSQL? Qdrant?

**PostgreSQL is enough, and by a wide margin.** Your graph is: 457 people, a few hundred projects, a few thousand entities, some tens of thousands of facts. That is a *small* graph. Postgres recursive CTEs handle the 1–2 hop traversals you actually need. Neo4j buys you Cypher ergonomics and deep-traversal performance you will not use, in exchange for a third system to operate, back up and upgrade.

**Add Neo4j only if** you find yourself writing 4+ hop traversals regularly, or if PPR over the entity graph becomes a core query path and Postgres recursive CTEs measurably can't keep up. Neither is likely.

### 9.5 The cross-script entity resolution problem — this one is real

«Егор» / "Yehor" / «Єгор» / "Egor" are one person. Graphiti's resolution pipeline is cosine similarity → **BM25 over entity names** → LLM adjudication. **The BM25 stage fails entirely across scripts** — these strings share no lexical surface.

Your mitigations, in order:

1. **`sender_id` is ground truth for the 457 people.** Telegram gives you this free. The literature does not have it. Use it and skip the entire hard problem for people.
2. **Transliteration normalization (ISO 9 / BGN-PCGN) as a pre-pass** for entity strings mentioned *in text*.
3. **Multilingual embedding similarity** as the fallback.
4. **LLM adjudication last**, blocked by chat and time window to avoid O(N²).

**Avoid any O(N²) resolution.** A-MEM's 15-hour build at LoCoMo scale is the canary; the survey calls it "super-linear update complexity." Any algorithm comparing each new item against all existing ones dies between 10⁴ and 10⁵. You have 6.8 × 10⁵.

---

## 10. Retrieval pipeline

Five distinct query classes, five distinct handlers. **Do not try to serve them from one endpoint** — "first mention" needs perfect recall and zero LLM reasoning; "how did it evolve" needs stratified coverage and heavy synthesis. They are different systems.

### 10.1 Deterministic intent routing

| Trigger tokens (RU / UK / EN) | Route |
|---|---|
| `сколько`, `скільки`, `how many`, `count`, `чаще всего`, `most` | **SQL aggregate** |
| `впервые`, `вперше`, `first`, `earliest`, `when did I` | **Argmin over match set** |
| `как менялось`, `як змінювалось`, `evolve`, `over the years`, `change` | **Time-stratified map-reduce** |
| explicit date/range: `в 2021`, `last summer`, `у 2019` | **SQL predicate first, rank within** |
| everything else | **Hybrid → RRF → rerank** |

**Log every routing decision and make it user-visible and overridable.** With 54.52% as the published state of the art for *learned* routing, you want an escape hatch.

### 10.2 Handler: general lookup

```
query → [dense: BGE-M3 → exact cosine, top-100]
      → [lexical: lemmatize (pymorphy3) → tsquery + trigram, top-100]
      → RRF fuse (k=60)
      → cross-encoder rerank (bge-reranker-v2-m3), top-20
      → assemble context with citations → answer
```

Top-20, not top-5 — Anthropic measured top-20 outperforming both. Rerank latency is "negligible" per the agentic ablation and buys 2.9% → 1.9% failure rate.

### 10.3 Handler: "when did I first mention X"

**This is not a retrieval query.** Formally: `SELECT MIN(ts) FROM messages WHERE matches(text, X)`.

Top-k similarity returns the *k most similar*, which are almost never the *earliest*, and there is no k at which "earliest" becomes reachable — similarity and recency are uncorrelated.

```
1. Resolve X to an UNBOUNDED match set (recall must be ~100%):
     BM25/trigram over lemmatized text
     UNION vector similarity ABOVE A THRESHOLD (a score>τ set scan, not top-k)
     UNION alias / transliteration / morphological variants
2. ORDER BY ts ASC LIMIT ~20
3. LLM-verify the earliest candidates one at a time, walking forward
   until one verifies as a genuine mention
4. Return the timestamp + surrounding session as evidence
```

**The expensive part is step 1's recall, not step 2's sort.** And note: **run this against the raw message table, not sessions** — a first mention is typically a bare token in a 15-character message, and BM25 with proper lemmatization will beat any embedding model on that specific step.

### 10.4 Handler: "how did my view on Y evolve"

Top-k fails here for a mechanical reason: **it optimizes for similarity density, not temporal coverage.** If you discussed Y intensely for three weeks in 2022, top-50 will be 45 messages from those three weeks and nothing from 2019 or 2025.

```
1. Partition 2018–2026 into ~30 quarterly bins (or adaptive bins by volume)
2. Run retrieval for Y INDEPENDENTLY WITHIN EACH BIN, top-n per bin
   → guarantees coverage, costs the same as one large retrieval
3. Summarize per stratum ("in Q3 2021 the stance was…")
4. SORT stratum summaries CHRONOLOGICALLY before the synthesis prompt
5. Explicitly prompt for change-detection: where did the stance shift,
   and what is the evidence for each shift
```

Step 4 is not cosmetic: Test of Time measured sorted presentation at **71.95% vs shuffled 58.82%**. Step 2 is ChronoQA's decomposition finding: **+68% relative on multi-document questions**.

### 10.5 Handler: aggregates

Straight to SQL. **Always show the generated query and the row count.** Text-to-SQL leaders sit at ~80% execution accuracy against a ~93% human baseline on BIRD; your schema is far simpler so expect better, but budget for wrong SQL and make it visible.

The hard case is the hybrid — *"how many times did I complain about work"* needs a semantic predicate inside an aggregate. **This is not a query-time operation.** Pre-compute a fixed taxonomy of labels offline once, index the label column, and the aggregation becomes pure SQL. Ad-hoc semantic aggregation over 681k rows is not achievable interactively on your hardware, and no paper claims otherwise.

---

## 11. Ranking pipeline

```
Stage 1  CANDIDATE GENERATION
         dense (exact cosine)     → 100
         lexical (BM25 lemmatized)→ 100
         Hard filters applied FIRST, as SQL predicates (exact, free)

Stage 2  FUSION
         RRF:  score(d) = Σ_r  1 / (k + rank_r(d)),  k = 60
         Prefer RRF over weighted score fusion — no score normalization
         needed, and robust to the two retrievers' incomparable scales.

Stage 3  CHEAP NON-LLM SIGNALS  (Graphiti's contribution, ~free)
         + entity_mention_count    "how central is this entity"
         + node_distance           "how close to the person I asked about"
         + session_importance      precomputed at enrichment
         + participant_match       did the named person actually speak here

Stage 4  CROSS-ENCODER RERANK
         bge-reranker-v2-m3 over top-50 → top-20
         (+1.7 EM; failure 2.9% → 1.9%; negligible latency)

Stage 5  RECENCY  — OFF BY DEFAULT
         Apply gauss decay (scale≈30d, offset≈7d, decay≈0.5) ONLY when
         intent detection finds no temporal anchor.
         NEVER apply Solr-style recip: 8.6× penalty on your 2018 content.
```

**The one thing not to do:** a global recency prior. Elastic's own documentation carries the worked example of decay swamping relevance. Your interesting queries are archaeological.

---

## 12. Embedding strategy

### 12.1 What to embed — the direct answer

| Unit | Embed? | Why |
|---|---|---|
| Every message | **No** | 65% are under 20 chars. This is the core error. |
| Messages > 200 chars (10,484) | **Optional, cheap** | Genuinely self-contained. A small second collection costs ~40 MB and helps precise recall. |
| **Merged time-gap sessions** | **Yes — the primary index** | ~60k units. The whole design rests on this. |
| **Session + extracted facts (composite)** | **Yes — this is what you embed** | +9.4% recall, +5.4% accuracy. Concatenate, never substitute. |
| Daily / weekly summaries | **No** | Summaries are the worst granularity measured (53.87–56.25 vs 71.57). Calendar rollups are views. |
| Participants (person dossiers) | **Yes — small, high value** | 457 vectors. Enables "who do I talk to about X." |
| Topics | **Yes — small** | A few thousand. Enables topic-similarity navigation. |
| Projects | **Yes — tiny** | Dozens. Free. |
| Extracted facts (standalone) | **Yes — separate collection** | Enables precise fact lookup. But *also* keep them concatenated into session `embed_text`. |

### 12.2 Model selection for RU/UK/EN

**ruMTEB** ([arXiv:2408.12503](https://arxiv.org/html/2408.12503v1), 23 datasets) — read the **retrieval** column, not the average:

| Model | Params | MIRACL-ru | RiaNews | RuBQ | **Retrieval avg** |
|---|---|---|---|---|---|
| **BGE-M3** | 567M | **70.16** | **82.99** | 71.22 | **74.79** |
| E5-mistral-7b | 7.1B | 67.66 | 78.94 | **75.98** | 74.19 |
| mE5-large | 560M | 67.33 | 80.67 | 74.13 | 74.04 |
| mE5-base | 278M | 61.60 | 70.24 | 69.58 | 67.14 |
| ru-en-RoSBERTa | 404M | 53.91 | 78.86 | 66.77 | 66.52 |
| mE5-small | 118M | 59.01 | 70.00 | 68.53 | 65.85 |

**Note the trap: the Russian-*specialized* model (ru-en-RoSBERTa) wins on classification and STS and loses retrieval by 8+ points. Language-specialized ≠ better retrieval.** On RusBEIR, BGE-M3 averages 61.13 nDCG@10 vs mE5-large 60.12 and BM25 52.16; adding a BGE reranker takes it to **65.85 (+4.7)**.

**Recommendation: BGE-M3.** Reasons, in order: best verified Russian retrieval; 8192-token context; 100+ languages including Ukrainian; and it produces **dense + sparse + ColBERT heads from one encoder** — the sparse head recovers exact-match on names and slang that dense smears, which matters enormously for your corpus.

**Alternatives worth knowing:** `multilingual-e5-large-instruct` (top *public* model on MMTEB at only 560M params) is a close second and slightly faster. `Qwen3-Embedding` has the highest aggregate MTEB-Multilingual score (70.58) and Matryoshka-truncatable dims 32–1024, **but publishes no per-language Russian or Ukrainian breakdown** — validate before trusting it.

**Ukrainian has essentially zero published retrieval evaluation. There is no ukMTEB.** Budget for building your own held-out set — a few hundred query/positive pairs from your actual messages will tell you more than every leaderboard in this document.

### 12.3 Dimensions, quantization, cost

| Config | Bytes/vector | 60k sessions | Quality note |
|---|---|---|---|
| fp32 d=1024 | 4,096 | 245 MB | baseline |
| **fp16 / halfvec d=1024** | **2,048** | **123 MB** | **negligible loss — use this** |
| int8 d=1024 | 1,024 | 61 MB | ~1–2% loss, test it |
| binary d=1024 | 128 | 7.7 MB | **model-dependent: GTE-ModernBERT retains 98%, E5-small-v2 drops to 87%. Do not assume — measure.** |

At your scale storage is irrelevant, so **optimize for quality: halfvec at full 1024 dims.** Matryoshka truncation is a tool for when you have 100M vectors. You have 60,000.

### 12.4 Late interaction — the answer is no

You asked implicitly by mentioning Qdrant's capabilities. The evidence is unusually clean and it says don't:

1. **No competitive RU/UK model exists.** jina-colbert-v2 MIRACL-ru **64.3** vs BGE-M3 dense **70.1**. This alone settles it. *(Note the vendor discrepancy: Jina's blog says "89 languages"; the peer-reviewed MRL 2024 paper says trained on **14**, with a "small 2.0% share for lower-resource languages." Ukrainian is in that 2% bucket at best.)*
2. **Short documents nullify the mechanism.** MaxSim's advantage is finding a query term's best match inside a *long* passage. On BEIR's shortest corpus — QuoraRetrieval, 11.44 words average — every late-interaction model **loses** to single-vector dense (answerai-colbert-small 87.72 vs bge-base 88.90). Your messages are shorter still.
3. **The controlled measurement.** BGE-M3's own ColBERT head over the identical encoder adds **+1.3 nDCG@10 on MIRACL average, +1.1 on Russian** — on passages ~100 words long. Extrapolating to 7-token messages: approximately zero.
4. **Qdrant disables HNSW for multivectors.** Brute force only.

**Storage is *not* the reason** — and I want to be explicit because the usual objection is wrong here. A ColBERTv2 2-bit index at 12 tokens/message is **294 MB**, *less than half* an int8 d=1024 single-vector index. The multi-vector penalty scales with document length and your documents are the shortest imaginable. **Reject late interaction on quality grounds, not storage.**

**The one defensible use:** MaxSim as a final-stage reranker over 50–200 candidates, where it costs <1 ms. If you want that, use BGE-M3's built-in ColBERT head — you already paid for the encoder, the multi-vector output is free, and it's the only RU-capable option with a controlled measurement behind it. Store token vectors only for the ~40,880 messages over 60 chars: at 32 bytes/vector binary, **~42 MB**. Cheap, bounded, honest experiment.

### 12.5 ASR, OCR, and vision

| Task | Volume | Recommendation | Notes |
|---|---|---|---|
| Voice + video notes | **18,638** | **faster-whisper `large-v3-turbo`** (CTranslate2) | Best RU/UK quality per unit compute. WhisperX adds alignment + diarization but you rarely need it for 1:1 notes. Mostly <60s clips — batch aggressively. |
| Photos | **12,100** | **PaddleOCR (Cyrillic)** first; VLM captioning second | **A large share of personal Telegram photos are screenshots and memes containing text.** OCR is the high-value, cheap pass. Captioning is the expensive, lower-value one. Do OCR first, measure how many photos yield text, then decide on captioning. |
| Documents | 2,113 | Standard text extraction | Trivial. |

**Ordering matters:** ASR unlocks 18,638 messages that are currently invisible to search. That is 2.7% of your corpus and, given they're voice notes to close contacts, likely well above average in personal significance. **Prioritize ASR over photo captioning.**

---

## 13. Metadata extraction strategy

### 13.1 The structuring decision

Your list of ~28 metadata types needs a rule, not a case-by-case argument. The rule: **structure it if you will filter, aggregate, or sort on it. Leave it as searchable text otherwise.**

**Tier A — structured entities (own tables, indexed, joinable)**

| Type | Why structured |
|---|---|
| **People** | `sender_id` is ground truth. 457 rows. The most important dimension in the archive. |
| **Projects** | Small, curatable, you'll filter by them constantly. |
| **Commitments / tasks** | Need a *lifecycle* (open → done → abandoned). Only a table gives you "find forgotten commitments." |
| **Decisions** | High value, low volume, need outcome tracking. |
| **Dates / deadlines** | Must be sortable and comparable. Never leave a deadline as text. |
| **Money** | Amount + currency + direction. Aggregatable. Also: UAH inflation over 7.6 years means you need the date attached. |
| **Locations** | Filterable, and enables travel history. |
| **Technologies** | You're a developer — this is a high-cardinality, high-value facet for "when did I first hear about X." |
| **Life milestones** | A few hundred rows over 7.6 years. Disproportionate value. |

**Tier B — controlled-vocabulary labels (an array column + GIN index)**

Topics, learning subjects, long-term interests, habits, recurring events, relationship type. These need a **fixed taxonomy defined up front**. Free-form LLM labels produce thousands of near-synonyms and become unaggregatable. Define ~100–200 labels, extract against that closed set, and revise the taxonomy deliberately.

**Tier C — scalar scores on the session row**

Sentiment (−1..1), emotional intensity, importance (1–10), formality, language mix (JSONB). Cheap to compute, essential for the mood-over-time analytics, and useless as retrieval targets.

**Tier D — leave as searchable text**

Books, movies, music, games, ideas, opinions, preferences, problems, health mentions. Reason: **the extraction cost is high and the aggregate value is low.** You will search "what did I say about that book" far more often than you'll run `SELECT * FROM books`. Extract them as facts (Tier A's `fact` table with a `predicate`) only where they recur.

### 13.2 The health and emotional-state caveat

Health mentions and emotional state are the two categories where I'd push back on your list. Structuring them means building a longitudinal mood and health record of yourself, and then querying it. That's a real capability — "what was happening in the months before things went wrong" is one of your stated goals — and it's also the part of this system most likely to be **wrong in a way that feels authoritative.**

Sentiment analysis on code-switched Russian/Ukrainian IM with heavy irony and slang is not reliable. A chart showing "your mood declined in 2023" built from that is a confident-looking artifact resting on a shaky measurement. **Build it if you want it, but attach confidence intervals and always link back to the actual messages.** Never let the derived score be the answer; make it a pointer to evidence.

### 13.3 Extract from sessions, never from messages

The mechanical point: an extraction prompt over a 15-character message produces nothing or garbage. Over a 20-message session it produces usable structure. **This is another reason segmentation is the load-bearing stage** — it's a prerequisite for every enrichment downstream, not just for retrieval.

### 13.4 Extraction cost, honestly

From measured data on comparable systems, token amplification over raw text ranges **~15× to ~78×** depending on the extraction schema. On your ~8M raw tokens:

| Amplification | Total tokens | Local (your box) | Frontier API (for reference) |
|---|---|---|---|
| 15× | 120M | ~1–2 weeks CPU | ~$570 |
| 30× | 240M | ~3–4 weeks CPU | ~$1,140 |
| 80× | 640M | months — **don't** | ~$3,040 |

**Design for ~15×.** Keep the extraction schema tight. And **budget for reprocessing, not just processing** — you will change the schema repeatedly, and the star schema in §5.4 exists precisely so that's cheap.

---

## 14. Analytics and AI capabilities

### 14.1 Analytics — ranked by value per unit of effort

**Tier 1 — pure SQL over your existing views. Build these in an afternoon.**

| Analytic | Query shape | Why it's valuable |
|---|---|---|
| Message volume by month/year/chat | `GROUP BY` | You have it already. It's the skeleton of a life timeline. |
| **Relationship half-life** | last_message_at per chat, decay curve | 487 chats, many dormant. "Who have I drifted from" is emotionally significant and trivially computable. |
| **Response latency by person, over time** | `LEAD(ts) - ts` per chat | A remarkably sharp proxy for relationship closeness. Watch it change around specific dates. |
| **Initiation ratio** | who sends the first message of each session | Who pursues whom. Changes over a relationship's life. |
| Diurnal and weekly rhythm shifts | `GROUP BY hour, year` | Sleep-schedule changes are visible without anyone mentioning sleep. |
| Vocabulary growth / churn | distinct lemmas per period | Language change is measurable and interesting. |
| Language-mix drift (RU/UK/EN) | `lang_mix` aggregated by month | **For a Ukrainian in 2018–2026 this is not a linguistics curiosity. It is a record of something.** |

**Tier 2 — needs enrichment, high value.**

Topic prevalence over time (with change-point detection). Mood trajectory per relationship. Project timelines reconstructed from mentions. Learning-topic progression. Idea-generation rate. Commitment completion rate. Travel history from location mentions. Network analysis over co-mentions.

**Tier 3 — the ones worth adding that you didn't list.**

| Analytic | Why |
|---|---|
| **Change-point detection on any series** | Don't eyeball charts. Run PELT or Bayesian change-point detection on topic frequency, sentiment, volume, response latency. It finds the dates that mattered *without you knowing to look*. This is the highest-value addition on this list. |
| **Topic birth/death rate** | "What disappeared" is one of your stated questions. It's a survival-analysis problem, not a retrieval one. |
| **Conversational entropy** | How predictable is a given relationship's content. Drops sharply when something changes. |
| **Reciprocity asymmetry** | Message length ratio, question-asking ratio. Reveals imbalance you can't feel from inside. |
| **Self-disclosure depth over time** | Proxy: mean message length + first-person + emotional-word density per relationship. Tracks intimacy trajectory. |
| **Idea → action conversion rate** | Cross-reference stated intentions against later evidence. Brutal and useful. |
| **Cross-chat topic diffusion** | When you learn something in chat A, how long until it appears in chat B? A measure of how you actually process ideas. |

### 14.2 AI capabilities — what to build, sequenced by value/effort

**Immediately valuable, cheap:**

1. **On-this-day resurfacing.** Highest value per line of code in this document. Day One proved it. One cron job, one query.
2. **Forgotten commitments.** Commitment table + status lifecycle + "open and older than 90 days." This is the feature that turns an archive into a system that acts on you.
3. **Attributed natural-language QA.** The core capability. Every answer cites message IDs and dates. Non-negotiable — an unattributed answer over your own life is worse than no answer.
4. **Per-person dossiers, auto-maintained.** 457 of them, regenerated on schedule. "What do I know about this person, what did we last discuss, what did I promise them."

**High value, moderate effort:**

5. **Timeline generation for an entity/topic.** The stratified handler from §10.4, exposed directly.
6. **Contradiction detection.** Query the bi-temporal fact table for overlapping `t_valid` intervals with incompatible objects. Surfaces "you said X in 2021 and not-X in 2024" — one of the genuinely novel capabilities here.
7. **Abandoned-idea detection.** Ideas mentioned ≥3 times in a window and zero times since. Survival analysis over topics.
8. **"What would past me think?"** Retrieve your own statements from a target period, synthesize a stance, present it in your own words with citations. Uses the stratified retrieval already built.
9. **Conversation replay with context.** Reconstruct a specific period with everything you know about it — sessions, facts valid at the time, what you didn't yet know.

**Speculative but distinctive:**

10. **Decision post-mortems.** Decision table + outcome linking + "how did that go." Requires manual seeding but is where a personal archive genuinely outperforms memory.
11. **Duplicate-idea detection.** You have had the same idea five times. Cluster idea-facts, show recurrence. Slightly humbling.
12. **Pre-conversation briefing.** Before you meet someone, generate: last N topics, open commitments both ways, things they mentioned that you should ask about. This is the single most immediately *useful* feature in the list.
13. **Predictive reminders.** "You usually message your grandmother around this interval and it's been 3× longer than usual."

**The one I'd caution about:** automated journaling and "life review" generation. Both produce plausible, well-written narrative from noisy inputs, and narrative is exactly the format in which errors become invisible. If you build them, force citation density and keep them explicitly draft-quality.

---

## 15. Scaling

Your stated future: 10+ years, millions of messages, images, documents, voice, OCR, STT.

### 15.1 What actually scales, and what doesn't

At your growth rate (~90k messages/year), you reach **1.5M messages around 2035**. That is not a scaling problem for any component in this design. Sessions grow to ~130k. Vectors stay under 300 MB. Postgres does not care.

**What does scale badly:**

| Component | Failure point | Mitigation |
|---|---|---|
| **Entity resolution** | Any O(N²) comparison dies between 10⁴ and 10⁵ items | **Block by `sender_id`, chat, and time window from day one.** Never compare globally. |
| **Re-enrichment passes** | Each full reprocess is weeks of CPU and you will do many | Version everything; reprocess incrementally by date range; keep the work queue resumable. |
| **Community detection** (if you ever add graph) | Global recompute, no incremental story | Another reason to use time as the partition key instead. |
| **Contradiction accumulation** | Hundreds of superseded facts per entity over a decade | Bi-temporal intervals plus `max()` resolution. Do not let an LLM adjudicate. |
| **Semantic drift from re-summarization** | Compounds silently over a decade | The immutable episodic log is the answer. Never summarize a summary. |
| **Deletion** | O(N·d) index rebuild plus backflow from derived artifacts | Reference counting from day one. Retrofitting is very expensive. |
| **Media storage** | Photos and voice notes are the actual byte problem, not vectors | Store media outside the DB; keep hashes and derived text in it. |

### 15.2 The honest limit

**Nobody has measured any of this past 10M tokens.** BEAM's 10M is one linear conversation, not 487 separately-indexed chats requiring cross-chat entity resolution. Treat every scaling claim in this document — including mine — as directionally supported, not quantified. You are operating past the edge of the published evidence.

---

## 16. Implementation roadmap

### MVP — 2 weekends. Goal: beat grep, and know by how much.

1. **Build the baseline and measure it.** `ripgrep` over a text dump of your archive. Write 30–50 real questions you actually want answered, spanning all five query classes. **Score grep on them.** This number is your bar for everything else.
2. **Measure your gap distribution.** Plot inter-message gaps per chat. Find the bimodal valley. This is a 20-line script and it is worth more than any paper cited here.
3. **Segment.** Implement time-gap sessionization with the fitted thresholds, ≤30 messages, ≤250 tokens, singletons merged. Write to a `session` table.
4. **Index sessions.** BGE-M3 dense (halfvec) + lemmatized tsvector. Deterministic headers. No enrichment yet.
5. **Hybrid retrieval + RRF + rerank.** ~200 lines.
6. **Score it against the same 30–50 questions.** If it doesn't clearly beat grep, stop and debug segmentation before building anything else.

**Deliverable: a number. Do not skip step 6.**

### V1 — 1–2 months. Goal: the system becomes useful daily.

7. **Deterministic intent routing** with RU/UK/EN trigger lists, user-visible and overridable.
8. **The four query handlers** — lookup, argmin/first-mention, aggregate/SQL, stratified evolution.
9. **ASR backfill** on 18,638 voice/video notes. Highest-value unlock in the whole plan — 2.7% of your corpus, currently invisible, disproportionately personal.
10. **OCR backfill** on 12,100 photos. Measure the text-yield rate before deciding on captioning.
11. **Local LLM session enrichment**, run incrementally, newest-first so recent data is useful immediately. Summary + topics + entities + facts + importance.
12. **On-this-day resurfacing** and **per-person dossiers**. Cheap, immediately valuable, and they'll tell you whether the enrichment quality is good enough.
13. **Expose it through Telegram**, not a web app. You already live there. Khoj's multi-client design is the model.

### V2 — 3–6 months. Goal: capabilities nothing else has.

14. **Bi-temporal fact table** with deterministic `max()` conflict resolution.
15. **Commitment extraction with lifecycle** → forgotten-commitment detection.
16. **Contradiction detection** over overlapping validity intervals.
17. **Change-point detection** across topic frequency, sentiment, volume, response latency. Feed the detected dates into timeline generation.
18. **Pre-conversation briefing.** The most immediately useful feature in this document.
19. **Build a proper eval set** — 200+ questions with known answers, and start tracking regressions. Nothing here is measurable without it.

### V3 — 6–12 months. Goal: research territory.

20. **Fit a decay curve to your own 7.6 years of retrieval behaviour.** Every published forgetting formula uses hand-picked constants validated over days. **You would be ahead of the literature.**
21. **PPR over an entity graph** (HippoRAG 2 style) as an experiment, measured against the V1 baseline. Adopt only if it wins.
22. **A RU/UK retrieval eval set** built from your own data — there is no ukMTEB, and this would be a genuine contribution.
23. **Cross-chat topic diffusion analysis.**
24. **Multilingual memory benchmark.** You have the only corpus in existence for this. Publishing an anonymized methodology (not data) would be a real contribution to a field that currently pretends only English exists.

---

## 17. Biggest technical risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **Segmentation is wrong and everything downstream inherits the damage.** SOTA dialogue segmentation reaches only Pk 38.11 on realistic data — misclassifying boundary/non-boundary 38% of the time. | **Critical** | Use time gaps (deterministic, free, reliable) not learned segmentation. Version the segmenter. Make rebuilds cheap. Manually inspect 100 random sessions before trusting the pipeline. |
| 2 | **Local LLM extraction quality on code-switched RU/UK slang is unmeasured and probably poor.** | **High** | Hand-label 200 sessions. Measure precision/recall of extraction before running the 60k-session pass. Store `extractor_version` and `confidence` so you can filter and re-run. |
| 3 | **Semantic drift.** Any re-summarization pass compounds error over years. | High | Immutable episodic log. Never summarize a summary. All projections cite source message IDs. |
| 4 | **Silent recall loss.** You never learn what the system failed to find. | High | Exact search, not ANN. Log every query's candidate counts. Periodically spot-check against brute-force ground truth. |
| 5 | **Contradiction accumulation over 7.6 years.** LLM adjudication degrades 14 points from 64K to 262K context. | High | `max()` in code, never in a prompt. Bi-temporal intervals. |
| 6 | **O(N²) entity resolution** kills the pipeline between 10⁴ and 10⁵ items. | High | Block by `sender_id`, chat, and time window. Never compare globally. |
| 7 | **Backfill wall-clock.** ASR + enrichment is weeks of CPU. Half-finished pipelines rot. | Medium-high | Resumable work queue with per-item status. Ship retrieval before enrichment completes. Process newest-first. |
| 8 | **Deletion leaves shards** ("backflow"). | Medium | Reference-count every derived artifact from day one. Retrofitting is very expensive. |
| 9 | **No eval set means no way to know if changes help.** | Medium | Build 30–50 questions in the MVP, 200+ by V2. Without this you are tuning blind. |
| 10 | **Cyrillic tokenization inflates every cost estimate ~1.75×** vs the Latin-text assumptions in every paper cited. | Low-medium | Measure your actual token counts early; don't trust the estimates in this document without checking. |
| 11 | **You are past the edge of published evidence** at 8M tokens, sub-20-char utterances, and multilingual. | Medium | Expect surprises. Instrument heavily. Treat every recommendation here as a hypothesis with a measurement attached. |

## 18. Biggest product risks

| # | Risk | Why it matters |
|---|---|---|
| 1 | **You build it and don't use it.** The most likely failure by a wide margin. Dot shut down; Rewind pivoted to hardware. The consumer market for memory assistants is unproven, and *your* market is one person. | Ship into Telegram, not a web app. Make the first features *push* (on-this-day, pre-conversation briefing) rather than *pull* (a search box you must remember to open). |
| 2 | **Confident wrongness.** A well-written summary of your own life that's subtly incorrect is worse than nothing — you have no way to detect it, because it's about *you* and it sounds right. | Attribution on every claim. Confidence scores. Never let a derived score be the final answer; make it a pointer to evidence. |
| 3 | **The mirror problem.** A system that tells you what you were like will shape what you become. Stored "Identity/Values/Personality" layers become self-fulfilling. | This is the strongest reason to *compute* profiles on demand from evidence rather than *store* them. It is an architectural decision with a psychological consequence. |
| 4 | **Reading it hurts.** Seven years of intimate conversation includes breakups, deaths, arguments, and the war. A search that surfaces those without warning is a real harm. | Build content warnings and date-range exclusions before you build mood analytics. Give yourself a way to mark periods as "don't resurface." |
| 5 | **Other people's data.** 456 other people are in this archive and none of them consented to being analyzed. | Not a legal problem for personal use in most jurisdictions, but worth deciding deliberately: person dossiers on your friends are a different artifact from a search index. Keep it local, never expose it, and think before you build the reciprocity-asymmetry chart about a specific person. |
| 6 | **Scope creep into a product.** The feature list in §14 is 13 items long and each is a weekend. | The roadmap is ordered by value. Ship MVP step 6 — the number — before touching anything in V2. |
| 7 | **Maintenance burden outlives motivation.** A pipeline with ASR, OCR, VLM, LLM enrichment, embeddings and two databases is a lot of surface for one person. | This is the strongest practical argument for collapsing to one database. Every system you remove is a system you don't have to keep alive in 2030. |

---

## 19. Future research directions

Ordered by how much of a genuine contribution each would be, given that you hold a corpus almost nobody else has.

1. **A multilingual conversational-memory benchmark.** **There is none as of July 2026.** Every benchmark reviewed here is English-only, and the surveys say so explicitly. You have 7.6 years of code-switched RU/UK/EN conversation. An anonymized methodology plus a synthetic-but-realistic derived benchmark would be a real contribution to a field with a blind spot the size of Eastern Europe.
2. **Sub-20-character utterance memory.** Zero papers. LoCoMo averages 30 tokens/turn; the entire field assumes propositional turns. IM is the dominant form of human text communication and nobody has studied memory over it.
3. **Empirically fitted forgetting curves.** Every decay formula in the literature uses hand-picked constants — MemoryBank validated over 10 days. Fit one to seven years of real retrieval behaviour and you'd have the first empirically grounded retention model in the field.
4. **Segmentation of personal IM without reply structure.** Named as open (RQ2 in the episodic-memory position paper). You have 7.9% reply coverage as supervised anchors over 92% unlabeled data — a natural semi-supervised setup nobody has exploited.
5. **Cross-script entity resolution.** «Егор»/"Yehor"/«Єгор» is unsolved in every system reviewed, and Graphiti's BM25 stage fails on it outright. You have `sender_id` as ground truth for 457 entities — a rare labeled dataset for exactly this problem.
6. **Contradiction detection over multi-year personal belief.** BEAM's authors call it "a challenging open problem." Your archive contains thousands of natural instances.
7. **Whether personal archives should have memory at all.** The mirror problem in §18.3 is not engineering, but it's the most interesting question here. A system that models your identity changes it. Nobody has studied this and someone should.

---

## Appendix — key sources

**Benchmarks & critique:** [LoCoMo](https://arxiv.org/abs/2402.17753) · [locomo-audit](https://github.com/dial481/locomo-audit) · [Penfield audit](https://penfieldlabs.substack.com/p/we-audited-locomo-64-of-the-answer) · [LongMemEval](https://arxiv.org/pdf/2410.10813) · [BEAM](https://arxiv.org/pdf/2510.27246v2) · [Benchmark Theatre](https://essays.bloo-mind.ai/posts/2026-05-20-mem-eval/)

**Memory systems:** [Mem0](https://arxiv.org/abs/2504.19413) · [Zep/Graphiti](https://arxiv.org/abs/2501.13956) · [Zep rebuttal](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/) · [zep-papers#5](https://github.com/getzep/zep-papers/issues/5) · [Letta benchmark](https://www.letta.com/blog/benchmarking-ai-agent-memory) · [Sleep-time compute](https://arxiv.org/abs/2504.13171) · [HippoRAG 2](https://arxiv.org/abs/2502.14802) · [A-MEM](https://arxiv.org/abs/2502.12110) · [Cognee](https://github.com/topoteretes/cognee) · [Episodic memory position paper](https://arxiv.org/abs/2502.06975)

**Granularity & chunking:** [SeCom](https://arxiv.org/abs/2502.05589) · [Semantic chunking cost](https://www.arxiv.org/pdf/2410.13070) · [Chroma chunking](https://www.trychroma.com/research/evaluating-chunking) · [Late chunking](https://arxiv.org/pdf/2409.04701) · [Anthropic contextual retrieval](https://www.anthropic.com/news/contextual-retrieval)

**Temporal:** [TCELongBench](https://arxiv.org/html/2406.02472v1) · [Test of Time](https://openreview.net/pdf?id=44CoQe6VCq) · [TempRAGEval/MRAG](https://aclanthology.org/2025.findings-emnlp.167.pdf) · [ChronoQA](https://arxiv.org/html/2508.12282v1) · [Conflict resolution](https://arxiv.org/html/2606.01435v1)

**Agentic & routing:** [Dissecting Agentic RAG](https://arxiv.org/html/2606.21553) · [Adaptive-RAG](https://arxiv.org/pdf/2403.14403) · [Beyond Static Retrieval](https://arxiv.org/html/2509.25530) · [GraphRAG](https://arxiv.org/html/2404.16130v2) · [TAG](https://arxiv.org/pdf/2408.14717)

**Multilingual & models:** [ruMTEB](https://arxiv.org/html/2408.12503v1) · [BGE-M3](https://arxiv.org/pdf/2402.03216) · [RusBEIR](https://arxiv.org/html/2504.12879) · [jina-colbert-v2 (MRL 2024)](https://aclanthology.org/2024.mrl-1.11.pdf) · [BEIR](https://arxiv.org/pdf/2104.08663)

**Infrastructure:** [Qdrant filterable HNSW](https://qdrant.tech/articles/filterable-hnsw/) · [Qdrant multi-vector limits](https://qdrant.tech/course/multi-vector-search/module-1/multi-vector-in-qdrant/) · [Vespa streaming mode](https://blog.vespa.ai/scaling-personal-ai-assistants-with-streaming-mode/) · [Vespa embedding tradeoffs](https://blog.vespa.ai/embedding-tradeoffs-quantified/)

**Forgetting & deletion:** [Generative Agents](https://arxiv.org/abs/2304.03442) · [FadeMem](https://arxiv.org/html/2601.18642) · [FSFM](https://arxiv.org/html/2604.20300v1) · [Agentic unlearning](https://arxiv.org/html/2602.17692v1) · [SSGM](https://arxiv.org/html/2603.11768v1)

---

## Appendix B — verification notes

The pipeline code shipped alongside this report was tested against a synthetic corpus matching your measured distribution (70% of messages under 20 chars, bursty with realistic inter-burst gaps). Results and one instructive failure:

**Segmentation validated the core estimate.** 4,176 synthetic messages → 298 sessions, **14.0× compression**, zero singletons, median session 14 messages / 78 tokens. Extrapolated to your 681,331 messages: **~48,700 sessions** — inside the 50,000–70,000 range this document assumes throughout.

**Gap fitting found the bimodal valley** at 656 s (~11 min) against a p50 of 100 s and p90 of 175 s. Note the valley sits well above p90 — a fixed p90 threshold would have over-split. This is the argument for fitting the distribution rather than picking a percentile.

**Intent routing: 8/8** on RU/UK/EN test queries, and the two safety invariants hold — recency decay never fires when a date anchor is present, and does fire on present-tense queries.

**Bi-temporal invalidation** correctly closed validity intervals in chronological order given deliberately out-of-order input (Харьков 2019→2022, Киев 2022→2024, Львів 2024→present), and `max(version)` resolution returned the current value.

**The instructive failure — cross-script entity resolution.** The first implementation used a straight ISO-9 transliteration, the approach the literature implies. It produced **four distinct keys for one person**: `Егор`→`ehor`, `Єгор`→`iehor`, `Yehor`→`yehor`, `Egor`→`egor`. It does not work, and this is worth stating because it is exactly the failure the research predicts — Graphiti's BM25 resolution stage "fails entirely across scripts."

The fix needed **two tiers**:

1. **Coarse phonetic key** — transliterate, then collapse the equivalence classes RU, UK and Latin romanizations disagree about (ye/ie/je→e, kh→h, ts→c, y/j→i, leading h/g→g, doubled letters, trailing vowels). This merges person names: all five spellings of Yehor → `egor`.
2. **Consonant skeleton** — drop all vowels. Required because Russian and Ukrainian differ by *systematic vowel alternation*, not spelling convention: Киев/Kiev→`kev` but Київ/Kyiv→`kiv`; Харьков→`garkov` but Харків→`garkiv`. No transliteration table fixes that; dropping vowels does (`kv`, `grkv`).

The skeleton tier deliberately over-merges, so it is a **candidate generator, never a decision** — confirm with embedding similarity, then an LLM only if still ambiguous. It also needed a minimum-length guard: `Аня`→`n` is a 1-character skeleton that would collide with everything. Threshold is 2, not 3, because `Київ`→`kv` is precisely the case the tier exists for.

Final state: all five variant groups (a first name, a surname, Kyiv, Kharkiv, Lviv) collapse correctly, and nine distinct first names taken from your actual chat list stay distinct.

**What this episode should tell you:** the cross-script problem is real, it is unsolved in every system reviewed, and it took two iterations and a non-obvious technique to handle even at toy scale. Budget for it. And remember that for the **457 people** you skip it entirely — Telegram's `sender_id` is ground truth the literature does not have.

**Not tested:** everything requiring a live database, embedding model, or LLM — the SQL functions, the embedder, the lemmatizer, the rerankers, and the query handlers. The schema is syntactically checked (27 tables, 33 indexes, 6 functions, 6 views, balanced plpgsql blocks) but has not been run against a PostgreSQL instance. Treat it as a reviewed design, not a tested migration.
