-- ============================================================================
--  Chronicle 003 — rename the aggregate unit: `episode` -> `segment`
--
--  WHY: JARVIS calls ONE Telegram message an episode. Chronicle called a
--  time-gap cluster of ~13 of them an episode. Two live repos, same word,
--  opposite granularity. Chronicle moves because docs/RESEARCH.md already
--  calls this "segment-level" (SeCom's term) and segment.py is already named
--  for it, so `segment` is the more faithful name here.
--
--  WHAT THIS IS NOT: a data migration. Every statement below is catalog-only.
--  ALTER TABLE/COLUMN/INDEX/SEQUENCE ... RENAME rewrites no heap and no index,
--  so all 51,044 rows and 274 MB of halfvec are untouched and NOTHING is
--  re-embedded. That matters: re-embedding this corpus is ~9.8 h of CPU.
--
--  SHAPE: guarded and idempotent. On a fresh database 001 has already created
--  the `segment` world, so this whole file is a no-op — which is what makes it
--  safe to leave in the 001/002/003 sequence that `make migrate` and the
--  compose initdb mount both run.
--
--  ON A LIVE DATABASE THIS IS STEP 1 OF 2. It DROPS hybrid_search and
--  stratified_search, because both name `segment_id` in RETURNS TABLE and
--  PostgreSQL cannot rename an OUT parameter with CREATE OR REPLACE. Their
--  definitions live in 002_retrieval.sql and are deliberately NOT duplicated
--  here, so re-apply 002 immediately afterwards:
--
--      make migrate-rename          # runs 003 then 002, in that order
--
--  Running 003 alone leaves the database without its two retrieval functions.
--  The final block below raises a loud error rather than letting that pass
--  silently.
-- ============================================================================

DO $$
BEGIN
    IF to_regclass('public.episode') IS NULL THEN
        RAISE NOTICE '003: nothing to do — schema already uses `segment`.';
        RETURN;
    END IF;

    -- The two SQL functions store their bodies as text and re-parse at
    -- execution, so after the table rename they would fail at runtime with
    -- "relation episode does not exist". Drop them here; 002 recreates them.
    DROP FUNCTION IF EXISTS hybrid_search(halfvec, TEXT, TIMESTAMPTZ, TIMESTAMPTZ,
                                          TEXT[], TEXT[], INT, INT, INT);
    DROP FUNCTION IF EXISTS stratified_search(halfvec, INTERVAL, INT);

    -- ---- the table, and every column carrying the old noun -----------------
    ALTER TABLE episode        RENAME COLUMN episode_id            TO segment_id;
    ALTER TABLE episode        RENAME TO segment;

    ALTER TABLE entity_mention RENAME COLUMN episode_id            TO segment_id;
    ALTER TABLE fact           RENAME COLUMN source_episode_id     TO source_segment_id;
    ALTER TABLE commitment     RENAME COLUMN resolution_episode_id TO resolution_segment_id;
    ALTER TABLE commitment     RENAME COLUMN source_episode_id     TO source_segment_id;

    -- ---- implicit objects ---------------------------------------------------
    -- These are the classic miss: BIGSERIAL and PRIMARY KEY invent names that
    -- appear NOWHERE in the migration source, and ALTER TABLE ... RENAME TO
    -- does not touch them. Left alone they are the only thing in the database
    -- still saying "episode", and they resurface years later in an error
    -- message that no longer matches any identifier in the codebase.
    ALTER SEQUENCE episode_episode_id_seq RENAME TO segment_segment_id_seq;

    ALTER INDEX episode_pkey           RENAME TO segment_pkey;
    ALTER INDEX episode_fts_idx        RENAME TO segment_fts_idx;
    ALTER INDEX episode_parts_idx      RENAME TO segment_parts_idx;
    ALTER INDEX episode_sources_idx    RENAME TO segment_sources_idx;
    ALTER INDEX episode_thread_idx     RENAME TO segment_thread_idx;
    ALTER INDEX episode_topics_idx     RENAME TO segment_topics_idx;
    ALTER INDEX episode_ts_idx         RENAME TO segment_ts_idx;
    ALTER INDEX episode_unenriched_idx RENAME TO segment_unenriched_idx;

    ALTER TABLE segment    RENAME CONSTRAINT episode_time_order TO segment_time_order;
    ALTER TABLE entity_mention RENAME CONSTRAINT entity_mention_episode_id_fkey
                                              TO entity_mention_segment_id_fkey;
    ALTER TABLE fact       RENAME CONSTRAINT fact_source_episode_id_fkey
                                          TO fact_source_segment_id_fkey;
    ALTER TABLE commitment RENAME CONSTRAINT commitment_resolution_episode_id_fkey
                                          TO commitment_resolution_segment_id_fkey;
    ALTER TABLE commitment RENAME CONSTRAINT commitment_source_episode_id_fkey
                                          TO commitment_source_segment_id_fkey;

    -- ---- view OUTPUT columns ------------------------------------------------
    -- A view records its own column names at creation. Renaming a base-table
    -- column does NOT propagate (views bind by attribute number), so a view
    -- built with `SELECT e.*` keeps emitting `episode_id` against a table that
    -- no longer has one. ALTER VIEW ... RENAME COLUMN avoids a DROP/CREATE and
    -- keeps every dependency intact.
    ALTER VIEW v_on_this_day             RENAME COLUMN episode_id    TO segment_id;
    ALTER VIEW v_monthly_topics          RENAME COLUMN episodes      TO segments;
    ALTER VIEW v_promotable_facts        RENAME COLUMN episode_support
                                                                     TO segment_support;
    ALTER VIEW v_forgotten_commitments   RENAME COLUMN resolution_episode_id
                                                                     TO resolution_segment_id;
    ALTER VIEW v_forgotten_commitments   RENAME COLUMN source_episode_id
                                                                     TO source_segment_id;

    RAISE NOTICE '003: renamed episode -> segment (catalog only, 0 rows rewritten).';
    RAISE NOTICE '003: hybrid_search and stratified_search were DROPPED. '
                 'Re-apply 002_retrieval.sql NOW, or use `make migrate-rename`.';
END $$;


-- WARNING, deliberately not EXCEPTION. This file drops the two functions
-- itself, so the condition below is TRUE on every successful upgrade — raising
-- here would abort `make migrate-rename` with ON_ERROR_STOP before it ever got
-- to step 2, which is the opposite of the intent. The hard post-condition
-- lives in the Makefile target, after 002 has been re-applied.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'hybrid_search') THEN
        RAISE WARNING
            'hybrid_search is now MISSING. Re-apply migrations/002_retrieval.sql '
            'before serving traffic, or the API answers /health and fails every '
            '/recall (see hard-won fact 20 for how invisible that failure is).';
    END IF;
END $$;
