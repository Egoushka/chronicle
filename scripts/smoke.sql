\set ON_ERROR_STOP on
INSERT INTO source(source, density, tier) VALUES ('telegram','narrative',1), ('wakapi','telemetry',1);
INSERT INTO person(display_name, telegram_user_id, is_self, phonetic_keys, skeleton_keys)
VALUES ('me', 1, TRUE, '{}', '{}'), ('Аня', 2, FALSE, '{"an"}', '{}');

-- bursty events across 2019..2026, mimicking the real distribution
INSERT INTO event(source, source_id, ts, kind, actor, person_id, text, thread_key)
SELECT 'telegram', 'c1:'||g,
       timestamptz '2019-01-01' + (g * interval '7 hours') + (random()*interval '30 min'),
       'message', CASE WHEN g%2=0 THEN 'me' ELSE 'Аня' END,
       CASE WHEN g%2=0 THEN 1 ELSE 2 END,
       CASE WHEN g%7=0 THEN 'обсуждали квартиру в Киеве и ипотеку'
            WHEN g%11=0 THEN 'думаю про kubernetes и деплой сервисов'
            WHEN g%5=0 THEN 'ок' ELSE 'ага' END,
       'c1'
FROM generate_series(1, 8000) g;

INSERT INTO event(source, source_id, ts, kind, actor, text, thread_key)
SELECT 'wakapi', 'p:'||g, timestamptz '2024-01-01' + (g*interval '2 days'),
       'coding_session', 'me', 'coded on chronicle for 45 min (python)', 'wakapi:chronicle'
FROM generate_series(1,300) g;

SELECT 'events inserted: ' || count(*) FROM event;

-- fit gaps
SELECT 'threads fitted: ' || fit_thread_gaps(100);
SELECT thread_key, gap_seconds, gap_fit_stats->>'p90' AS p90 FROM thread_config;

-- episodes (segmentation normally happens in Python; insert a plausible set)
INSERT INTO episode(thread_key, sources, started_at, ended_at, event_count,
                    participant_ids, source_event_ids, raw_text, embed_text,
                    lemmatized_text, topics, is_substantive, embedding, segmenter_version)
SELECT 'c1', '{telegram}',
       timestamptz '2019-01-01' + (g*interval '2 days'),
       timestamptz '2019-01-01' + (g*interval '2 days') + interval '25 min',
       12, '{1,2}', ARRAY['c1:'||g],
       'me: обсуждали квартиру в Киеве', '[chat: Аня][2019-01] обсуждали квартиру в Киеве и ипотеку',
       'обсуждать квартира киев ипотека', '{housing,finance}', TRUE,
       (SELECT array_agg(random())::halfvec(1024) FROM generate_series(1,1024)),
       'seg-2026.07-timegap-v1'
FROM generate_series(1, 1200) g;
SELECT 'episodes: ' || count(*) FROM episode;

-- hybrid search
SELECT 'hybrid_search rows: ' || count(*) FROM hybrid_search(
    (SELECT embedding FROM episode LIMIT 1), 'квартира ипотека',
    NULL, NULL, NULL, NULL, 100, 20);

-- date-filtered (the 1%-cardinality case that breaks filtered HNSW)
SELECT 'hybrid_search 2019-03 window: ' || count(*) FROM hybrid_search(
    (SELECT embedding FROM episode LIMIT 1), 'квартира',
    '2019-03-01', '2019-03-31', NULL, NULL, 100, 20);

-- first mention (argmin)
SELECT 'first_mention -> ' || ts::date || ' | ' || left(text, 40)
FROM first_mention(ARRAY['kubernetes'], 3) LIMIT 1;

-- stratified
SELECT 'stratified bins: ' || count(DISTINCT bin_start) || ', rows: ' || count(*)
FROM stratified_search((SELECT embedding FROM episode LIMIT 1), '3 months', 10);

-- bi-temporal facts
INSERT INTO fact_predicate(predicate, single_valued) VALUES ('lives_in', TRUE);
INSERT INTO entity(entity_type, canonical_name, extractor_version) VALUES ('person','Аня','v1');
INSERT INTO fact(subject_id, predicate, object_text, t_valid, version, confidence,
                 source_event_ids, extractor_version)
SELECT (SELECT entity_id FROM entity LIMIT 1), 'lives_in', v.o, v.t, v.ver, 0.9, '{c1:1}', 'v1'
FROM (VALUES ('Харьков', timestamptz '2019-01-01', 1546300800000::bigint),
             ('Киев',    timestamptz '2022-06-01', 1654041600000::bigint),
             ('Львів',   timestamptz '2024-03-01', 1709251200000::bigint)) AS v(o,t,ver);
SELECT 'facts invalidated: ' || resolve_fact_conflicts();
SELECT 'current fact: ' || object_text FROM v_current_facts;
SELECT 'history: ' || object_text || ' [' || t_valid::date || ' -> ' ||
       coalesce(t_invalid::date::text,'present') || ']' FROM fact ORDER BY version;

-- views
SELECT 'v_daily rows: ' || count(*) FROM v_daily;
SELECT 'v_on_this_day: ' || count(*) FROM v_on_this_day;
SELECT 'v_promotable_facts: ' || count(*) FROM v_promotable_facts;
SELECT 'v_forgotten_commitments: ' || count(*) FROM v_forgotten_commitments;
