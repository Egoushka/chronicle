-- ============================================================================
--  Chronicle 002 — retrieval functions and views
--
--  Five query classes, five handlers. Do NOT serve them from one endpoint:
--  "first mention" needs perfect recall and zero LLM reasoning; "how did it
--  evolve" needs stratified coverage and heavy synthesis. Different systems.
-- ============================================================================

-- Hybrid retrieval with Reciprocal Rank Fusion.
-- RRF over weighted score fusion: no normalization needed, robust to the two
-- retrievers' incomparable scales. k=60 is standard.
--
-- The four filter predicates are REPEATED in both branches instead of being
-- factored into a shared `filtered` CTE. That duplication is deliberate and it
-- is the whole performance story of this function.
--
-- A CTE referenced twice is materialized by PostgreSQL, and a CTE scan cannot
-- use an index. Factoring the filters out therefore cost the lexical branch
-- `episode_fts_idx` (001_core.sql:173, 25 MB GIN, expression-identical to the
-- predicate below) and made it recompute to_tsvector() for every substantive
-- episode on every query. Measured on the live archive, 38,736 substantive
-- episodes:
--
--     lexical branch, via the materialized CTE     5,506 ms
--     lexical branch, against the base table           3.2 ms
--     whole function, CTE version                  5,694 ms
--     whole function, this version                   120-255 ms
--
-- Top-20 output is byte-identical between the two; this buys ~45x for nothing.
-- The dense branch is a brute-force scan either way (there is deliberately no
-- ANN index — see ADR-001) and costs ~180 ms, so it is the lexical half that
-- has to reach its index.
CREATE OR REPLACE FUNCTION hybrid_search(
    q_embedding  halfvec(1024),
    q_text       TEXT,
    date_from    TIMESTAMPTZ DEFAULT NULL,
    date_to      TIMESTAMPTZ DEFAULT NULL,
    thread_filter TEXT[]     DEFAULT NULL,
    source_filter TEXT[]     DEFAULT NULL,
    n_candidates INT         DEFAULT 100,
    n_final      INT         DEFAULT 20,
    rrf_k        INT         DEFAULT 60
)
RETURNS TABLE (episode_id BIGINT, rrf_score DOUBLE PRECISION,
               dense_rank INT, lex_rank INT) AS $$
WITH dense AS (
    SELECT e.episode_id,
           row_number() OVER (ORDER BY e.embedding <=> q_embedding)::int AS rnk
    FROM episode e
    WHERE e.is_substantive
      AND e.embedding IS NOT NULL
      AND (date_from     IS NULL OR e.started_at >= date_from)
      AND (date_to       IS NULL OR e.started_at <= date_to)
      AND (thread_filter IS NULL OR e.thread_key = ANY(thread_filter))
      AND (source_filter IS NULL OR e.sources && source_filter)
    ORDER BY e.embedding <=> q_embedding
    LIMIT n_candidates
),
lexical AS (
    SELECT e.episode_id,
           row_number() OVER (
               ORDER BY ts_rank_cd(
                   to_tsvector('ru_unaccent', coalesce(e.lemmatized_text, e.embed_text)),
                   plainto_tsquery('ru_unaccent', q_text)) DESC)::int AS rnk
    FROM episode e
    WHERE e.is_substantive
      -- Keep this expression character-identical to episode_fts_idx or the
      -- planner silently falls back to a sequential scan and the 45x is gone.
      AND to_tsvector('ru_unaccent', coalesce(e.lemmatized_text, e.embed_text))
          @@ plainto_tsquery('ru_unaccent', q_text)
      AND (date_from     IS NULL OR e.started_at >= date_from)
      AND (date_to       IS NULL OR e.started_at <= date_to)
      AND (thread_filter IS NULL OR e.thread_key = ANY(thread_filter))
      AND (source_filter IS NULL OR e.sources && source_filter)
    LIMIT n_candidates
)
SELECT coalesce(d.episode_id, l.episode_id),
       coalesce(1.0/(rrf_k + d.rnk), 0) + coalesce(1.0/(rrf_k + l.rnk), 0),
       d.rnk, l.rnk
FROM dense d FULL OUTER JOIN lexical l USING (episode_id)
ORDER BY 2 DESC
LIMIT n_final;
$$ LANGUAGE sql STABLE;


-- "When did I FIRST mention X" — an ARGMIN, not a top-k.
-- Top-k returns the k most SIMILAR, which are almost never the EARLIEST, and
-- no value of k fixes that: similarity and recency are uncorrelated.
-- Evidence: Test of Time single-fact 91.94% vs Timeline 31.66%.
-- Runs against `event`, NOT `episode` — a first mention is typically a bare
-- token in a 15-char message.
CREATE OR REPLACE FUNCTION first_mention(
    patterns TEXT[],
    n_verify INT DEFAULT 20
)
RETURNS TABLE (source TEXT, source_id TEXT, ts TIMESTAMPTZ,
               text TEXT, thread_key TEXT) AS $$
SELECT e.source, e.source_id, e.ts,
       coalesce(nullif(e.text,''), e.transcript, e.ocr_text),
       e.thread_key
FROM event e
WHERE to_tsvector('ru_unaccent',
        coalesce(e.text,'')||' '||coalesce(e.transcript,'')||' '||coalesce(e.ocr_text,''))
      @@ to_tsquery('ru_unaccent', array_to_string(patterns, ' | '))
   OR e.text ILIKE ANY (SELECT '%'||p||'%' FROM unnest(patterns) p)
ORDER BY e.ts ASC
LIMIT n_verify;
$$ LANGUAGE sql STABLE;


-- Time-stratified retrieval for "how did my view on Y evolve".
-- Top-k optimizes similarity DENSITY, not temporal COVERAGE: three weeks of
-- intense 2022 discussion would consume all 50 slots and 2019 vanishes.
-- ChronoQA: decomposition gave +68% relative on multi-document questions.
-- Returns rows in CHRONOLOGICAL order — Test of Time measured sorted
-- presentation at 71.95% vs shuffled 58.82%.
CREATE OR REPLACE FUNCTION stratified_search(
    q_embedding halfvec(1024),
    bin_width   INTERVAL DEFAULT '3 months',
    per_bin     INT DEFAULT 10
)
RETURNS TABLE (bin_start TIMESTAMPTZ, episode_id BIGINT,
               distance DOUBLE PRECISION, rank_in_bin INT) AS $$
WITH binned AS (
    SELECT e.episode_id,
           to_timestamp(floor(extract(epoch FROM e.started_at)
                        / extract(epoch FROM bin_width))
                        * extract(epoch FROM bin_width)) AS bin_start,
           (e.embedding <=> q_embedding)::double precision AS dist
    FROM episode e
    WHERE e.embedding IS NOT NULL AND e.is_substantive
),
ranked AS (
    SELECT b.*, row_number() OVER (PARTITION BY b.bin_start ORDER BY b.dist)::int AS rnk
    FROM binned b
)
SELECT r.bin_start, r.episode_id, r.dist, r.rnk
FROM ranked r WHERE r.rnk <= per_bin
ORDER BY r.bin_start, r.rnk;
$$ LANGUAGE sql STABLE;


-- Deterministic contradiction resolution. max(version) in SQL, never in a
-- prompt. arXiv:2606.01435: LLM adjudication loses 14 points from 64K->262K
-- context via prior-override and serial-comparison drift.
CREATE OR REPLACE FUNCTION resolve_fact_conflicts()
RETURNS int AS $$
DECLARE n int;
BEGIN
  WITH superseded AS (
      SELECT f.fact_id,
             lead(f.t_valid) OVER (PARTITION BY f.subject_id, f.predicate
                                   ORDER BY f.version) AS next_valid
      FROM fact f
      JOIN fact_predicate fp ON fp.predicate = f.predicate
      WHERE fp.single_valued AND f.t_invalid IS NULL AND f.t_expired IS NULL
  )
  UPDATE fact f SET t_invalid = s.next_valid
  FROM superseded s
  WHERE f.fact_id = s.fact_id AND s.next_valid IS NOT NULL;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$ LANGUAGE plpgsql;


-- Fit per-thread session gaps from the bimodal inter-event distribution.
--
-- Two things this function deliberately does NOT do:
--
--   1. It does not touch non-conversational sources. A wakapi coding session
--      or a dawarich stay is ALREADY an episode — its adapter did the
--      aggregation. Gap-fitting them produces a meaningless number (measured:
--      wakapi p90 = 172,800 s = 2 days, which then clamps to the 6 h ceiling
--      and silently claims to be a session boundary).
--
--   2. It does not hide the clamp. p90 is a crude proxy for the bimodal
--      valley that chronicle.segment.fit_gap_threshold() actually finds, and
--      on real data it overshoots: the synthetic Telegram thread measured
--      p90 = 26,186 s (7.3 h) against a true valley near 11 min. When the
--      fitted value hits a bound, `clamped` records which one, so a degenerate
--      fit is visible in thread_config instead of looking authoritative.
--
-- Prefer the Python fitter. This exists so a fresh database has sane defaults
-- before the first worker run, not as the production path.
CREATE OR REPLACE FUNCTION fit_thread_gaps(min_samples INT DEFAULT 100)
RETURNS int AS $$
DECLARE n int;
BEGIN
  WITH gaps AS (
      SELECT e.thread_key, e.source,
             extract(epoch FROM e.ts - lag(e.ts) OVER (PARTITION BY e.thread_key ORDER BY e.ts)) AS gap
      FROM event e
      JOIN source s ON s.source = e.source
      WHERE s.conversational          -- <- only sources that HAVE sessions
  ),
  stats AS (
      SELECT thread_key, min(source) AS source, count(*) AS n,
             percentile_cont(0.50) WITHIN GROUP (ORDER BY gap) AS p50,
             percentile_cont(0.75) WITHIN GROUP (ORDER BY gap) AS p75,
             percentile_cont(0.90) WITHIN GROUP (ORDER BY gap) AS p90
      FROM gaps WHERE gap IS NOT NULL AND gap > 0
      GROUP BY thread_key
      HAVING count(*) >= min_samples
  ),
  bounded AS (
      SELECT s.*,
             -- p75 rather than p90: p90 sits well into the between-burst mode
             -- on real IM data, so it over-merges distinct conversations.
             GREATEST(600, LEAST(21600, round(s.p75)::int)) AS fitted,
             CASE WHEN round(s.p75) < 600   THEN 'low'
                  WHEN round(s.p75) > 21600 THEN 'high'
                  ELSE NULL END AS clamped
      FROM stats s
  )
  INSERT INTO thread_config (thread_key, source, gap_seconds, gap_fit_stats, fitted_at)
  SELECT b.thread_key, b.source, b.fitted,
         jsonb_build_object('n', b.n, 'p50', b.p50, 'p75', b.p75, 'p90', b.p90,
                            'clamped', b.clamped, 'method', 'sql-p75-fallback'),
         now()
  FROM bounded b
  ON CONFLICT (thread_key) DO UPDATE
     SET gap_seconds = EXCLUDED.gap_seconds,
         gap_fit_stats = EXCLUDED.gap_fit_stats,
         fitted_at = EXCLUDED.fitted_at;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
--  VIEWS — calendar rollups are views, never tables.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_daily AS
SELECT date_trunc('day', ts)::date AS day, source, thread_key,
       count(*) AS events, sum(char_len) AS chars
FROM event GROUP BY 1,2,3;

CREATE OR REPLACE VIEW v_monthly_topics AS
SELECT date_trunc('month', e.started_at)::date AS month, t AS topic,
       count(*) AS episodes, avg(e.sentiment) AS mean_sentiment,
       avg(e.importance) AS mean_importance
FROM episode e, unnest(e.topics) AS t GROUP BY 1,2;

-- Current facts with deterministic max(version) resolution, in SQL.
CREATE OR REPLACE VIEW v_current_facts AS
SELECT DISTINCT ON (subject_id, predicate)
       fact_id, subject_id, predicate, object_text, object_id,
       t_valid, version, confidence, source_event_ids
FROM fact
WHERE t_invalid IS NULL AND t_expired IS NULL AND polarity = 1
ORDER BY subject_id, predicate, version DESC;

CREATE OR REPLACE VIEW v_forgotten_commitments AS
SELECT c.*, p.display_name AS counterparty, now() - c.stated_at AS age
FROM commitment c LEFT JOIN person p ON p.person_id = c.committee_id
WHERE c.status = 'open' AND c.stated_at < now() - interval '90 days'
ORDER BY c.stated_at;

-- On-this-day. Highest value per line of code in the system.
-- Excludes episodes overlapping a sensitive life_event.
CREATE OR REPLACE VIEW v_on_this_day AS
SELECT e.*
FROM episode e
WHERE extract(month FROM e.started_at) = extract(month FROM now())
  AND extract(day   FROM e.started_at) = extract(day   FROM now())
  AND e.started_at < date_trunc('year', now())
  AND e.is_substantive
  AND NOT EXISTS (
      SELECT 1 FROM life_event le
      WHERE le.is_sensitive
        AND e.started_at BETWEEN le.started_at
                             AND coalesce(le.ended_at, le.started_at + interval '30 days'))
ORDER BY e.importance DESC NULLS LAST;

-- Facts eligible for promotion into Hindsight. Deliberately narrow: recurs
-- across >=3 episodes AND >=2 threads. Target hundreds/year, not thousands —
-- the `personal` bank is already at 2,726 facts and times out on sync_retain.
CREATE OR REPLACE VIEW v_promotable_facts AS
SELECT f.fact_id, f.subject_id, f.predicate, f.object_text, f.t_valid,
       f.confidence, f.source_event_ids,
       count(DISTINCT em.episode_id) AS episode_support,
       count(DISTINCT ep.thread_key) AS thread_support
FROM fact f
JOIN entity_mention em ON em.entity_id = f.subject_id
JOIN episode ep ON ep.episode_id = em.episode_id
WHERE f.promoted_at IS NULL
  AND f.t_invalid IS NULL AND f.t_expired IS NULL
  AND f.confidence >= 0.7
GROUP BY f.fact_id, f.subject_id, f.predicate, f.object_text, f.t_valid,
         f.confidence, f.source_event_ids
HAVING count(DISTINCT em.episode_id) >= 3 AND count(DISTINCT ep.thread_key) >= 2;
