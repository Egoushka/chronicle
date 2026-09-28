-- Incremental maintenance: the worker stops being a one-shot backfill.
--
-- Until 2026-09-26 every stage assumed an empty database. `segment` re-read
-- every thread and INSERTed with no conflict target, so a second run would
-- have duplicated all 50,096 segments; nothing re-ran it, and the 4,711
-- events ingested on 2026-09-14 were never segmented at all. Ingest skipped
-- rows it had already seen, so 4,544 voice notes transcribed after the first
-- ingest could never reach the index.
--
-- Idempotent: `make migrate` re-applies every file.

-- "Which segments cite this event?" — asked by ingest whenever an event's text
-- changes upstream (a transcript landing, a message edited), so the segment is
-- rebuilt and re-embedded. Without the index that is a sequential scan of
-- every segment's array for every changed batch.
CREATE INDEX IF NOT EXISTS segment_source_events_idx
    ON segment USING GIN (source_event_ids);

-- The closed vocabulary `enrich` may emit. A free-text predicate would make
-- resolve_fact_conflicts() useless: `lives_in`, `lives in`, `moved_to` and
-- `city` would be four predicates, none ever superseding another, and every
-- address the owner ever had would read as current. Extend here, not in a prompt.
INSERT INTO fact_predicate (predicate, description, single_valued) VALUES
    ('lives_in',        'city or country of residence',                TRUE),
    ('works_at',        'employer or company',                        TRUE),
    ('job_title',       'role or position',                           TRUE),
    ('studies_at',      'school, university or course provider',      TRUE),
    ('relationship',    'relationship status or partner',             TRUE),
    ('owns',            'a possession: car, device, pet, property',   FALSE),
    ('likes',           'a stated preference or enjoyment',           FALSE),
    ('dislikes',        'a stated aversion',                          FALSE),
    ('plans',           'an intention or plan for the future',        FALSE),
    ('visited',         'a place travelled to or visited',            FALSE),
    ('bought',          'a purchase',                                 FALSE),
    ('learned',         'a skill, topic or technology studied',       FALSE),
    ('uses',            'a tool, service or technology in use',       FALSE),
    ('health',          'a health condition, injury or treatment',    FALSE),
    ('opinion',         'a stated opinion or belief',                 FALSE),
    ('knows',           'an acquaintance or relationship to a person', FALSE)
ON CONFLICT (predicate) DO NOTHING;
