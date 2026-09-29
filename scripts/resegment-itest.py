#!/usr/bin/env python3
"""End-to-end check of `worker resegment` — rebuilding threads at a new cap.

Sibling of purge-itest.py and here for the same reason: the failure modes are
PostgreSQL semantics a mock cannot have.

  * `commitment.resolution_segment_id` is ON DELETE NO ACTION (fact 31), so
    rebuilding a thread whose segment resolved a commitment aborts on a
    foreign key violation unless it is nulled first.
  * projection_dep has no foreign key, so the cascade into fact leaves
    orphans unless they are deleted by projection.
  * Rebuilt segments must lose their embedding, and a thread NOT named must
    keep its rows — the same rows, not equal-looking rebuilt ones.
  * Afterwards the incremental `segment` must see nothing to do: every event
    is cited again, so a nightly run creates no duplicates.

It also pins the reply-edge rule, which the worker disabled until 2026-09-29
by passing reply_to_id=None for every event.

    CHRONICLE_DB_URL=postgresql://... python3 scripts/resegment-itest.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TESTDB = "chronicle_resegment_itest"

live = os.environ.get("CHRONICLE_DB_URL")
if not live:
    pw, host, user = (os.environ.get("PGPASSWORD", ""), os.environ.get("PGHOST", "localhost"),
                      os.environ.get("PGUSER", "postgres"))
    live = f"postgresql://{user}:{pw}@{host}:5432/postgres"
base = live.rsplit("/", 1)[0]
ADMIN, URL = f"{base}/postgres", f"{base}/{TESTDB}"
assert TESTDB not in live, "refusing to run against the configured database"

T0 = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)


def main() -> int:
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TESTDB}")
        c.execute(f"CREATE DATABASE {TESTDB}")
    try:
        return run()
    finally:
        # FORCE: the worker's commands hold their connection for the process.
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {TESTDB} WITH (FORCE)")


def run() -> int:
    mig = os.path.join(ROOT, "migrations")
    for m in sorted(os.listdir(mig)):
        with psycopg.connect(URL, autocommit=True) as c, open(os.path.join(mig, m)) as fh:
            c.execute(fh.read())

    from chronicle import worker
    worker.DB_URL = URL

    def q(sql, *p):
        with psycopg.connect(URL) as c:
            return c.execute(sql, p).fetchall()

    def ex(sql, *p):
        with psycopg.connect(URL) as c:
            c.execute(sql, p)

    def segment(cap):
        worker.cmd_segment(SimpleNamespace(tier=1, max_messages=cap))

    def resegment(cap, *threads):
        worker.cmd_resegment(SimpleNamespace(max_messages=cap, thread=list(threads)))

    def fake_embed():
        ex("UPDATE segment SET embedding = array_fill(0.1, ARRAY[1024])::halfvec,"
           " embedder_version = 'x'")

    def counts(tk):
        return q("SELECT count(*), count(embedding) FROM segment WHERE thread_key = %s",
                 tk)[0]

    checks = []

    def check(name, got, want):
        checks.append((name, got == want, got, want))

    def events(thread, rows):
        ex("""INSERT INTO event (source, source_id, ts, kind, text, reply_to, thread_key)
              SELECT 'telegram', r.sid, r.ts, 'message', r.txt, r.reply, %s
                FROM unnest(%s::text[], %s::timestamptz[], %s::text[], %s::text[])
                     AS r(sid, ts, txt, reply)""",
           f"telegram:{thread}", [r[0] for r in rows], [r[1] for r in rows],
           [r[2] for r in rows], [r[3] for r in rows])

    ex("INSERT INTO source (source, density) VALUES ('telegram', 'narrative')"
       " ON CONFLICT (source) DO UPDATE SET density = 'narrative'")
    # 40 events a minute apart: far inside the 1800 s default gap, so the cap
    # is the only thing that splits them and the arithmetic is exact.
    events(111, [(f"111:{i}", T0 + timedelta(minutes=i), f"msg {i}", None)
                 for i in range(40)])
    # A second thread that only the unscoped runs touch.
    events(222, [(f"222:{i}", T0 + timedelta(minutes=i), f"msg {i}", None)
                 for i in range(5)])
    # Reply edge: three messages, a 90-minute silence (> the 30-minute gap,
    # < 4x it), then a reply to the first message and two more. Without the
    # edge that is two segments of three; with it, one of six.
    events(333, [(f"333:{i}", T0 + timedelta(minutes=i), f"msg {i}", None)
                 for i in range(3)]
           + [("333:3", T0 + timedelta(minutes=92), "re: msg 0", "333:0"),
              ("333:4", T0 + timedelta(minutes=93), "msg 4", None),
              ("333:5", T0 + timedelta(minutes=94), "msg 5", None)])

    # --- 1. incremental segment at cap 30 ----------------------------------
    segment(30)
    check("40 events at cap 30 -> 2 segments", counts("telegram:111")[0], 2)
    check("a reply edge suppresses the split", counts("telegram:333")[0], 1)
    check("segmenter_version records the cap",
          q("SELECT DISTINCT segmenter_version LIKE '%%/m30' FROM segment"), [(True,)])
    fake_embed()

    # --- 2. resegment one thread at cap 15 ---------------------------------
    before_222 = q("SELECT segment_id FROM segment WHERE thread_key = 'telegram:222'")
    resegment(15, "telegram:111")
    check("40 events at cap 15 -> 3 segments", counts("telegram:111"), (3, 0))
    check("the named thread's rows carry the new cap",
          q("SELECT DISTINCT segmenter_version LIKE '%%/m15' FROM segment"
            " WHERE thread_key = 'telegram:111'"), [(True,)])
    check("the other thread kept segment and embedding", counts("telegram:222"), (1, 1))
    check("...and they are the SAME rows",
          q("SELECT segment_id FROM segment WHERE thread_key = 'telegram:222'"),
          before_222)

    # --- 3. incremental segment after a rebuild: nothing to do -------------
    n = q("SELECT count(*) FROM segment")[0][0]
    segment(30)
    check("a nightly run after resegment creates nothing",
          q("SELECT count(*) FROM segment")[0][0], n)
    check("no event is cited twice",
          q("""SELECT count(*) FROM (SELECT k FROM segment, unnest(source_event_ids) k
                                     GROUP BY k HAVING count(*) > 1) d"""), [(0,)])

    # --- 4. rebuild under projections (fact 31) ----------------------------
    seg_a = q("SELECT min(segment_id) FROM segment WHERE thread_key = 'telegram:111'")[0][0]
    with psycopg.connect(URL) as c:
        eid = c.execute("""INSERT INTO entity (entity_type, canonical_name, extractor_version)
                           VALUES ('person', 'x', 'v1') RETURNING entity_id""").fetchone()[0]
        c.execute("INSERT INTO entity_mention (entity_id, segment_id, ts) VALUES (%s,%s,now())",
                  (eid, seg_a))
        fid = c.execute("""INSERT INTO fact (predicate, t_valid, version, source_segment_id,
                                             source_event_ids, extractor_version)
                           VALUES ('likes', now(), 1, %s, '{telegram:111:0}', 'v1')
                           RETURNING fact_id""", (seg_a,)).fetchone()[0]
        # source_segment_id left NULL on purpose: that edge CASCADES, so
        # pointing it into this thread would delete the commitment before the
        # NO ACTION edge could fire, and the check would prove nothing.
        cid = c.execute("""INSERT INTO commitment (text, direction, stated_at, status,
                               resolution_segment_id, source_event_ids, extractor_version)
                           VALUES ('pay rent', 'i_owe', now(), 'resolved', %s,
                                   '{telegram:111:1}', 'v1')
                           RETURNING commitment_id""", (seg_a,)).fetchone()[0]
        c.cursor().executemany("INSERT INTO projection_dep VALUES (%s,%s,%s,%s)",
                      [("fact", fid, "telegram", "111:0"),
                       ("commitment", cid, "telegram", "111:1")])
        c.execute("""INSERT INTO life_event (title, started_at, source_event_ids)
                     VALUES ('moved', now(), '{telegram:111:0,telegram:111:1}')""")

    try:
        resegment(30, "telegram:111")
        aborted = None
    except Exception as exc:                                   # noqa: BLE001
        aborted = f"{type(exc).__name__}: {exc}"
    check("rebuild did not abort on the NO ACTION edge", aborted, None)
    check("back to 2 segments at cap 30", counts("telegram:111")[0], 2)
    check("commitment survived, resolution nulled and reopened",
          q("SELECT resolution_segment_id, status FROM commitment"), [(None, "open")])
    check("fact cascaded with its segment", q("SELECT count(*) FROM fact"), [(0,)])
    check("entity_mention cascaded", q("SELECT count(*) FROM entity_mention"), [(0,)])
    check("the fact's projection_dep row is gone",
          q("SELECT projection_kind FROM projection_dep"), [("commitment",)])
    check("every event survived",
          q("SELECT count(*) FROM event WHERE thread_key = 'telegram:111'"), [(40,)])
    check("life_event citations untouched",
          q("SELECT cardinality(source_event_ids) FROM life_event"), [(2,)])

    # --- 5. an unknown thread is a warning, not a crash --------------------
    n = q("SELECT count(*) FROM segment")[0][0]
    resegment(30, "telegram:999")
    check("unknown thread changed nothing",
          q("SELECT count(*) FROM segment")[0][0], n)

    failed = [c for c in checks if not c[1]]
    for name, ok, got, want in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}"
              + ("" if ok else f"  (got {got!r}, want {want!r})"))
    print(f"resegment-itest {'FAILED' if failed else 'OK'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
