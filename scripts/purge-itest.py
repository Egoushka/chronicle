#!/usr/bin/env python3
"""End-to-end check for `chronicle.purge` against a real PostgreSQL.

Not in `tests/` because that suite deliberately needs no database and no
models. The defects this catches are all SQL semantics that a mock cannot
have — it has already caught two that read as obviously correct:

  * `x <> ANY(arr)` is NOT the negation of `x = ANY(arr)`. With more than one
    element the first is true for practically every row, so the prune it
    guarded silently did nothing.
  * Data-modifying CTEs share one snapshot, so a DELETE CTE cannot see rows an
    UPDATE CTE in the same statement emptied, and deletes none of them.

Run by scripts/smoke.sh, i.e. by CI, against the same throwaway database.

    CHRONICLE_DB_URL=postgresql://... python3 scripts/purge-itest.py
"""
import os
import sqlite3
import subprocess
import sys
import tempfile

import psycopg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTDB = "chronicle_purge_itest"

live = os.environ.get("CHRONICLE_DB_URL")
if not live:
    pw, host, user = (os.environ.get("PGPASSWORD", ""), os.environ.get("PGHOST", "localhost"),
                      os.environ.get("PGUSER", "postgres"))
    live = f"postgresql://{user}:{pw}@{host}:5432/postgres"
base = live.rsplit("/", 1)[0]
ADMIN, URL = f"{base}/postgres", f"{base}/{TESTDB}"
# The whole point of this file is that it deletes things. Never at the live one.
assert TESTDB not in live, "refusing to run against the configured database"


def main() -> int:
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TESTDB}")
        c.execute(f"CREATE DATABASE {TESTDB}")
    try:
        return run()
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {TESTDB}")


def run() -> int:
    mig = os.path.join(ROOT, "migrations")
    for m in sorted(os.listdir(mig)):
        with psycopg.connect(URL, autocommit=True) as c, open(os.path.join(mig, m)) as fh:
            c.execute(fh.read())

    # telegram fixture: one human chat, one bot by username, one bot by tag.
    tg = tempfile.mktemp(suffix=".db")
    s = sqlite3.connect(tg)
    s.execute("CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT,"
              " username TEXT, included INTEGER, updated_at TEXT)")
    s.execute("CREATE TABLE chat_tags (chat_id INTEGER, tag TEXT, source TEXT,"
              " created_at TEXT, PRIMARY KEY (chat_id, tag))")
    s.executemany("INSERT INTO chats VALUES (?,?,?,?,?,?)", [
        (111, "anna", "user", "anna", 1, ""),
        (333, "jarvis", "user", "examplejarvisbot", 1, ""),
        (444, "botfather", "user", "BotFather", 1, "")])
    s.execute("INSERT INTO chat_tags VALUES (444,'chat:bot','auto','')")
    s.commit()
    s.close()

    with psycopg.connect(URL, autocommit=True) as conn, conn.cursor() as c:
        c.execute("INSERT INTO source (source) VALUES ('telegram') ON CONFLICT DO NOTHING")
        # Two YEARS per chat: `event` is PARTITION BY RANGE (ts) and a delete
        # that only reached one partition would still look like a pass.
        rows = [("telegram", f"{chat}:{year}{i}", f"{year}-06-0{i+1} 10:00+00",
                 "message", f"m{i}", f"telegram:{chat}")
                for chat, year in ((111, 2019), (111, 2026), (333, 2019),
                                   (333, 2026), (444, 2026))
                for i in range(4)]
        c.executemany("INSERT INTO event (source, source_id, ts, kind, text, thread_key)"
                      " VALUES (%s,%s,%s,%s,%s,%s)", rows)

        for chat in (111, 333, 444):
            c.execute("""INSERT INTO segment (thread_key, sources, started_at, ended_at,
                           event_count, source_event_ids, raw_text, embed_text,
                           embedding, segmenter_version)
                         VALUES (%s,'{telegram}','2026-06-01+00','2026-06-02+00',4,%s,
                                 'x','x', array_fill(0.1::real, ARRAY[1024])::halfvec,'v1')""",
                      (f"telegram:{chat}", [f"telegram:{chat}:2026{i}" for i in range(4)]))

        c.execute("SELECT segment_id FROM segment WHERE thread_key='telegram:333'")
        doomed = c.fetchone()[0]
        c.execute("SELECT segment_id FROM segment WHERE thread_key='telegram:111'")
        kept = c.fetchone()[0]

        c.execute("INSERT INTO entity (entity_type, canonical_name, extractor_version)"
                  " VALUES ('person','x','v1') RETURNING entity_id")
        eid = c.fetchone()[0]
        c.execute("INSERT INTO entity_mention (entity_id, segment_id, ts)"
                  " VALUES (%s,%s,now())", (eid, doomed))
        c.execute("INSERT INTO fact_predicate (predicate) VALUES ('likes') ON CONFLICT DO NOTHING")
        c.execute("""INSERT INTO fact (predicate, t_valid, version, source_segment_id,
                       source_event_ids, extractor_version)
                     VALUES ('likes', now(), 1, %s, '{telegram:333:20260}','v1')""", (doomed,))
        # The trap: a SURVIVING commitment resolved BY a doomed segment.
        # confdeltype='a' on that edge, so an unprepared DELETE aborts on a
        # foreign key violation and the whole purge rolls back.
        c.execute("""INSERT INTO commitment (text, direction, stated_at, status,
                       resolution_segment_id, source_segment_id, source_event_ids,
                       extractor_version)
                     VALUES ('pay rent','i_owe',now(),'resolved',%s,%s,
                             '{telegram:111:20260}','v1')""", (doomed, kept))
        c.execute("""INSERT INTO life_event (title, started_at, source_event_ids)
                     VALUES ('mixed', now(), '{telegram:111:20260,telegram:333:20260}'),
                            ('all bot', now(), '{telegram:333:20261}')""")
        c.executemany("INSERT INTO projection_dep VALUES (%s,%s,%s,%s)",
                      [("fact", 1, "telegram", "333:20260"),
                       ("fact", 1, "telegram", "111:20260")])

    env = {**os.environ, "CHRONICLE_DB_URL": URL, "TELEGRAM_DB_URL": tg,
           "PYTHONPATH": ROOT}
    dry = subprocess.run([sys.executable, "-m", "chronicle.purge"],
                         env=env, capture_output=True, text=True, check=False)
    assert dry.returncode == 0, dry.stderr
    with psycopg.connect(URL) as conn, conn.cursor() as c:
        c.execute("SELECT count(*) FROM event")
        assert c.fetchone()[0] == 20, "DRY RUN DELETED ROWS"

    run_ = subprocess.run([sys.executable, "-m", "chronicle.purge", "--apply"],
                          env=env, capture_output=True, text=True, check=False)
    assert run_.returncode == 0, run_.stderr

    with psycopg.connect(URL) as conn, conn.cursor() as c:
        def q(sql):
            c.execute(sql)
            return c.fetchone()[0]
        checks = {
            "bot events gone":            (q("SELECT count(*) FROM event WHERE thread_key IN ('telegram:333','telegram:444')"), 0),
            "human events kept":          (q("SELECT count(*) FROM event WHERE thread_key='telegram:111'"), 8),
            "2019 partition pruned":      (q("SELECT count(*) FROM event_2019 WHERE thread_key='telegram:333'"), 0),
            "2019 partition kept":        (q("SELECT count(*) FROM event_2019 WHERE thread_key='telegram:111'"), 4),
            "2026 partition pruned":      (q("SELECT count(*) FROM event_2026 WHERE thread_key='telegram:333'"), 0),
            "segments left":              (q("SELECT count(*) FROM segment"), 1),
            "embeddings went with them":  (q("SELECT count(*) FROM segment WHERE embedding IS NOT NULL"), 1),
            "entity_mention cascaded":    (q("SELECT count(*) FROM entity_mention"), 0),
            "fact cascaded":              (q("SELECT count(*) FROM fact"), 0),
            "commitment survived":        (q("SELECT count(*) FROM commitment"), 1),
            "its resolution nulled":      (q("SELECT count(*) FROM commitment WHERE resolution_segment_id IS NULL AND status='open'"), 1),
            "mixed life_event kept":      (q("SELECT count(*) FROM life_event WHERE title='mixed'"), 1),
            "mixed life_event pruned":    (q("SELECT cardinality(source_event_ids) FROM life_event WHERE title='mixed'"), 1),
            "all-bot life_event dropped": (q("SELECT count(*) FROM life_event WHERE title='all bot'"), 0),
            "projection_dep pruned":      (q("SELECT count(*) FROM projection_dep WHERE source_id='333:20260'"), 0),
            "projection_dep kept":        (q("SELECT count(*) FROM projection_dep WHERE source_id='111:20260'"), 1),
            "erasure_log written":        (q("SELECT count(*) FROM erasure_log"), 1),
            "erasure_log event count":    (q("SELECT (scope_ref->>'events_deleted')::int FROM erasure_log"), 12),
            "erasure_log projections":    (q("SELECT projections_pruned FROM erasure_log"), 2),
        }

    bad = 0
    for k, (got, want) in checks.items():
        ok = got == want
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {k:<28} got={got} want={want}")
    print("purge-itest OK" if not bad else f"purge-itest FAILED ({bad})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
