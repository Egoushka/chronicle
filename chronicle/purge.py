"""Erasure — delete what the adapters' filters now exclude but once ingested.

Source filters are FETCH-time. Tightening one stops the next ingest and does
nothing at all to the rows a laxer rule already indexed, which stay retrievable
through `/recall` and the MCP surface forever. This is the other half.

    python -m chronicle.purge                 # dry run: counts, changes nothing
    python -m chronicle.purge --apply         # commit, in one transaction
    python -m chronicle.purge --source telegram

Run it in chronicle-WORKER on the box. Working out what to delete means
reading each source's own database, and the worker is the only container that
mounts them; chronicle-api inherits TELEGRAM_DB_URL from env_file, has no
/srv/telegram, and dies on `unable to open database file`.

The first real use was the bot chats: `personal_only` never kept them out
(hard-won fact 27), and the assistant's own bot chat had 138 events and 21 embedded
segments in the live database before JARVIS existed — an assistant reading its
own prior output back as external memory about the owner.

Why a target and not a one-off DELETE: exclusion rules change. They changed
once already, they will change again, and re-running an erasure should be
boring. The rules themselves live in each adapter's `excluded_thread_keys`,
never here, so this file never needs editing when one moves.

DELETION ORDER IS LOAD-BEARING. `segment` cascades to `entity_mention`,
`fact.source_segment_id` and `commitment.source_segment_id`, but three edges
are NOT cascades and are silently wrong if you skip them — see `_purge`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

log = logging.getLogger("chronicle.purge")


def collect(sources: list[str] | None = None) -> dict[str, set[str]]:
    """Ask every configured adapter which thread keys its filters exclude.

    Unconfigured sources yield nothing rather than raising: the same "any
    subset of sources" contract `doctor.build` documents. That is also the one
    real footgun here — purging with TELEGRAM_DB_URL unset reports zero to
    delete and exits 0, which looks identical to "already clean". `main`
    refuses an --apply that found no adapters at all for exactly that reason.
    """
    from .adapters import ADAPTERS
    from .doctor import build

    out: dict[str, set[str]] = {}
    # ADAPTERS, not sources.POLICIES: `build` resolves against the adapter
    # registry, so anything not in it can never be purged either.
    for name in (sources or sorted(ADAPTERS)):
        try:
            adapter = build(name)
        except Exception as exc:                        # noqa: BLE001
            log.warning("%s: cannot build adapter (%s) — SKIP", name, exc)
            continue
        if adapter is None:
            continue
        keys = adapter.excluded_thread_keys()
        if keys:
            out[name] = keys
        log.info("%s: %d excluded thread_key(s)", name, len(keys))
    return out


# ---------------------------------------------------------------------------
#  counting and deleting
# ---------------------------------------------------------------------------

_COUNTS = """
SELECT (SELECT count(*) FROM event   WHERE source = %(src)s AND thread_key = ANY(%(tk)s)),
       (SELECT count(*) FROM segment WHERE thread_key = ANY(%(tk)s)),
       (SELECT count(*) FROM segment WHERE thread_key = ANY(%(tk)s) AND embedding IS NOT NULL),
       (SELECT count(*) FROM entity_mention m JOIN segment s USING (segment_id)
         WHERE s.thread_key = ANY(%(tk)s)),
       (SELECT count(*) FROM fact f JOIN segment s ON s.segment_id = f.source_segment_id
         WHERE s.thread_key = ANY(%(tk)s)),
       (SELECT count(*) FROM commitment c JOIN segment s ON s.segment_id = c.source_segment_id
         WHERE s.thread_key = ANY(%(tk)s)),
       (SELECT count(*) FROM commitment c JOIN segment s ON s.segment_id = c.resolution_segment_id
         WHERE s.thread_key = ANY(%(tk)s))
"""

FIELDS = ("events", "segments", "embedded", "entity_mentions",
          "facts", "commitments", "commitments_resolved_by")


def count(conn, source: str, keys: set[str]) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(_COUNTS, {"src": source, "tk": sorted(keys)})
        return dict(zip(FIELDS, cur.fetchone()))


def _purge(conn, source: str, keys: set[str]) -> int:
    """Delete one source's excluded threads. Caller owns the transaction."""
    tk = sorted(keys)
    p = {"src": source, "tk": tk}

    with conn.cursor() as cur:
        # 1. commitment.resolution_segment_id is ON DELETE NO ACTION (verified
        #    against the live catalog: confdeltype='a', while the other three
        #    segment edges are 'c'). A commitment RESOLVED BY a doomed segment
        #    therefore aborts the whole delete with a foreign key violation.
        #    Null it rather than deleting the commitment: the commitment itself
        #    is evidence from a chat that survives, only its resolution is
        #    being erased, and dropping it would delete a fact nobody asked to
        #    forget. Zero rows today — enrich is stubbed — which is exactly why
        #    this has to be written now rather than discovered later.
        cur.execute("""
            UPDATE commitment SET resolution_segment_id = NULL, status = 'open'
             WHERE resolution_segment_id IN
                   (SELECT segment_id FROM segment WHERE thread_key = ANY(%(tk)s))
        """, p)
        unresolved = cur.rowcount

        # 2. life_event has no FK at all — source_event_ids is a bare TEXT[].
        #    Prune the doomed ids out of each array, then delete only the rows
        #    THIS prune emptied. A life_event spanning a bot chat and a real one
        #    keeps the real half; `fact` has a CHECK saying evidence must be
        #    non-empty and the same principle applies here by hand.
        #
        #    `<> ALL`, never `<> ANY`. `x = ANY(arr)` and `x <> ANY(arr)` are
        #    not opposites: the second is true whenever arr holds any element
        #    that differs from x, so with 94 thread keys it is true for every
        #    row and the prune silently does nothing. Caught by the integration
        #    check, which is the only reason it is not still in here.
        #
        #    Ids here are the SEGMENT-side format `telegram:{chat}:{msg}` —
        #    source-prefixed, unlike `event.source_id` — so the chat_id is
        #    field 2. Verified live: `segment.source_event_ids[1]` is
        #    `telegram:123456789:4242` while the same event's
        #    `event.source_id` is `123456789:4242`.
        #    Drop-then-prune, as two statements over DISJOINT row sets, never
        #    one statement with a data-modifying CTE. PostgreSQL gives every
        #    sub-statement of a single query the same snapshot, so a DELETE CTE
        #    cannot see rows an UPDATE CTE emptied and silently deletes nothing
        #    — measured, `life_dropped` came back 0 with the row sitting there.
        doomed_id = ("%(src)s || ':' || split_part(x, ':', 2) = ANY(%(tk)s)")
        cur.execute(f"""
            DELETE FROM life_event le
             WHERE cardinality(le.source_event_ids) > 0
               AND NOT EXISTS (SELECT 1 FROM unnest(le.source_event_ids) x
                                WHERE NOT ({doomed_id}))
        """, p)
        life_dropped = cur.rowcount
        cur.execute(f"""
            UPDATE life_event le
               SET source_event_ids = ARRAY(
                     SELECT x FROM unnest(le.source_event_ids) x
                      WHERE %(src)s || ':' || split_part(x, ':', 2) <> ALL(%(tk)s))
             WHERE EXISTS (SELECT 1 FROM unnest(le.source_event_ids) x
                            WHERE {doomed_id})
        """, p)
        life_pruned = cur.rowcount

        # 3. projection_dep is the refcount graph and also has no FK, so
        #    nothing cascades into it. Keyed on (source, source_id) — the
        #    EVENT's ids, unprefixed, matching `event.source_id`.
        cur.execute("""
            DELETE FROM projection_dep pd
             WHERE pd.source = %(src)s
               AND pd.source_id IN (SELECT source_id FROM event
                                     WHERE source = %(src)s AND thread_key = ANY(%(tk)s))
        """, p)
        deps = cur.rowcount

        # 4. segment. Cascades entity_mention, fact and commitment.source_.
        #    `embedding` is a COLUMN on this row, not a side table, so the
        #    vectors go with it and nothing that survives is re-embedded —
        #    the remaining corpus is ~9.8 h of CPU.
        cur.execute("DELETE FROM segment WHERE thread_key = ANY(%(tk)s)", p)
        segments = cur.rowcount

        # 5. event, last: the segments cited it. Partitioned by RANGE (ts), so
        #    this DELETE on the parent fans out to every year partition that
        #    holds a matching row; `event_thread_idx (thread_key, ts)` exists
        #    on each one.
        cur.execute("DELETE FROM event WHERE source = %(src)s AND thread_key = ANY(%(tk)s)", p)
        events = cur.rowcount

        # 6. Audit. scope_ref records the RULE, not just the ids it resolved
        #    to today, so a later reader can tell a rule change from a data
        #    change.
        cur.execute("""
            INSERT INTO erasure_log (scope, scope_ref, completed_at, projections_pruned)
            VALUES (%s, %s, now(), %s) RETURNING erasure_id
        """, (f"{source}_excluded_threads",
              json.dumps({"source": source, "thread_keys": tk,
                          "rule": "adapter.excluded_thread_keys()",
                          "events_deleted": events,
                          "commitments_unresolved": unresolved,
                          "life_events_pruned": life_pruned,
                          "life_events_dropped": life_dropped,
                          "projection_dep_rows": deps}),
              segments))
        erasure_id = cur.fetchone()[0]

        # 7. Prove it, inside the same transaction. A purge that leaves rows
        #    behind is the "backflow" the dependency graph exists to prevent,
        #    and finding out later means finding out through /recall.
        cur.execute("""
            SELECT (SELECT count(*) FROM event WHERE source=%(src)s AND thread_key = ANY(%(tk)s)),
                   (SELECT count(*) FROM segment WHERE thread_key = ANY(%(tk)s)),
                   (SELECT count(*) FROM entity_mention m
                     LEFT JOIN segment s USING (segment_id) WHERE s.segment_id IS NULL)
        """, p)
        left_e, left_s, orphans = cur.fetchone()
        if left_e or left_s or orphans:
            raise RuntimeError(
                f"purge incomplete: {left_e} events, {left_s} segments, "
                f"{orphans} orphan mentions remain — rolling back")

    log.info("%s: erasure_id=%d  events=%d segments=%d deps=%d "
             "commitments_unresolved=%d life_pruned=%d life_dropped=%d",
             source, erasure_id, events, segments, deps,
             unresolved, life_pruned, life_dropped)
    return events


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chronicle.purge")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; without it this only counts")
    ap.add_argument("--source", action="append", dest="sources",
                    help="limit to one source (repeatable)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    excluded = collect(args.sources)
    if not excluded:
        # Distinguishable from "clean": collect() logs a line per adapter it
        # built, so an empty result with no such lines means nothing was
        # configured, not that nothing matched.
        log.warning("no adapter reported an excluded thread_key — nothing to do")
        return 0

    from .worker import connect
    conn = connect()
    try:
        total = 0
        for source, keys in sorted(excluded.items()):
            c = count(conn, source, keys)
            total += c["events"]
            print(f"{source}: {len(keys)} excluded thread_key(s)")
            for f in FIELDS:
                print(f"    {f:<24} {c[f]:>8,}")

        if not args.apply:
            print(f"\nDRY RUN — nothing deleted. {total:,} event(s) would go.")
            print("Re-run with --apply to commit.")
            return 0

        if total == 0:
            print("\nnothing to delete; already clean.")
            return 0

        for source, keys in sorted(excluded.items()):
            _purge(conn, source, keys)
        conn.commit()
        print(f"\npurged {total:,} event(s). erasure_log written.")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
