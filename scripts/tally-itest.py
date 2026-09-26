#!/usr/bin/env python3
"""chronicle_tally must refuse everything but reading chronicle's tables.

/tally runs agent-written SQL as this role, so these are the attacks that
mattered when it ran as the superuser — each one worked then. Run by
scripts/smoke.sh against the database it has just migrated.

    python3 scripts/tally-itest.py chronicle_smoke
"""
import os
import secrets
import sys

import psycopg
from psycopg import errors, sql

DB = sys.argv[1] if len(sys.argv) > 1 else "chronicle_smoke"
pw, host, user = (os.environ.get("PGPASSWORD", ""), os.environ.get("PGHOST", "localhost"),
                  os.environ.get("PGUSER", "postgres"))
ADMIN = f"postgresql://{user}:{pw}@{host}:5432/{DB}"

# What the api does at startup.
tally_pw = secrets.token_urlsafe(16)
with psycopg.connect(ADMIN, autocommit=True) as c:
    c.execute(sql.SQL("ALTER ROLE chronicle_tally PASSWORD {}").format(sql.Literal(tally_pw)))
TALLY = f"postgresql://chronicle_tally:{tally_pw}@{host}:5432/{DB}"


def refused(query: str, *expect: type[Exception]) -> None:
    conn = psycopg.connect(TALLY)
    try:
        conn.execute(query)
    except expect:
        return
    finally:
        conn.close()
    raise AssertionError(f"chronicle_tally was allowed: {query}")


conn = psycopg.connect(TALLY)
# smoke.sql has seeded rows by now; reading them and calling the retrieval
# functions is what /tally is for.
assert conn.execute("SELECT count(*) FROM segment").fetchone()[0] > 0
conn.execute("SELECT count(*) FROM hybrid_search(NULL, 'x')").fetchone()
assert conn.execute("SHOW statement_timeout").fetchone() == ("10s",)
conn.close()

# The blocklist bypass that started this.
refused("WITH x AS (DELETE FROM segment RETURNING 1) SELECT count(*) FROM x",
        errors.ReadOnlySqlTransaction, errors.InsufficientPrivilege)
# Read-only mode off and committed: privilege still refuses the write.
refused("SELECT set_config('default_transaction_read_only','off',false); COMMIT; "
        "DELETE FROM segment", errors.InsufficientPrivilege)
# Superuser-only reach beyond the database.
refused("SELECT pg_read_file('/etc/passwd')", errors.InsufficientPrivilege)
refused("SET ROLE postgres", errors.InsufficientPrivilege)
refused("SELECT 1 FROM pg_authid", errors.InsufficientPrivilege)
refused("SELECT pg_sleep(11)", errors.QueryCanceled)

print("tally-itest OK")
