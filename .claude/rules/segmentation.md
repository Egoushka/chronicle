---
paths:
  - "chronicle/segment.py"
  - "chronicle/resolve.py"
  - "tests/test_segment.py"
  - "tests/test_resolve.py"
  - "scripts/resegment-itest.py"
---
Split from CLAUDE.md, loaded when matching files are touched: Segmentation and entity resolution: hard-won facts.

6. **`is_substantive` filtering is NARRATIVE-only.** The filler-burst heuristic
   ("a burst of `ок` is not a memory") marked every telemetry segment false and
   hid them from every query.
7. **Cross-script entity resolution needs two tiers.** Straight ISO-9
   transliteration gives four keys for one person (ehor/iehor/yehor/egor).
   Tier 1 coarse phonetic handles names; tier 2 consonant skeleton is required
   because RU/UK differ by systematic vowel alternation (Киев→`kev` vs
   Київ→`kiv`, both →`kv`). Minimum skeleton length is **2**, not 3 — `Київ`→
   `kv` is the case it exists for. See `docs/ADR-002`.
8. **`_merge_runts` must stay O(n).** An earlier version called
   `segments.index(s)` in a loop — quadratic, and it matched by dataclass
   equality so identical segments resolved to the same index.
9. **`fit_thread_gaps` must skip non-conversational sources.** wakapi's p90 gap
   is 2 days, which clamps to the 6h ceiling and then claims to be a session
   boundary. The SQL fallback also records `clamped` so a degenerate fit is
   visible rather than authoritative.
44. **Every two-message segment was hidden from `/recall`.** `_substantive()`
   let a short segment through only if it was ONE message over 80 chars, so
   2,812 two-message telegram segments were non-substantive however long —
   525 of them 200+ chars, and 5 of the 57 gold messages in the first eval.
   Short segments now pass on content. A filter tuned against filler bursts
   has to be checked against the SHORT exchanges that matter most.
48. **The reply-edge rule never ran.** `segment_chat` suppresses a split when
   a reply points back into the open segment, and the worker built every
   Event with `reply_to_id=None`, so none of the 7.9% of messages carrying a
   reply ever did. `event.reply_to` and `event.source_id` are both
   `{chat}:{msg}` for telegram and join directly. A rule with no caller looks
   exactly like a rule that works — `resegment-itest` fails if it goes dark.
