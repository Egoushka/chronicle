---
paths:
  - "chronicle/worker.py"
  - "chronicle/enrich.py"
  - "chronicle/gate.py"
  - "chronicle/prompts.py"
  - "chronicle/adapters/telegram.py"
  - "tests/test_enrich.py"
  - "tests/test_gate.py"
  - "tests/test_prompts.py"
  - "scripts/worker-itest.py"
  - "scripts/nightly.sh"
---
Split from CLAUDE.md, loaded when matching files are touched: Worker pipeline and enrichment: hard-won facts.

37. **telegram resumes on `synced_at`, never on the message date.**
   telegram-sync UPSERTs, and its `DO UPDATE` rewrites `text` and `synced_at`
   together, so write time is the only watermark that sees a transcript
   landing on an old voice note, an edit, or a late backfill. On the date
   watermark, 4,544 transcribed notes could never reach chronicle (4,637 empty
   events against 93 empty source rows). `synced_at` is stamped BEFORE
   telegram-sync's batch commits, so resume re-reads a 10-minute overlap
   (`SYNC_LAG`), and the bound always carries six fractional digits —
   `isoformat()` drops them at microsecond 0, and `'+'` sorts below `'.'`.
38. **The worker must be safe to run twice.** `segment` used to re-read every
   thread and INSERT with no conflict target: right exactly once, a full
   duplicate of 50,096 segments the second time. So nothing re-ran it and the
   index froze at 2026-08-11. It now segments only unsegmented events and
   continues a thread's last segment IN PLACE (same `segment_id`, embedding
   and enrichment cleared), and ingest upserts and rewrites any segment citing
   a changed event. `scripts/worker-itest.py` runs the pipeline five times
   against a real database; it is the only test that can see this.
42. **Copied secrets die with their source's rotation.** chronicle's
   `LITELLM_API_KEY` was copied from telegram-sync at first deploy; the
   2026-09-23 master-key rotation (deploy repo #98) gave each consumer its
   own virtual key and left chronicle's copy returning 401. Nothing noticed,
   because only `enrich` uses it. chronicle needs its own `chronicle` key.
