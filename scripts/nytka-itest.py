#!/usr/bin/env python3
"""End-to-end check of the Nytka source against a real PostgreSQL, twice over:
once as Nytka's database (a fixture in its schema) and once as chronicle's.

Unit tests cover the people rules (tests/test_nytka.py). This covers what only
a database shows:

    * the adapter's SQL runs, in the order the worker resumes on
    * open conversations wait, a re-run creates nothing, a conversation that
      grows is EXTENDED in place
    * a muted stretch never becomes an event
    * a conversation deleted upstream is purged; one MERGED away is not (its
      segments live on in the survivor, and purging them would be permanent)
    * a bankless source cannot reach `v_promotable_facts`, as evidence or as
      support for another source's fact (migration 008)

Run by scripts/smoke.sh, i.e. by CI, next to worker-itest.py.

    CHRONICLE_DB_URL=postgresql://... python3 scripts/nytka-itest.py
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TESTDB, SRCDB = "chronicle_nytka_itest", "nytka_itest_src"

live = os.environ.get("CHRONICLE_DB_URL")
if not live:
    pw, host, user = (os.environ.get("PGPASSWORD", ""), os.environ.get("PGHOST", "localhost"),
                      os.environ.get("PGUSER", "postgres"))
    live = f"postgresql://{user}:{pw}@{host}:5432/postgres"
base = live.rsplit("/", 1)[0]
ADMIN, URL, SRC = f"{base}/postgres", f"{base}/{TESTDB}", f"{base}/{SRCDB}"
assert TESTDB not in live, "refusing to run against the configured database"

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)      # a Monday, 15:00 in Kyiv
A, B, C, D, M = (str(uuid.uuid4()) for _ in range(5))

# The subset of Nytka's schema the adapter reads (server/db/migrations 0001-0010).
NYTKA_DDL = """
CREATE TABLE conversations (id uuid PRIMARY KEY, started_at timestamptz NOT NULL,
    ended_at timestamptz NOT NULL, status text NOT NULL, created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL, source text NOT NULL DEFAULT 'nytka', external_id text);
CREATE TABLE segments (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    started_at timestamptz NOT NULL, ended_at timestamptz NOT NULL, text text NOT NULL,
    speaker text, speaker_id text, is_user boolean);
CREATE TABLE people (id uuid PRIMARY KEY, name text NOT NULL);
CREATE TABLE person_voices (speaker_id text PRIMARY KEY,
    person_id uuid NOT NULL REFERENCES people (id) ON DELETE CASCADE);
CREATE TABLE settings (key text PRIMARY KEY, value text NOT NULL, updated_at timestamptz NOT NULL);
"""


class Stub(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint that counts the calls it gets."""
    calls = 0

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        Stub.calls += 1
        body = json.dumps({"choices": [{"message": {"content": json.dumps(
            {"summary": "s", "topics": [], "importance": 1, "sentiment": 0,
             "facts": [], "commitments": []})}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main() -> int:
    with psycopg.connect(ADMIN, autocommit=True) as c:
        for db in (TESTDB, SRCDB):
            c.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {db}")
    try:
        return run()
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as c:
            for db in (TESTDB, SRCDB):
                c.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def run() -> int:
    mig = os.path.join(ROOT, "migrations")
    for m in sorted(os.listdir(mig)):
        with psycopg.connect(URL, autocommit=True) as c, open(os.path.join(mig, m)) as fh:
            c.execute(fh.read())
    with psycopg.connect(SRC, autocommit=True) as c:
        c.execute(NYTKA_DDL)

    for k in [k for k in os.environ if k.endswith(("_DB_URL", "_DB_PATH"))]:
        del os.environ[k]
    os.environ.update(NYTKA_DB_URL=SRC, CHRONICLE_DB_URL=URL)

    from chronicle import purge, worker
    worker.DB_URL = URL
    args = SimpleNamespace(tier=4)

    def src(sql, *p):
        with psycopg.connect(SRC, autocommit=True) as c:
            c.execute(sql, p)

    def q(sql, *p):
        with psycopg.connect(URL) as c:
            return c.execute(sql, p).fetchall()

    def conv(cid, status, updated, source="nytka"):
        src("INSERT INTO conversations (id, started_at, ended_at, status, created_at, updated_at,"
            " source) VALUES (%s,%s,%s,%s,%s,%s,%s)", cid, T0, T0, status, T0, updated, source)

    def say(cid, minute, text, who="SPEAKER_01", sid="v1", user=False):
        src("INSERT INTO segments (conversation_id, started_at, ended_at, text, speaker,"
            " speaker_id, is_user) VALUES (%s,%s,%s,%s,%s,%s,%s)", cid,
            T0 + timedelta(minutes=minute), T0 + timedelta(minutes=minute, seconds=5),
            text, who, sid, user)

    def minutes_after(n):
        return T0 + timedelta(minutes=n)

    checks = []

    def check(name, got, want):
        checks.append((name, got == want, got, want))

    # --- the fixture: settings, a named voice, four conversations ---------------
    src("INSERT INTO settings VALUES ('user.timeZone','Europe/Kyiv',now()),"
        " ('mute.windows','[{\"days\":[1],\"start\":\"15:30\",\"end\":\"16:00\"}]',now())")
    pid = str(uuid.uuid4())
    src("INSERT INTO people VALUES (%s,'Anna')", pid)
    src("INSERT INTO person_voices VALUES ('v1',%s)", pid)

    conv(A, "closed", minutes_after(10))
    for i in range(4):
        say(A, i, f"we should move the flat viewing to saturday, variant {i}, call the agent")
    say(A, 4, "yes please, book it", who="SPEAKER_00", sid="v0", user=True)
    say(A, 5, "and the other one?", who="SPEAKER_02", sid="v2")
    say(A, 6, "   ")                                      # blank: never an event
    conv(B, "open", minutes_after(11))                    # still growing: waits
    say(B, 7, "this conversation has not closed yet, so it is not read")
    conv(M, "closed", minutes_after(40))                  # 15:30-15:35 Kyiv = muted
    say(M, 30, "this was said inside the mute window and must stay out")
    say(M, 31, "so was this one, the second utterance in the window")
    say(M, 65, "this one is after the window ends, at sixteen-oh-five")
    conv(C, "closed", minutes_after(12))
    for i in range(3):
        say(C, 60 + i, f"about the other topic, number {i}, the deposit and the lease terms")
    conv(D, "closed", minutes_after(13), source="omi")
    for i in range(3):
        say(D, 70 + i, f"an imported conversation, line {i}, about the trip to the mountains")

    # --- run 1 ------------------------------------------------------------------
    worker.cmd_ingest(args)
    check("run 1: open and muted utterances stay out; blank is no event",
          q("SELECT count(*) FROM event WHERE source = 'nytka'")[0][0], 6 + 1 + 3 + 3)
    check("run 1: the open conversation is not read",
          q("SELECT count(*) FROM event WHERE thread_key = %s", f"nytka:{B}")[0][0], 0)
    check("run 1: the utterance after the window is read",
          q("SELECT count(*) FROM event WHERE thread_key = %s", f"nytka:{M}")[0][0], 1)
    check("run 1: actors", sorted(r[0] for r in q(
              "SELECT DISTINCT actor FROM event WHERE thread_key = %s", f"nytka:{A}")),
          ["Anna", "SPEAKER_02", "me"])
    check("run 1: the source row has no Hindsight bank",
          q("SELECT hindsight_bank, density FROM source WHERE source = 'nytka'")[0],
          (None, "narrative"))
    check("run 1: the watermark is the newest updated_at read",
          q("SELECT last_ingested_at FROM source WHERE source = 'nytka'")[0][0],
          minutes_after(40))

    worker.cmd_ingest(args)
    check("run 2: a re-run reads the overlap and writes nothing new",
          q("SELECT count(*) FROM event WHERE source = 'nytka'")[0][0], 13)

    worker.cmd_segment(args)
    check("run 2: one segment per conversation read",
          sorted(q("SELECT thread_key, event_count FROM segment")),
          sorted([(f"nytka:{A}", 6), (f"nytka:{M}", 1), (f"nytka:{C}", 3), (f"nytka:{D}", 3)]))
    check("run 2: the header says Nytka, not a UUID",
          q("SELECT bool_and(embed_text LIKE '[chat: Nytka]%%') FROM segment")[0][0], True)
    check("run 2: Anna is named in the header",
          q("SELECT embed_text LIKE '%%[with: Anna, me, SPEAKER_02]%%' FROM segment"
            " WHERE thread_key = %s", f"nytka:{A}")[0][0], True)
    check("run 2: the long conversation is substantive, the lone short line is not",
          dict(q("SELECT thread_key, is_substantive FROM segment")),
          {f"nytka:{A}": True, f"nytka:{M}": False, f"nytka:{C}": True, f"nytka:{D}": True})

    # --- the open conversation closes, another grows -----------------------------
    src("UPDATE conversations SET status = 'closed', updated_at = %s WHERE id = %s",
        minutes_after(90), B)
    say(A, 20, "one more thing about the viewing, bring the passport", who="SPEAKER_00",
        sid="v0", user=True)
    src("UPDATE conversations SET updated_at = %s WHERE id = %s", minutes_after(91), A)
    worker.cmd_ingest(args)
    worker.cmd_segment(args)
    check("run 3: the closed one and the new line arrived",
          q("SELECT count(*) FROM event WHERE source = 'nytka'")[0][0], 15)
    check("run 3: the grown conversation was extended in place",
          q("SELECT count(*), max(event_count) FROM segment WHERE thread_key = %s",
            f"nytka:{A}")[0], (1, 7))

    # --- erasure -----------------------------------------------------------------
    # C is deleted upstream. D is MERGED into A: its segments move, its row goes.
    src("DELETE FROM conversations WHERE id = %s", C)
    src("UPDATE segments SET conversation_id = %s WHERE conversation_id = %s", A, D)
    src("DELETE FROM conversations WHERE id = %s", D)
    src("UPDATE conversations SET updated_at = %s WHERE id = %s", minutes_after(120), A)
    worker.cmd_ingest(args)
    check("merge: the survivor re-read changes no event",
          q("SELECT count(*) FROM event WHERE source = 'nytka'")[0][0], 15)

    from chronicle.doctor import build
    ad = build("nytka")
    check("erasure: only the deleted conversation is excluded; the merged-away one is not",
          ad.excluded_thread_keys(), {f"nytka:{C}"})
    gone = purge.collect(["nytka"])
    check("erasure: purge.collect agrees", gone, {"nytka": {f"nytka:{C}"}})

    conn = worker.connect()
    try:
        purge._purge(conn, "nytka", gone["nytka"])
        conn.commit()
    finally:
        conn.close()
    check("erasure: the deleted conversation is gone from events and segments",
          q("SELECT (SELECT count(*) FROM event WHERE thread_key = %s),"
            " (SELECT count(*) FROM segment WHERE thread_key = %s)",
            f"nytka:{C}", f"nytka:{C}")[0], (0, 0))
    check("erasure: the merged-away conversation's utterances are still indexed",
          q("SELECT count(*) FROM event WHERE thread_key = %s", f"nytka:{D}")[0][0], 3)

    # Deleting the survivor takes the merged thread with it: no segment survives.
    src("DELETE FROM conversations WHERE id = %s", A)
    check("erasure: a deleted survivor takes its merged threads too",
          purge.collect(["nytka"]), {"nytka": {f"nytka:{A}", f"nytka:{D}"}})
    os.environ["NYTKA_EXCLUDE_CONVERSATIONS"] = M
    check("erasure: an explicit exclusion is named for purge as well",
          build("nytka").excluded_thread_keys() >= {f"nytka:{M}"}, True)
    del os.environ["NYTKA_EXCLUDE_CONVERSATIONS"]

    # --- enrichment ---------------------------------------------------------------
    # Other people's speech is not sent to a cloud model, and enrich is the only
    # path that would. Run it with a live endpoint: it must receive no call.
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ.update(ENRICH_MODEL="stub", LITELLM_API_KEY="k", ENRICH_LIMIT="50",
                      ENRICH_URL=f"http://127.0.0.1:{srv.server_port}/v1")
    check("enrich: the stub is reachable and the queue would not be empty",
          q("SELECT count(*) FROM segment s JOIN source r ON r.source = s.sources[1]"
            " WHERE s.is_substantive AND s.enriched_at IS NULL AND r.source = 'nytka'")[0][0] > 0,
          True)
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("INSERT INTO fact_predicate (predicate) VALUES ('likes')"
                  " ON CONFLICT DO NOTHING")
    worker.cmd_enrich(SimpleNamespace())
    check("enrich: no call reached the model for a bankless source", Stub.calls, 0)
    srv.shutdown()

    # --- promotion ---------------------------------------------------------------
    # Five segments by hand: three telegram ones in two threads, two nytka ones.
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("INSERT INTO source (source, hindsight_bank) VALUES ('telegram','personal')"
                  " ON CONFLICT (source) DO UPDATE SET hindsight_bank = 'personal'")
        c.execute("INSERT INTO fact_predicate (predicate) VALUES ('likes')"
                  " ON CONFLICT DO NOTHING")
        seg = {}
        for name, thread, srcname in [("t1", "telegram:1", "telegram"), ("t2", "telegram:1", "telegram"),
                                      ("t3", "telegram:2", "telegram"), ("n1", "nytka:x", "nytka"),
                                      ("n2", "nytka:y", "nytka")]:
            seg[name] = c.execute(
                """INSERT INTO segment (thread_key, sources, started_at, ended_at, event_count,
                       source_event_ids, raw_text, embed_text, segmenter_version)
                   VALUES (%s, ARRAY[%s], now(), now(), 1, ARRAY[%s], 'x', 'x', 'v1')
                   RETURNING segment_id""", (thread, srcname, f"{srcname}:{name}")).fetchone()[0]
        ents = {}
        for name, mentions in [("plain", ["t1", "t2", "t3"]),         # telegram only
                               ("padded", ["t1", "n1", "n2"]),        # nytka lifts it over 3/2
                               ("spoken", ["t1", "t2", "t3"])]:       # telegram, nytka evidence
            ents[name] = c.execute(
                "INSERT INTO entity (entity_type, canonical_name, extractor_version)"
                " VALUES ('person', %s, 'v1') RETURNING entity_id", (name,)).fetchone()[0]
            for m in mentions:
                c.execute("INSERT INTO entity_mention (entity_id, segment_id, ts)"
                          " VALUES (%s,%s,now())", (ents[name], seg[m]))
        for name, evidence in [("plain", "telegram:t1"), ("padded", "telegram:t1"),
                               ("spoken", "nytka:n1")]:
            c.execute("""INSERT INTO fact (subject_id, predicate, t_valid, version, confidence,
                           source_event_ids, extractor_version)
                         VALUES (%s, 'likes', now(), 1, 0.9, ARRAY[%s], 'v1')""",
                      (ents[name], evidence))
    check("promotion: only the telegram-only fact is promotable",
          [r[0] for r in q("""SELECT e.canonical_name FROM v_promotable_facts v
                                JOIN entity e ON e.entity_id = v.subject_id""")], ["plain"])

    bad = [c for c in checks if not c[1]]
    for name, ok, got, want in checks:
        print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"\n      got  {got!r}\n      want {want!r}"))
    if bad:
        print(f"\nnytka-itest FAILED: {len(bad)} of {len(checks)}")
        return 1
    print(f"\nnytka-itest OK: {len(checks)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
