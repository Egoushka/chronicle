# Roadmap

Each goal is written as what is true when it is done. The version counts
them: **v0.4.2** means four goals done and two fixes since. Finishing a goal
is a minor release; 1.0 is the end of the first stage. What changed in each
release: [CHANGELOG.md](CHANGELOG.md).

Order is deliberate. The project's design rule is that chronicle must beat
`grep` on its owner's own questions before it grows, so everything that adds
surface waits behind the goals that measure retrieval.

Legend: ✅ done · ⏳ in progress · ▶ next · · later

---

## Stage 0 — Worth using over grep, for its owner (v0.x)

1. ✅ **It runs on one host, on a schedule, and is public.** Ingest, segment,
   embed and serve nightly; the eval harness and erasure path exist. (v0.1.0)
2. ✅ **A deployment says which release it runs.** One version in the
   package, the changelog and `/health`; releases are tagged; a host runs a
   tag, not a working tree. (v0.2.0)
3. ⏳ **No secret reaches the index, the MCP or an LLM.** Redacted at ingest
   for every source; what is already stored is rewritten by a backfill. This
   is what gates the tier-3 sources (documents, bookmarks, photos).
4. ✅ **Chronicle beats ripgrep by 10+ points on the owner's questions,
   measured honestly.** Scored in each system's own unit; grep gets only the
   question's words, as chronicle does; 70+ labelled questions. 71 questions:
   chronicle 71.1% vs ripgrep 54.2% (lookup 67.0% vs 41.8%). (v0.3.0)
5. ✅ **Segment size is measured, not assumed.** The 30-event cap is swept
   (`make resegment`) on the threads the eval cites, and the archive is
   rebuilt at the winner. Swept on 46 eval threads: 15 -> 73.2%, 20 ->
   71.6%, 30 -> 71.1%. Archive rebuilt at 15: 75.4% overall, lookup 75.5%
   (was 67.0%). (v0.4.0)
6. · **The remaining lookup misses are explained.** Diagnosed 2026-09-29: of
   7, three rank 33-80 (a reranker could reach them), one sits in a segment
   filtered as non-substantive, and three are in chats the source never
   named, so the answer's text holds no word of the question.
7. · **Enrichment is on or deleted, decided by an A/B.** The one run so far
   lowered the score; test facts in the returned hit instead of in the
   embedded text.
8. · **A source that goes silent raises an alert.** Today only `doctor`'s
   "nothing in 90 days" notices, and location and photos died unnoticed.

## Stage 1 — Someone else can run it (v1.x)

- · A fresh host goes from `git clone` to a first answer by following the
  README alone, and CI proves it.
- · Releases publish container images; upgrades are pull + migrate.
- · The calendar source (OAuth) works end to end.
- · Facts that recur across threads are promoted to a curated memory, at
  hundreds a year, not thousands.

## Could have

Worth doing if a measurement asks for it; not scheduled.

- An approximate index (HNSW) once the dense scan stops being ~0.4 s.
- Late backfills of old chats merged into the segments they fall between,
  instead of segmented among themselves.
- A `ground` tool: attach real conversations to a curated memory's claims.
- A location source that does not depend on a phone app.
- Work-tracker sources — a policy decision before a technical one, since the
  data belongs to an employer.

## Will not do

Settled, with the numbers in [CLAUDE.md](CLAUDE.md#design-rules-that-are-not-negotiable):

- Index individual messages.
- Build a summary pyramid.
- Train a query router, or let an LLM decide which of two facts is newer.
- Apply a global recency prior.
- Keep a resident local LLM on the host.
