-- chronicle_tally: the only identity caller-supplied SQL ever runs as.
--
-- /tally executes SQL the agent writes. It used to run over the api pool as
-- `chronicle`, which POSTGRES_USER makes a SUPERUSER, behind a keyword
-- blocklist. The blocklist was never a boundary: `WITH x AS (DELETE ...)
-- SELECT` passes it, and a superuser in a read-only transaction can still
-- pg_read_file() anything the server can, cancel other backends, and
-- set_config() its own statement_timeout to 0. So chronicle-api and
-- chronicle-mcp were pulled off `edge` on 2026-09-24.
--
-- The boundary is now privilege, enforced by the server:
--   * NOSUPERUSER, SELECT only    — a write fails on permission, whatever the
--                                   transaction mode or the SQL's shape.
--   * default_transaction_read_only, statement_timeout — belt and braces. Both
--                                   are user-settable, which is why the api
--                                   opens a FRESH connection per call and never
--                                   commits: set_config() inside the call
--                                   rolls back with it and cannot outlive it.
--   * CONNECTION LIMIT 4          — the api pool's own max_size; a flood of
--                                   /tally calls queues instead of starving
--                                   the recall path of backends.
--
-- No password here: the api sets a fresh random one at startup (it already
-- holds the superuser pool), so there is no secret to encrypt into .env.
-- A role with a NULL password cannot authenticate under scram-sha-256.
--
-- Idempotent: `make migrate` re-applies every file.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chronicle_tally') THEN
        CREATE ROLE chronicle_tally LOGIN;
    END IF;
END $$;

ALTER ROLE chronicle_tally NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS NOINHERIT CONNECTION LIMIT 4;
ALTER ROLE chronicle_tally SET default_transaction_read_only = on;
ALTER ROLE chronicle_tally SET statement_timeout = '10s';
ALTER ROLE chronicle_tally SET idle_in_transaction_session_timeout = '30s';

GRANT USAGE ON SCHEMA public TO chronicle_tally;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO chronicle_tally;
-- Tables the migrating role creates later. Keyed on current_user rather than
-- `chronicle` so CI, which migrates as `postgres`, runs the same file.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO chronicle_tally;
