#!/usr/bin/env python3
"""End-to-end check of `python -m chronicle.redact --apply`.

A secret already stored lives in three places the backfill must reach in one
transaction: the event, the segment's raw_text/embed_text built from it, and
the segment's embedding (which encodes it and must be cleared). A dry run
must change nothing, and a second --apply must find nothing.

    CHRONICLE_DB_URL=postgresql://... python3 scripts/redact-itest.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TESTDB = "chronicle_redact_itest"

live = os.environ.get("CHRONICLE_DB_URL")
if not live:
    pw, host, user = (os.environ.get("PGPASSWORD", ""), os.environ.get("PGHOST", "localhost"),
                      os.environ.get("PGUSER", "postgres"))
    live = f"postgresql://{user}:{pw}@{host}:5432/postgres"
base = live.rsplit("/", 1)[0]
ADMIN, URL = f"{base}/postgres", f"{base}/{TESTDB}"
assert TESTDB not in live, "refusing to run against the configured database"

T0 = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
# Assembled at runtime so gitleaks and the pre-commit denylist stay quiet.
SECRET = "gh" + "p_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"


def main() -> int:
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TESTDB}")
        c.execute(f"CREATE DATABASE {TESTDB}")
    try:
        return run()
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {TESTDB} WITH (FORCE)")


def run() -> int:
    mig = os.path.join(ROOT, "migrations")
    for m in sorted(os.listdir(mig)):
        with psycopg.connect(URL, autocommit=True) as c, open(os.path.join(mig, m)) as fh:
            c.execute(fh.read())

    os.environ["CHRONICLE_DB_URL"] = URL
    from chronicle import redact, worker
    worker.DB_URL = URL

    def q(sql, *p):
        with psycopg.connect(URL) as c:
            return c.execute(sql, p).fetchall()

    checks = []

    def check(name, got, want):
        checks.append((name, got == want, got, want))

    with psycopg.connect(URL) as c:
        c.execute("INSERT INTO source (source, density) VALUES ('telegram', 'narrative')"
                  " ON CONFLICT (source) DO UPDATE SET density = 'narrative'")
        c.cursor().executemany(
            "INSERT INTO event (source, source_id, ts, kind, text, payload, thread_key)"
            " VALUES ('telegram', %s, %s, 'message', %s, %s::jsonb, 'telegram:1')",
            [("1:0", T0, "here is the deploy token", "{}"),
             ("1:1", T0 + timedelta(minutes=1), SECRET, "{}"),
             ("1:2", T0 + timedelta(minutes=2), "thanks, rotating it now",
              '{"url": "https://me:' + "pw123456" + '@host/x"}')])
    worker.cmd_segment(SimpleNamespace(tier=1))
    with psycopg.connect(URL) as c:
        c.execute("UPDATE segment SET embedding = array_fill(0.1, ARRAY[1024])::halfvec,"
                  " embedder_version = 'x'")

    check("fixture: the secret reached the segment",
          q("SELECT count(*) FROM segment WHERE raw_text LIKE %s", f"%{SECRET}%"), [(1,)])

    redact.main([])
    check("dry run changed nothing",
          q("SELECT count(*) FROM event WHERE text = %s", SECRET), [(1,)])

    redact.main(["--apply"])
    check("event text redacted",
          q("SELECT text FROM event WHERE source_id = '1:1'"),
          [("[REDACTED:github_token]",)])
    check("payload redacted",
          q("SELECT payload->>'url' FROM event WHERE source_id = '1:2'"),
          [("https://[REDACTED:url_password]@host/x",)])
    check("no segment text holds it",
          q("SELECT count(*) FROM segment WHERE raw_text LIKE %s OR embed_text LIKE %s",
            f"%{SECRET}%", f"%{SECRET}%"), [(0,)])
    check("the segment kept its surrounding prose",
          q("SELECT raw_text LIKE '%%deploy token%%' AND raw_text LIKE '%%[REDACTED:github_token]%%'"
            " FROM segment"), [(True,)])
    check("its embedding was cleared for re-encode",
          q("SELECT count(*) FROM segment WHERE embedding IS NULL"), [(1,)])
    check("erasure_log row written, counts only",
          q("SELECT scope, (scope_ref->>'events')::int FROM erasure_log"),
          [("secret_redaction", 2)])
    check("the log never holds the value",
          q("SELECT count(*) FROM erasure_log WHERE scope_ref::text LIKE %s",
            f"%{SECRET}%"), [(0,)])

    hits = redact.scan(psycopg.connect(URL))
    check("a second pass finds nothing", len(hits), 0)

    failed = [c for c in checks if not c[1]]
    for name, ok, got, want in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}"
              + ("" if ok else f"  (got {got!r}, want {want!r})"))
    print(f"redact-itest {'FAILED' if failed else 'OK'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
