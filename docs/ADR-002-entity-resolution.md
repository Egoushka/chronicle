# ADR-002 — Two-tier cross-script entity resolution

**Status:** accepted · **Date:** 2026-07-25

## Context

«Егор» / «Єгор» / "Yehor" / "Egor" are one person. The corpus is Russian,
Ukrainian and English, code-switched, with transliteration and slang.

Graphiti — the reference implementation for this problem — resolves entities
with cosine similarity → BM25 over entity names → LLM adjudication. **The BM25
stage fails entirely across scripts**, because these strings share no lexical
surface at all.

## What failed first

A straight ISO-9 transliteration, which is what the literature implies. It
produced **four distinct keys for one person**:

```
Егор  -> ehor      Yehor -> yehor
Єгор  -> iehor     Egor  -> egor
```

Latin input passes through untransliterated, RU `г`→h vs UK `ґ`→g, `є`→ie vs
ye. It does not work.

## Decision

Two tiers, both cheap, both blocking-only.

**Tier 1 — coarse phonetic key.** Transliterate, then collapse the equivalence
classes RU, UK and Latin romanizations disagree about: `ye|ie|je→e`, `kh→h`,
`ts→c`, `y|j→i`, leading `h|g→g`, doubled letters, trailing vowels. All five
spellings of Yehor → `egor`. Handles person names.

**Tier 2 — consonant skeleton.** Drop all vowels. Required because Russian and
Ukrainian differ by *systematic vowel alternation*, not spelling convention:

```
Киев / Kiev     -> kev        Київ   / Kyiv    -> kiv        both -> kv
Харьков         -> garkov     Харків / Kharkiv -> garkiv     both -> grkv
```

No transliteration table fixes that. Dropping vowels does.

**Minimum length 2.** `Аня` → `n` is a 1-character skeleton that collides with
everything. The threshold is 2 rather than 3 because `Київ` → `kv` is precisely
the case tier 2 exists for.

## Constraints

**Tier 2 deliberately over-merges. It is a candidate GENERATOR, never a
decision.** Confirm with embedding similarity, then an LLM only if still
ambiguous.

**Blocking by chat and time window is a correctness requirement, not an
optimization.** A-MEM's 15-hour build at LoCoMo scale is what unblocked O(N²)
resolution looks like, on a corpus ~900× smaller than this one. Any algorithm
comparing each new item against all existing ones dies between 10⁴ and 10⁵.

**None of this applies to the 457 people.** Telegram's `sender_id` is ground
truth the literature does not have. Use it and skip the problem entirely; this
ADR is for entities named *in text*.

## Verification

`tests/test_resolve.py`: five variant groups collapse correctly, nine real
chat-list names stay distinct, the short-name guard holds, blocking works.
