#!/usr/bin/env python3
"""End-to-end check that the worker is safe to run on a SCHEDULE.

It was not, until 2026-09-26: `segment` re-read every thread and inserted
with no conflict target, so a second run duplicated every segment, and ingest
dropped text that changed after a row was first seen. Both are properties of
running the pipeline twice against a real database, which no unit test can
exercise, so this runs it several times and checks what accumulated.

    * a re-run creates nothing                     (idempotent)
    * new messages extend the open conversation     (no cut at a run boundary)
    * a transcript landing on an old voice note     (CDC reaches the segment)
      rewrites the segment, clears its embedding
    * enrich writes facts/commitments/citations,    (and a second pass
      and re-enriching replaces them                 supersedes the first)

Run by scripts/smoke.sh, i.e. by CI, next to purge-itest.py.

    CHRONICLE_DB_URL=postgresql://... python3 scripts/worker-itest.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import psycopg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TESTDB = "chronicle_worker_itest"

live = os.environ.get("CHRONICLE_DB_URL")
if not live:
    pw, host, user = (os.environ.get("PGPASSWORD", ""), os.environ.get("PGHOST", "localhost"),
                      os.environ.get("PGUSER", "postgres"))
    live = f"postgresql://{user}:{pw}@{host}:5432/postgres"
base = live.rsplit("/", 1)[0]
ADMIN, URL = f"{base}/postgres", f"{base}/{TESTDB}"
assert TESTDB not in live, "refusing to run against the configured database"

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
#  a telegram-sync that can be written to between runs
# ---------------------------------------------------------------------------

class Telegram:
    def __init__(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.clock = T0
        s = sqlite3.connect(self.path)
        s.execute("""CREATE TABLE messages (id INTEGER, chat_id INTEGER, chat_title TEXT,
            chat_type TEXT, sender_id INTEGER, sender_name TEXT, text TEXT, date TEXT,
            is_outgoing INTEGER, reply_to_id INTEGER, synced_at TEXT, media_type TEXT,
            PRIMARY KEY (chat_id, id))""")
        s.execute("CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT,"
                  " username TEXT, included INTEGER, updated_at TEXT)")
        s.execute("CREATE TABLE chat_tags (chat_id INTEGER, tag TEXT, source TEXT,"
                  " created_at TEXT, PRIMARY KEY (chat_id, tag))")
        s.execute("INSERT INTO chats VALUES (111, 'Anna', 'user', 'anna', 1, '')")
        s.commit()
        s.close()

    def _tick(self) -> str:
        # synced_at is WRITE time and only moves forward, like telegram-sync's.
        self.clock += timedelta(hours=1)
        return self.clock.isoformat(timespec="microseconds")

    def say(self, msg_id: int, minute: int, text: str, media: str | None = None,
            who: str = "Anna") -> None:
        s = sqlite3.connect(self.path)
        s.execute("INSERT INTO messages VALUES (?,111,'Anna','user',1,?,?,?,?,NULL,?,?)",
                  (msg_id, "me" if who == "me" else "Anna", text,
                   (T0 + timedelta(minutes=minute)).isoformat(), int(who == "me"),
                   self._tick(), media))
        s.commit()
        s.close()

    def transcribe(self, msg_id: int, text: str) -> None:
        s = sqlite3.connect(self.path)
        s.execute("UPDATE messages SET text = ?, synced_at = ? WHERE chat_id = 111 AND id = ?",
                  (text, self._tick(), msg_id))
        s.commit()
        s.close()


# ---------------------------------------------------------------------------
#  an OpenAI-compatible stub for enrich
# ---------------------------------------------------------------------------

REPLY = {"summary": "Anna and Sam planned the move to Lviv.",
         "topics": ["relocation", "Apartment", "relocation"],
         "importance": 3, "sentiment": 0.4,
         "facts": [{"subject": "Anna", "predicate": "lives_in", "object": "Lviv",
                    "confidence": 0.9},
                   {"subject": "me", "predicate": "plans", "object": "visit Lviv",
                    "confidence": 0.8},
                   {"subject": "Anna", "predicate": "is_cool", "object": "yes"}],
         "commitments": [{"text": "send the lease", "direction": "i_owe",
                          "due": "2026-09-10", "confidence": 0.7}]}


class Stub(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        Stub.calls += 1
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"choices": [{"message": {"content":
                           "```json\n" + json.dumps(REPLY) + "\n```"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


# ---------------------------------------------------------------------------

def main() -> int:
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TESTDB}")
        c.execute(f"CREATE DATABASE {TESTDB}")
    try:
        return run()
    finally:
        # FORCE: the worker's commands hold their connection for the process
        # lifetime (they are one-shot CLI calls), and this process is five runs.
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {TESTDB} WITH (FORCE)")


def run() -> int:
    mig = os.path.join(ROOT, "migrations")
    for m in sorted(os.listdir(mig)):
        with psycopg.connect(URL, autocommit=True) as c, open(os.path.join(mig, m)) as fh:
            c.execute(fh.read())

    tg = Telegram()
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ.update(TELEGRAM_DB_URL=tg.path, CHRONICLE_DB_URL=URL,
                      ENRICH_MODEL="stub", LITELLM_API_KEY="k", CHRONICLE_OWNER="Sam",
                      ENRICH_URL=f"http://127.0.0.1:{srv.server_port}/v1")

    from chronicle import worker
    worker.DB_URL = URL
    args = SimpleNamespace(tier=1)

    def q(sql, *p):
        with psycopg.connect(URL) as c:
            return c.execute(sql, p).fetchall()

    def fake_embed():
        # `embed` needs BGE-M3; what matters here is that it gets CLEARED.
        with psycopg.connect(URL) as c:
            c.execute("""UPDATE segment SET embedding = array_fill(0.1, ARRAY[1024])::halfvec,
                                            embedder_version = 'x'""")

    checks = []

    def check(name, got, want):
        checks.append((name, got == want, got, want))

    # --- run 1: five messages, one a voice note with no transcript yet -----
    for i in range(4):
        tg.say(i, i, f"про квартиру, варіант {i}, дзвонив рієлтор")
    tg.say(4, 4, "", media="voice")
    worker.cmd_ingest(args); worker.cmd_segment(args); fake_embed()
    check("run 1: one segment", q("SELECT count(*) FROM segment")[0][0], 1)
    check("run 1: it holds all five", q("SELECT event_count FROM segment")[0][0], 5)
    check("run 1: header names the chat",
          q("SELECT embed_text LIKE '[chat: Anna] [with: Anna]%%' FROM segment")[0][0], True)

    # --- run 2: nothing new -> nothing changes --------------------------------
    worker.cmd_ingest(args); worker.cmd_segment(args)
    check("run 2: re-run creates nothing", q("SELECT count(*) FROM segment")[0][0], 1)
    check("run 2: re-run keeps the embedding",
          q("SELECT count(*) FROM segment WHERE embedding IS NOT NULL")[0][0], 1)

    # --- run 3: the voice note is transcribed, the chat continues -------------
    tg.transcribe(4, "[voice] трикімнатна за вісімсот")
    tg.say(5, 6, "ок, беру", who="me")
    tg.say(6, 7, "домовились на суботу")
    worker.cmd_ingest(args)
    check("run 3: transcript reached the event",
          q("SELECT text FROM event WHERE source_id = '111:4'")[0][0],
          "[voice] трикімнатна за вісімсот")
    check("run 3: ...and the segment citing it",
          q("SELECT raw_text LIKE '%%трикімнатна%%' FROM segment")[0][0], True)
    check("run 3: ...whose embedding is now stale",
          q("SELECT embedding IS NULL FROM segment")[0][0], True)
    worker.cmd_segment(args)
    check("run 3: the conversation EXTENDED, not split",
          q("SELECT count(*), max(event_count) FROM segment")[0], (1, 7))

    # --- run 4: five hours later is a new conversation -----------------------
    for i in range(3):
        tg.say(10 + i, 300 + i, f"нова тема {i}: відпустка, квитки, готель")
    worker.cmd_ingest(args); worker.cmd_segment(args)
    check("run 4: a gap makes a new segment", q("SELECT count(*) FROM segment")[0][0], 2)
    check("run 4: no event is cited twice",
          q("""SELECT count(*), count(DISTINCT k) FROM
                 (SELECT unnest(source_event_ids) k FROM segment) x""")[0], (10, 10))

    # --- run 5: an empty overwrite is refused ---------------------------------
    tg.transcribe(4, "")
    worker.cmd_ingest(args)
    check("run 5: empty text never overwrites",
          q("SELECT text FROM event WHERE source_id = '111:4'")[0][0],
          "[voice] трикімнатна за вісімсот")

    # --- enrich ---------------------------------------------------------------
    fake_embed()
    worker.cmd_enrich(args)
    check("enrich: both segments sent", Stub.calls, 2)
    check("enrich: unknown predicate dropped, 2 facts per segment",
          q("SELECT count(*) FROM fact")[0][0], 4)
    check("enrich: 'me' resolves to the owner",
          q("SELECT count(*) FROM entity WHERE canonical_name = 'Sam'")[0][0], 1)
    check("enrich: single-valued lives_in, only the newest is current",
          q("""SELECT count(*) FROM fact WHERE predicate = 'lives_in'
                  AND t_invalid IS NULL""")[0][0], 1)
    check("enrich: commitments written", q("SELECT count(*) FROM commitment")[0][0], 2)
    check("enrich: every projection cited",
          q("""SELECT count(DISTINCT (projection_kind, projection_id))
                 FROM projection_dep""")[0][0], 6)
    check("enrich: topics deduped and clamped",
          q("SELECT topics FROM segment ORDER BY started_at LIMIT 1")[0][0],
          ["relocation", "apartment"])
    check("enrich: importance clamped to 1", q("SELECT max(importance) FROM segment")[0][0], 1.0)
    check("enrich: facts land in enrich_text",
          q("""SELECT bool_and(enrich_text LIKE '%%Anna lives in Lviv%%')
                 FROM segment""")[0][0], True)
    check("enrich: embed_text left as it was",
          q("SELECT bool_and(embed_text NOT LIKE '%%|| facts:%%') FROM segment")[0][0], True)
    check("enrich: and the embedding kept",
          q("SELECT count(*) FROM segment WHERE embedding IS NULL")[0][0], 0)

    # A text change re-opens the segment; re-enriching must REPLACE its facts.
    tg.transcribe(4, "[voice] трикімнатна за вісімсот, без меблів")
    worker.cmd_ingest(args)
    check("re-enrich: text change re-opens enrichment",
          q("SELECT count(*) FROM segment WHERE enriched_at IS NULL")[0][0], 1)
    worker.cmd_enrich(args)
    check("re-enrich: facts replaced, not duplicated", q("SELECT count(*) FROM fact")[0][0], 4)
    check("re-enrich: citations replaced too",
          q("""SELECT count(DISTINCT (projection_kind, projection_id))
                 FROM projection_dep""")[0][0], 6)

    # A dead endpoint stops after ONE batch instead of burning ENRICH_LIMIT
    # calls, and `all` still embeds but exits non-zero (nightly.sh -> ntfy).
    tg.transcribe(4, "[voice] трикімнатна, без меблів, з балконом")
    worker.cmd_ingest(args)
    Stub.calls = 0
    os.environ["ENRICH_URL"] = "http://127.0.0.1:9/v1"       # nothing listens
    rc = worker.cmd_enrich(args)
    check("dead endpoint: enrich reports failure", rc, 1)
    check("dead endpoint: nothing half-written",
          q("SELECT count(*) FROM segment WHERE enriched_at IS NULL")[0][0], 1)
    os.environ["ENRICH_URL"] = f"http://127.0.0.1:{srv.server_port}/v1"

    srv.shutdown()
    width = max(len(c[0]) for c in checks)
    bad = 0
    for name, ok, got, want in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {name:<{width}}  {got!r}"
              + ("" if ok else f"  (want {want!r})"))
        bad += not ok
    print("worker-itest", "OK" if not bad else f"FAILED ({bad})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
