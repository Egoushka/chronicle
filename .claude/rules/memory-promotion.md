---
paths:
  - "chronicle/facts.py"
  - "migrations/008_promotion_bankless.sql"
---
Split from CLAUDE.md, loaded when matching files are touched: Relationship to a curated memory (Hindsight).

## Relationship to a curated memory (Hindsight)

Opposites, which is why they compose. Hindsight: ~5,200 curated facts, no
evidence trail — a notebook. Chronicle: 681k events, nothing but evidence — a
recording.

**Chronicle must not flood Hindsight.** The reference deployment's largest bank
is already at 2,726 facts and times out on `sync_retain`. Piping ~50k segments of facts into it
would 40× the bank and make `recall` useless.

- **`ground` (Chronicle → answer), every recall.** Attach real conversations to
  Hindsight's unsourced claims, including contradicting ones. Replaces
  time-based staleness rules with a measurement.
- **Promotion (Chronicle → Hindsight), rare.** `v_promotable_facts` requires
  ≥3 segments AND ≥2 threads at confidence ≥0.7. Target hundreds/year. Check
  `get_bank_stats` after each run.
