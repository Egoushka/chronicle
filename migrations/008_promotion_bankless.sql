-- Facts never promote out of a source that has no Hindsight bank.
--
-- `source.hindsight_bank` is NULL for a source whose content must stay in
-- chronicle (sources.py: nytka, miniflux, owntracks). The view that lists
-- promotable facts did not look at it, so a fact about a person who only ever
-- spoke near the owner could pass the "3 segments, 2 threads" test: every
-- Nytka conversation is its own thread, which makes the second condition easy.
--
-- Two exclusions, both through the `source` table so a new bankless source is
-- covered the day its policy row is written:
--   * a fact whose evidence (source_event_ids, source-prefixed) includes an
--     event of a bankless source;
--   * a segment of a bankless source as SUPPORT for any fact, so it cannot
--     lift a Telegram fact over the threshold either.
--
-- Idempotent: `make migrate` re-applies every file.

CREATE OR REPLACE VIEW v_promotable_facts AS
SELECT f.fact_id, f.subject_id, f.predicate, f.object_text, f.t_valid,
       f.confidence, f.source_event_ids,
       count(DISTINCT em.segment_id) AS segment_support,
       count(DISTINCT seg.thread_key) AS thread_support
FROM fact f
JOIN entity_mention em ON em.entity_id = f.subject_id
JOIN segment seg ON seg.segment_id = em.segment_id
WHERE f.promoted_at IS NULL
  AND f.t_invalid IS NULL AND f.t_expired IS NULL
  AND f.confidence >= 0.7
  AND NOT EXISTS (SELECT 1
                    FROM unnest(f.source_event_ids) ev
                    JOIN source bs ON bs.source = split_part(ev, ':', 1)
                   WHERE bs.hindsight_bank IS NULL)
  AND NOT EXISTS (SELECT 1
                    FROM unnest(seg.sources) sn
                    JOIN source bs ON bs.source = sn
                   WHERE bs.hindsight_bank IS NULL)
GROUP BY f.fact_id, f.subject_id, f.predicate, f.object_text, f.t_valid,
         f.confidence, f.source_event_ids
HAVING count(DISTINCT em.segment_id) >= 3 AND count(DISTINCT seg.thread_key) >= 2;
