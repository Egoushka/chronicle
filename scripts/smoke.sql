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

-- segments (segmentation normally happens in Python; insert a plausible set)
INSERT INTO segment(thread_key, sources, started_at, ended_at, event_count,
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
SELECT 'segments: ' || count(*) FROM segment;

-- Three segments about a rare subject, for the IDF lexical branch.
INSERT INTO segment(thread_key, sources, started_at, ended_at, event_count,
                    source_event_ids, raw_text, embed_text, lemmatized_text,
                    is_substantive, embedding, segmenter_version)
SELECT 'c2', '{telegram}', timestamptz '2025-03-01' + g * interval '1 day',
       timestamptz '2025-03-01' + g * interval '1 day' + interval '10 min', 3,
       ARRAY['c2:'||g], 'me: купил роутер mikrotik', 'купил роутер mikrotik hap',
       'купить роутер mikrotik hap', TRUE,
       (SELECT array_agg(random())::halfvec(1024) FROM generate_series(1,1024)),
       'seg-2026.07-timegap-v1'
FROM generate_series(1, 3) g;

-- hybrid search
-- IDF lexical branch: needs lexeme_df; before a refresh it is empty and
-- search is dense-only, which must not error either.
SELECT 'lexeme_df refreshed over: ' || refresh_lexeme_df();
-- A natural-language question: the old plainto_tsquery ANDed every word and
-- matched none of these; the IDF branch must find all three.
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM hybrid_search(
      (SELECT embedding FROM segment LIMIT 1), 'когда я купил роутер mikrotik домой?',
      NULL, NULL, NULL, NULL, 100, 50) WHERE lex_rank IS NOT NULL;
  IF n <> 3 THEN RAISE EXCEPTION 'IDF lexical branch found % of 3 rare segments', n; END IF;
END $$;
SELECT 'idf lexical: 3 of 3 rare segments found';
-- The enrichment list (migration 006): a word only the enrichment holds must
-- surface its segment when asked, and change nothing when not.
UPDATE segment SET enrich_text = 'аренда хатыни'
 WHERE segment_id = (SELECT segment_id FROM segment ORDER BY segment_id LIMIT 1);
DO $$
DECLARE on_n int; off_n int;
BEGIN
  SELECT count(*) INTO on_n FROM hybrid_search(
      (SELECT embedding FROM segment LIMIT 1), 'аренда хатыни',
      NULL, NULL, NULL, NULL, 100, 50, 60, TRUE)
    WHERE segment_id = (SELECT min(segment_id) FROM segment);
  SELECT count(*) INTO off_n FROM hybrid_search(
      (SELECT embedding FROM segment LIMIT 1), 'аренда хатыни',
      NULL, NULL, NULL, NULL, 100, 50, 60, FALSE)
    WHERE lex_rank IS NOT NULL AND segment_id = (SELECT min(segment_id) FROM segment);
  IF on_n <> 1 THEN RAISE EXCEPTION 'enrichment list did not surface its segment'; END IF;
  IF off_n <> 0 THEN RAISE EXCEPTION 'enrichment leaked into the default search'; END IF;
END $$;
SELECT 'enrich list: surfaces on, silent off';
UPDATE segment SET enrich_text = NULL;
SELECT 'hybrid_search rows: ' || count(*) FROM hybrid_search(
    (SELECT embedding FROM segment LIMIT 1), 'квартира ипотека',
    NULL, NULL, NULL, NULL, 100, 20);

-- date-filtered (the 1%-cardinality case that breaks filtered HNSW)
SELECT 'hybrid_search 2019-03 window: ' || count(*) FROM hybrid_search(
    (SELECT embedding FROM segment LIMIT 1), 'квартира',
    '2019-03-01', '2019-03-31', NULL, NULL, 100, 20);

-- first mention (argmin)
SELECT 'first_mention -> ' || ts::date || ' | ' || left(text, 40)
FROM first_mention(ARRAY['kubernetes'], 3) LIMIT 1;

-- stratified
SELECT 'stratified bins: ' || count(DISTINCT bin_start) || ', rows: ' || count(*)
FROM stratified_search((SELECT embedding FROM segment LIMIT 1), '3 months', 10);

-- bi-temporal facts
-- seeded by migrations/005; kept so this file also runs against 001-004 alone
INSERT INTO fact_predicate(predicate, single_valued) VALUES ('lives_in', TRUE)
    ON CONFLICT (predicate) DO NOTHING;
INSERT INTO entity(entity_type, canonical_name, extractor_version) VALUES ('person','Аня','v1');
INSERT INTO fact(subject_id, predicate, object_text, t_valid, version, confidence,
                 source_event_ids, extractor_version)
SELECT (SELECT entity_id FROM entity LIMIT 1), 'lives_in', v.o, v.t, v.ver, 0.9, '{c1:1}', 'v1'
FROM (VALUES ('Харьков', timestamptz '2019-01-01', 1546300800000::bigint),
             ('Киев',    timestamptz '2022-06-01', 1654041600000::bigint),
             ('Львів',   timestamptz '2024-03-01', 1709251200000::bigint)) AS v(o,t,ver);
SELECT 'facts invalidated: ' || resolve_fact_conflicts();
-- version order disagreeing with t_valid order must not violate
-- fact_valid_order (first live enrich run, 2026-09-26)
INSERT INTO fact(subject_id, predicate, object_text, t_valid, version, confidence,
                 source_event_ids, extractor_version)
SELECT (SELECT entity_id FROM entity LIMIT 1), 'lives_in', v.o, v.t, v.ver, 0.9, '{c1:2}', 'v1'
FROM (VALUES ('Одеса', timestamptz '2025-01-02', 1735700000000::bigint),
             ('Дніпро', timestamptz '2025-01-01', 1735800000000::bigint)) AS v(o,t,ver);
SELECT 'out-of-order versions resolved: ' || resolve_fact_conflicts();
SELECT 'current fact: ' || object_text FROM v_current_facts;
SELECT 'history: ' || object_text || ' [' || t_valid::date || ' -> ' ||
       coalesce(t_invalid::date::text,'present') || ']' FROM fact ORDER BY version;

-- views
SELECT 'v_daily rows: ' || count(*) FROM v_daily;
SELECT 'v_on_this_day: ' || count(*) FROM v_on_this_day;
SELECT 'v_promotable_facts: ' || count(*) FROM v_promotable_facts;
SELECT 'v_forgotten_commitments: ' || count(*) FROM v_forgotten_commitments;
