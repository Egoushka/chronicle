-- Enrichment as a third retrieval list, not as embedded text (roadmap goal 7).
--
-- The one enrich run so far wrote summary, topics and facts INTO embed_text and
-- the eval fell 55.0% -> 52.3%. This keeps the embedding and the lexical index
-- of the raw text exactly as they were and adds the enrichment as its own
-- lexical list, fused by RRF only when a caller asks (`use_enrich`), so the
-- same index can be scored with and without it.
--
-- Idempotent: `make migrate` re-applies every file.

ALTER TABLE segment ADD COLUMN IF NOT EXISTS enrich_text TEXT;

CREATE INDEX IF NOT EXISTS segment_enrich_fts_idx ON segment USING GIN (
    to_tsvector('ru_unaccent', coalesce(enrich_text, '')))
    WHERE enrich_text IS NOT NULL;

DROP FUNCTION IF EXISTS hybrid_search(halfvec, text, timestamptz, timestamptz,
                                      text[], text[], int, int, int);

CREATE OR REPLACE FUNCTION hybrid_search(
    q_embedding  halfvec(1024),
    q_text       TEXT,
    date_from    TIMESTAMPTZ DEFAULT NULL,
    date_to      TIMESTAMPTZ DEFAULT NULL,
    thread_filter TEXT[]     DEFAULT NULL,
    source_filter TEXT[]     DEFAULT NULL,
    n_candidates INT         DEFAULT 100,
    n_final      INT         DEFAULT 20,
    rrf_k        INT         DEFAULT 60,
    use_enrich   BOOLEAN     DEFAULT FALSE
)
RETURNS TABLE (segment_id BIGINT, rrf_score DOUBLE PRECISION,
               dense_rank INT, lex_rank INT) AS $$
WITH dense AS (
    SELECT e.segment_id,
           row_number() OVER (ORDER BY e.embedding <=> q_embedding)::int AS rnk
    FROM segment e
    WHERE e.is_substantive
      AND e.embedding IS NOT NULL
      AND (date_from     IS NULL OR e.started_at >= date_from)
      AND (date_to       IS NULL OR e.started_at <= date_to)
      AND (thread_filter IS NULL OR e.thread_key = ANY(thread_filter))
      AND (source_filter IS NULL OR e.sources && source_filter)
    ORDER BY e.embedding <=> q_embedding
    LIMIT n_candidates
),
kept AS (
    SELECT d.word, ln(m.ndocs::float / d.ndoc) AS idf
    FROM unnest(tsvector_to_array(to_tsvector('ru_unaccent', q_text))) AS w(word)
    JOIN lexeme_df d USING (word)
    CROSS JOIN lexeme_df_meta m
    WHERE d.ndoc < 0.05 * m.ndocs
),
lexical AS (
    SELECT e.segment_id,
           row_number() OVER (ORDER BY sum(k.idf) DESC, e.segment_id)::int AS rnk
    FROM segment e
    JOIN kept k
      ON to_tsvector('ru_unaccent', coalesce(e.lemmatized_text, e.embed_text))
         @@ quote_literal(k.word)::tsquery
    WHERE e.is_substantive
      AND (date_from     IS NULL OR e.started_at >= date_from)
      AND (date_to       IS NULL OR e.started_at <= date_to)
      AND (thread_filter IS NULL OR e.thread_key = ANY(thread_filter))
      AND (source_filter IS NULL OR e.sources && source_filter)
    GROUP BY e.segment_id
    ORDER BY rnk
    LIMIT n_candidates
),
-- The question's terms over the enrichment text only. Empty unless asked.
-- A word the raw text never uses is absent from lexeme_df; that is exactly
-- what an enrichment adds (a paraphrase), so it counts as rare, not as noise.
kept_e AS (
    SELECT w.word, ln(m.ndocs::float / greatest(coalesce(d.ndoc, 1), 1)) AS idf
    FROM unnest(tsvector_to_array(to_tsvector('ru_unaccent', q_text))) AS w(word)
    LEFT JOIN lexeme_df d USING (word)
    CROSS JOIN lexeme_df_meta m
    WHERE use_enrich AND (d.ndoc IS NULL OR d.ndoc < 0.05 * m.ndocs)
),
enriched AS (
    SELECT e.segment_id,
           row_number() OVER (ORDER BY sum(k.idf) DESC, e.segment_id)::int AS rnk
    FROM segment e
    JOIN kept_e k
      ON to_tsvector('ru_unaccent', coalesce(e.enrich_text, ''))
         @@ quote_literal(k.word)::tsquery
    WHERE use_enrich
      AND e.enrich_text IS NOT NULL
      AND e.is_substantive
      AND (date_from     IS NULL OR e.started_at >= date_from)
      AND (date_to       IS NULL OR e.started_at <= date_to)
      AND (thread_filter IS NULL OR e.thread_key = ANY(thread_filter))
      AND (source_filter IS NULL OR e.sources && source_filter)
    GROUP BY e.segment_id
    ORDER BY rnk
    LIMIT n_candidates
)
SELECT coalesce(d.segment_id, l.segment_id, n.segment_id),
       coalesce(1.0/(rrf_k + d.rnk), 0) + coalesce(1.0/(rrf_k + l.rnk), 0)
         + coalesce(1.0/(rrf_k + n.rnk), 0),
       d.rnk, l.rnk
FROM dense d
FULL OUTER JOIN lexical l USING (segment_id)
FULL OUTER JOIN enriched n USING (segment_id)
ORDER BY 2 DESC
LIMIT n_final;
$$ LANGUAGE sql STABLE;
