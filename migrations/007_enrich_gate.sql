-- Pre-extraction gate score (chronicle/gate.py).
--
-- gate_p is the gate's probability that extraction would keep something from
-- this segment; gate_version names the backend, model and question that
-- produced it, so a changed question or model re-scores instead of reusing a
-- stale number. Both are NULL until the gate has seen the segment, and reset
-- when the segment's text changes (`_update_segment`).
--
-- Idempotent: `make migrate` re-applies every file.

ALTER TABLE segment ADD COLUMN IF NOT EXISTS gate_p       REAL;
ALTER TABLE segment ADD COLUMN IF NOT EXISTS gate_version TEXT;
