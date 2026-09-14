-- ============================================================================
--  Chronicle 001 — core schema
--
--  Generalized from the Telegram-specific design: raw_message -> event,
--  session -> segment. Everything else (bi-temporal facts, entities,
--  commitments, refcounting, star schema) is unchanged and source-agnostic.
--
--  Principles:
--    1. `event` is IMMUTABLE and APPEND-ONLY. Only ground truth.
--    2. Everything else is a DERIVED PROJECTION: versioned, droppable.
--    3. Every projection carries source_event_ids and a refcount.
--    4. Facts are BI-TEMPORAL. Invalidate, never delete.
--    5. Calendar rollups are VIEWS. Summarizing a summary is how archives rot.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE TEXT SEARCH CONFIGURATION ru_unaccent ( COPY = russian );
ALTER TEXT SEARCH CONFIGURATION ru_unaccent
  ALTER MAPPING FOR hword, hword_part, word WITH unaccent, russian_stem;

-- ---------------------------------------------------------------------------
--  LAYER 0 — immutable event spine
-- ---------------------------------------------------------------------------

-- 18 channels are implemented. `density` is the load-bearing column: it
-- decides how hard a source is aggregated BEFORE anything reaches the segment
-- layer. Turning on every source without it reproduces the original mistake
-- (indexing 681k sub-20-char messages) one level up, as the wrong source mix.
CREATE TABLE source (
    source        TEXT PRIMARY KEY,
    density       TEXT NOT NULL DEFAULT 'discrete'
                  CHECK (density IN ('narrative','discrete','telemetry','ambient')),
    tier          SMALLINT NOT NULL DEFAULT 3,   -- 1 core .. 4 ambient
    -- Only NARRATIVE sources get time-gap segmentation. Telemetry arrives
    -- pre-aggregated from its adapter; gap-fitting it yields nonsense
    -- (measured: wakapi p90 = 2 days, which clamps to the 6h ceiling).
    conversational BOOLEAN GENERATED ALWAYS AS (density = 'narrative') STORED,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    hindsight_bank TEXT,                         -- where promoted facts route
    last_ingested_at TIMESTAMPTZ,                -- resume point after an OOM kill
    last_error    TEXT,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- AMBIENT sources are stored but kept off the default retrieval surface.
CREATE INDEX source_retrievable_idx ON source (source)
    WHERE enabled AND density <> 'ambient';

CREATE TABLE person (
    person_id     BIGSERIAL PRIMARY KEY,
    display_name  TEXT NOT NULL,
    -- Telegram sender_id is GROUND TRUTH for identity. The literature has to
    -- solve cross-script resolution («Егор»/"Yehor"/«Єгор») with cosine +
    -- BM25 + an LLM, and the BM25 stage fails entirely across scripts.
    -- For 457 people we skip the whole problem.
    telegram_user_id BIGINT UNIQUE,
    aliases       TEXT[] NOT NULL DEFAULT '{}',
    phonetic_keys TEXT[] NOT NULL DEFAULT '{}',
    skeleton_keys TEXT[] NOT NULL DEFAULT '{}',
    is_self       BOOLEAN NOT NULL DEFAULT FALSE,
    relationship  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX person_phonetic_idx ON person USING GIN (phonetic_keys);
CREATE INDEX person_skeleton_idx ON person USING GIN (skeleton_keys);

CREATE TABLE event (
    event_id      BIGSERIAL,
    source        TEXT        NOT NULL REFERENCES source(source),
    source_id     TEXT        NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    kind          TEXT,
    actor         TEXT,
    person_id     BIGINT      REFERENCES person(person_id),
    text          TEXT        NOT NULL DEFAULT '',
    payload       JSONB       NOT NULL DEFAULT '{}',
    reply_to      TEXT,
    thread_key    TEXT        NOT NULL,      -- chat_id / project / device

    -- Derived media text. The ONE exception to immutability: append-only
    -- enrichment of content that was always there, not a rewrite.
    transcript    TEXT,
    ocr_text      TEXT,

    char_len      INT GENERATED ALWAYS AS (length(text)) STORED,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, source_id, ts)
) PARTITION BY RANGE (ts);

-- Telegram corpus starts 2018-12-30.
CREATE TABLE event_2018 PARTITION OF event FOR VALUES FROM ('2018-01-01') TO ('2019-01-01');
CREATE TABLE event_2019 PARTITION OF event FOR VALUES FROM ('2019-01-01') TO ('2020-01-01');
CREATE TABLE event_2020 PARTITION OF event FOR VALUES FROM ('2020-01-01') TO ('2021-01-01');
CREATE TABLE event_2021 PARTITION OF event FOR VALUES FROM ('2021-01-01') TO ('2022-01-01');
CREATE TABLE event_2022 PARTITION OF event FOR VALUES FROM ('2022-01-01') TO ('2023-01-01');
CREATE TABLE event_2023 PARTITION OF event FOR VALUES FROM ('2023-01-01') TO ('2024-01-01');
CREATE TABLE event_2024 PARTITION OF event FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
CREATE TABLE event_2025 PARTITION OF event FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE event_2026 PARTITION OF event FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE event_future PARTITION OF event FOR VALUES FROM ('2027-01-01') TO (MAXVALUE);

CREATE INDEX event_ts_idx      ON event (ts);
CREATE INDEX event_thread_idx  ON event (thread_key, ts);
CREATE INDEX event_source_idx  ON event (source, ts);
CREATE INDEX event_person_idx  ON event (person_id, ts);

-- Answers "when did I FIRST mention X" — a bare token in a 15-char message,
-- where dense embeddings are noise and BM25 over lemmatized text wins.
-- RusBEIR: BM25 beats BGE-M3 by 13pp on some Russian retrieval tasks.
CREATE INDEX event_fts_idx ON event USING GIN (
    to_tsvector('ru_unaccent',
        coalesce(text,'') || ' ' || coalesce(transcript,'') || ' ' || coalesce(ocr_text,''))
);
CREATE INDEX event_trgm_idx ON event USING GIN (text gin_trgm_ops);

-- ---------------------------------------------------------------------------
--  LAYER 1 — segments (the retrieval unit)
--
--  681,331 Telegram events -> ~50,000 segments. Highest-value transformation
--  in the system. SeCom: segment 71.57 > turn 65.58 > session 63.16 >
--  summaries 53.87-56.25, measured at 30 tokens/turn. Ours are 5-10.
-- ---------------------------------------------------------------------------

CREATE TABLE segment (
    segment_id    BIGSERIAL PRIMARY KEY,
    thread_key    TEXT        NOT NULL,
    sources       TEXT[]      NOT NULL,      -- usually one; cross-source is allowed
    started_at    TIMESTAMPTZ NOT NULL,
    ended_at      TIMESTAMPTZ NOT NULL,
    event_count   INT         NOT NULL,
    participant_ids BIGINT[]  NOT NULL DEFAULT '{}',
    source_event_ids TEXT[]   NOT NULL,      -- evidence, always

    -- raw_text is what you RETURN. embed_text is what you EMBED.
    -- Conflating these is the most common error in this pattern.
    -- LongMemEval: facts CONCATENATE with the original, never replace it —
    -- "using these condensed forms alone does not enhance memory recall."
    raw_text      TEXT NOT NULL,
    embed_text    TEXT NOT NULL,
    lemmatized_text TEXT,

    -- Enrichment. NULL until the batch worker reaches this row; retrieval
    -- must work without it, so you can ship before the backfill finishes.
    summary       TEXT,
    topics        TEXT[] NOT NULL DEFAULT '{}',
    lang_mix      JSONB,
    importance    REAL,
    sentiment     REAL,
    sentiment_conf REAL,
    is_substantive BOOLEAN NOT NULL DEFAULT TRUE,

    embedding     halfvec(1024),

    segmenter_version TEXT NOT NULL,
    extractor_version TEXT,
    embedder_version  TEXT,
    enriched_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT segment_time_order CHECK (ended_at >= started_at)
);

CREATE INDEX segment_ts_idx       ON segment (started_at);
CREATE INDEX segment_thread_idx   ON segment (thread_key, started_at);
CREATE INDEX segment_parts_idx    ON segment USING GIN (participant_ids);
CREATE INDEX segment_topics_idx   ON segment USING GIN (topics);
CREATE INDEX segment_sources_idx  ON segment USING GIN (sources);
CREATE INDEX segment_unenriched_idx ON segment (created_at) WHERE enriched_at IS NULL;
CREATE INDEX segment_fts_idx ON segment USING GIN (
    to_tsvector('ru_unaccent', coalesce(lemmatized_text, embed_text)));

-- DELIBERATELY NO ANN INDEX.
-- ~60k halfvec(1024) = ~123 MB, fits in shared_buffers. Exact cosine is
-- single-digit ms AND every date filter stays exact and free. Filtered HNSW
-- fragments below p_c = 1/<k>; a one-month filter selects ~1.1% of the
-- corpus, deep in the fragmentation regime. ChronoQA measured naive temporal
-- filtering REDUCING recall (0.4903 vs 0.5458 R@5).
-- Revisit past ~1M segments:
-- CREATE INDEX segment_hnsw_idx ON segment
--   USING hnsw (embedding halfvec_cosine_ops) WITH (m=16, ef_construction=200);

-- ---------------------------------------------------------------------------
--  LAYER 2 — semantic projections
-- ---------------------------------------------------------------------------

CREATE TABLE entity (
    entity_id     BIGSERIAL PRIMARY KEY,
    entity_type   TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    aliases       TEXT[] NOT NULL DEFAULT '{}',
    phonetic_keys TEXT[] NOT NULL DEFAULT '{}',
    skeleton_keys TEXT[] NOT NULL DEFAULT '{}',
    person_id     BIGINT REFERENCES person(person_id),
    embedding     halfvec(1024),
    first_seen_at TIMESTAMPTZ,
    last_seen_at  TIMESTAMPTZ,
    mention_count INT NOT NULL DEFAULT 0,     -- cheap non-LLM ranking signal
    extractor_version TEXT NOT NULL,
    UNIQUE (entity_type, canonical_name)
);
CREATE INDEX entity_phonetic_idx ON entity USING GIN (phonetic_keys);
CREATE INDEX entity_skeleton_idx ON entity USING GIN (skeleton_keys);
CREATE INDEX entity_mentions_idx ON entity (mention_count DESC);

CREATE TABLE entity_mention (
    entity_id  BIGINT NOT NULL REFERENCES entity(entity_id) ON DELETE CASCADE,
    segment_id BIGINT NOT NULL REFERENCES segment(segment_id) ON DELETE CASCADE,
    ts         TIMESTAMPTZ NOT NULL,
    salience   REAL,
    PRIMARY KEY (entity_id, segment_id)
);
CREATE INDEX em_ts_idx ON entity_mention (ts);

CREATE TABLE fact_predicate (
    predicate     TEXT PRIMARY KEY,
    description   TEXT,
    -- TRUE: a new value INVALIDATES the previous one (lives_in).
    -- FALSE: values accumulate (read_book).
    single_valued BOOLEAN NOT NULL DEFAULT TRUE
);

-- Bi-temporal. Two independent timelines:
--   T  (t_valid/t_invalid)   when the fact was true IN THE WORLD
--   T' (t_created/t_expired) when the SYSTEM learned or retracted it
-- "Where does Anna live?" has different correct answers in 2019 and 2026.
-- Telegram timestamps make t_valid GROUNDED rather than inferred.
--
-- CAVEAT: Zep/Graphiti scored 7% on FactConsolidation single-hop conflict
-- resolution — WORST of any system tested (HippoRAG-2 54%, BM25 48%). This
-- schema REPRESENTS contradiction; resolve_fact_conflicts() RESOLVES it,
-- in SQL, with max(version). Never in a prompt.
CREATE TABLE fact (
    fact_id       BIGSERIAL PRIMARY KEY,
    subject_id    BIGINT REFERENCES entity(entity_id),
    predicate     TEXT NOT NULL REFERENCES fact_predicate(predicate),
    object_text   TEXT,
    object_id     BIGINT REFERENCES entity(entity_id),

    t_valid       TIMESTAMPTZ NOT NULL,
    t_invalid     TIMESTAMPTZ,
    t_created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_expired     TIMESTAMPTZ,

    version       BIGINT NOT NULL,            -- epoch millis of source event
    confidence    REAL NOT NULL DEFAULT 0.5,
    polarity      SMALLINT NOT NULL DEFAULT 1,

    source_segment_id BIGINT REFERENCES segment(segment_id) ON DELETE CASCADE,
    source_event_ids  TEXT[] NOT NULL,
    extractor_version TEXT NOT NULL,
    refcount      INT NOT NULL DEFAULT 0,

    -- Set when this fact has been promoted into Hindsight. Chronicle is the
    -- evidence layer; Hindsight is the judgment layer. Promotion is rare and
    -- deliberate — target hundreds/year. The `personal` bank is already at
    -- 2,726 facts and times out on sync_retain; a firehose destroys it.
    hindsight_bank        TEXT,
    hindsight_document_id TEXT,
    promoted_at   TIMESTAMPTZ,

    CONSTRAINT fact_valid_order CHECK (t_invalid IS NULL OR t_invalid >= t_valid),
    CONSTRAINT fact_evidence_nonempty CHECK (cardinality(source_event_ids) > 0)
);
CREATE INDEX fact_subject_idx ON fact (subject_id, predicate);
CREATE INDEX fact_valid_idx   ON fact (t_valid, t_invalid);
CREATE INDEX fact_current_idx ON fact (subject_id, predicate, version DESC)
    WHERE t_invalid IS NULL AND t_expired IS NULL;
CREATE INDEX fact_promotable_idx ON fact (confidence DESC)
    WHERE promoted_at IS NULL AND t_invalid IS NULL;

CREATE TABLE commitment (
    commitment_id BIGSERIAL PRIMARY KEY,
    text          TEXT NOT NULL,
    committer_id  BIGINT REFERENCES person(person_id),
    committee_id  BIGINT REFERENCES person(person_id),
    direction     TEXT NOT NULL,              -- i_owe | owed_to_me
    stated_at     TIMESTAMPTZ NOT NULL,
    due_at        TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'open',
    resolved_at   TIMESTAMPTZ,
    resolution_segment_id BIGINT REFERENCES segment(segment_id),
    confidence    REAL,
    source_segment_id BIGINT REFERENCES segment(segment_id) ON DELETE CASCADE,
    source_event_ids  TEXT[] NOT NULL,
    extractor_version TEXT NOT NULL
);
CREATE INDEX commitment_open_idx ON commitment (status, stated_at) WHERE status = 'open';

CREATE TABLE life_event (
    life_event_id BIGSERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL,
    ended_at      TIMESTAMPTZ,
    category      TEXT,
    significance  SMALLINT,
    -- Seven years of intimate conversation includes breakups, deaths, and the
    -- war. Surfacing those unprompted is a real harm, not an edge case.
    is_sensitive  BOOLEAN NOT NULL DEFAULT FALSE,
    description   TEXT,
    curated_by    TEXT NOT NULL DEFAULT 'llm',
    source_event_ids TEXT[] NOT NULL DEFAULT '{}'
);
CREATE INDEX life_event_time_idx ON life_event (started_at);

-- Dependency graph — enables provable deletion. "Backflow": deleting an event
-- doesn't delete it if derived artifacts survive. Reference-count from DAY
-- ONE; retrofitting is very expensive.
CREATE TABLE projection_dep (
    projection_kind TEXT NOT NULL,
    projection_id   BIGINT NOT NULL,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    PRIMARY KEY (projection_kind, projection_id, source, source_id)
);
CREATE INDEX projdep_event_idx ON projection_dep (source, source_id);

CREATE TABLE erasure_log (
    erasure_id    BIGSERIAL PRIMARY KEY,
    scope         TEXT NOT NULL,
    scope_ref     JSONB NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    projections_pruned INT
);

-- Per-thread fitted session gap. Nobody has published a principled threshold
-- for personal IM (30-min web convention vs MSC's 1-7h bracket the range),
-- so measure your own bimodal distribution. See chronicle/segment.py.
CREATE TABLE thread_config (
    thread_key    TEXT PRIMARY KEY,
    source        TEXT NOT NULL REFERENCES source(source),
    label         TEXT,
    gap_seconds   INT NOT NULL DEFAULT 1800,
    gap_fit_stats JSONB,
    fitted_at     TIMESTAMPTZ
);
