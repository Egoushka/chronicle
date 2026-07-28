"""Batch worker — ingest, segment, embed, enrich. Resumable by construction.

Runs as `restart: "no"` and exits when its batch is done. That is not laziness:
the box is 32 GB with 61.4 GB of `mem_limit` committed across 107 services and
has already hit 96% swap, and `memswap_limit == mem_limit` everywhere, so a
cgroup at its cap is OOM-killed rather than swapped. A resident 7B model has
nowhere to live.

Every stage therefore checkpoints. An OOM kill costs one batch, not the run.

    python -m chronicle.worker doctor      # ALWAYS run this first
    python -m chronicle.worker ingest      # sources -> event
    python -m chronicle.worker fit-gaps    # measure per-thread session gaps
    python -m chronicle.worker segment     # event -> episode
    python -m chronicle.worker embed       # episode.embedding
    python -m chronicle.worker enrich      # summary/topics/facts (slow, optional)
    python -m chronicle.worker all         # the above, in order

Ordering matters. Retrieval works after `embed`; `enrich` is additive and can
grind for weeks in the background while the system is already useful. Ship
before enrichment finishes.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from typing import Iterable, Iterator

log = logging.getLogger("chronicle.worker")

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "500"))
DB_URL = os.environ.get("CHRONICLE_DB_URL", "")


# ---------------------------------------------------------------------------
#  db helpers
# ---------------------------------------------------------------------------

def connect():
    import psycopg
    if not DB_URL:
        raise SystemExit("CHRONICLE_DB_URL is not set")
    return psycopg.connect(DB_URL, autocommit=False)


def _chunked(it: Iterable, n: int) -> Iterator[list]:
    buf = []
    for x in it:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


# ---------------------------------------------------------------------------
#  1. INGEST
# ---------------------------------------------------------------------------

def cmd_ingest(args) -> int:
    """Pull every configured source into `event`.

    Resume point is `source.last_ingested_at`, advanced only after a batch
    COMMITS. A crash mid-batch re-reads that batch, and `ON CONFLICT DO
    NOTHING` on the primary key makes the replay harmless.
    """
    from .doctor import build
    from .sources import Tier, enabled

    conn = connect()
    total = 0

    for policy in enabled(Tier(args.tier)):
        ad = build(policy.source)
        if ad is None:
            log.info("skip %s (not configured)", policy.source)
            continue

        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO source (source, density, tier, hindsight_bank)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (source) DO UPDATE
                     SET density = EXCLUDED.density, tier = EXCLUDED.tier
                   RETURNING last_ingested_at""",
                (policy.source, policy.density.value, int(policy.tier), policy.bank))
            since = cur.fetchone()[0]
        conn.commit()

        n = 0
        try:
            for batch in _chunked(ad.fetch(since=since), BATCH_SIZE):
                _write_events(conn, batch)
                # Advance to the max WATERMARK in the batch, not the last ts.
                # For rollup adapters the two differ: ts is the span start and
                # resuming from it re-reads the span, producing a duplicate
                # partial event on every run.
                _set_watermark(conn, policy.source,
                               max(e.watermark_ts or e.ts for e in batch))
                conn.commit()
                n += len(batch)
                if n % 10_000 == 0:
                    log.info("  %s: %d events", policy.source, n)
        except Exception as exc:                            # noqa: BLE001
            # One broken source must never fail the run.
            conn.rollback()
            _set_error(conn, policy.source, f"{type(exc).__name__}: {exc}")
            conn.commit()
            log.warning("%s failed after %d events (continuing): %s",
                        policy.source, n, exc)
            continue

        _set_error(conn, policy.source, None)
        conn.commit()
        log.info("%s: %d events", policy.source, n)
        total += n

    log.info("ingested %d events", total)
    return 0


def _write_events(conn, batch) -> None:
    rows = [(e.source, e.source_id, e.ts, e.kind, e.actor, e.text,
             __import__("json").dumps(e.payload, default=str),
             e.reply_to, e.thread_key) for e in batch]
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO event (source, source_id, ts, kind, actor, text,
                                  payload, reply_to, thread_key)
               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
               ON CONFLICT (source, source_id, ts) DO NOTHING""", rows)


def _set_watermark(conn, source: str, ts: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE source SET last_ingested_at = %s WHERE source = %s",
                    (ts, source))


def _set_error(conn, source: str, err: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE source SET last_error = %s WHERE source = %s", (err, source))


# ---------------------------------------------------------------------------
#  2. FIT GAPS
# ---------------------------------------------------------------------------

def cmd_fit_gaps(args) -> int:
    """Measure each conversational thread's bimodal inter-event distribution.

    Prefers the Python fitter (finds the actual valley) over the SQL p75
    fallback. Nobody has published a principled inactivity threshold for
    personal IM — the 30-min web convention and MSC's 1-7h bracket the range —
    so measuring your own data beats every paper on the subject.
    """
    from .segment import fit_gap_threshold

    conn = connect()
    with conn.cursor() as cur:
        cur.execute("""SELECT e.thread_key, min(e.source), count(*)
                       FROM event e JOIN source s ON s.source = e.source
                       WHERE s.conversational
                       GROUP BY e.thread_key HAVING count(*) >= 100""")
        threads = cur.fetchall()

    fitted = 0
    for thread_key, source, _n in threads:
        with conn.cursor() as cur:
            cur.execute("SELECT ts FROM event WHERE thread_key = %s ORDER BY ts",
                        (thread_key,))
            stamps = [r[0] for r in cur]
        fit = fit_gap_threshold(stamps, chat_id=0)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO thread_config
                       (thread_key, source, gap_seconds, gap_fit_stats, fitted_at)
                   VALUES (%s,%s,%s,%s::jsonb, now())
                   ON CONFLICT (thread_key) DO UPDATE
                     SET gap_seconds = EXCLUDED.gap_seconds,
                         gap_fit_stats = EXCLUDED.gap_fit_stats,
                         fitted_at = now()""",
                (thread_key, source, fit.threshold_seconds,
                 __import__("json").dumps({
                     "n": fit.n_samples, "p50": fit.p50, "p90": fit.p90,
                     "valley": fit.valley_seconds, "method": "python-valley"})))
        fitted += 1
    conn.commit()
    log.info("fitted %d threads", fitted)
    return 0


# ---------------------------------------------------------------------------
#  3. SEGMENT
# ---------------------------------------------------------------------------

def cmd_segment(args) -> int:
    """event -> episode. The highest-value stage in the system.

    Only NARRATIVE sources are segmented. TELEMETRY and DISCRETE events arrive
    pre-aggregated from their adapters and map 1:1 to episodes — running a
    time-gap segmenter over them produces meaningless thresholds.
    """
    import json

    from .segment import SEGMENTER_VERSION, Event, segment_chat

    conn = connect()
    with conn.cursor() as cur:
        cur.execute("""SELECT e.thread_key, s.density,
                              coalesce(tc.gap_seconds, 1800)
                       FROM event e
                       JOIN source s ON s.source = e.source
                       LEFT JOIN thread_config tc ON tc.thread_key = e.thread_key
                       GROUP BY e.thread_key, s.density, tc.gap_seconds""")
        threads = cur.fetchall()

    made = 0
    for thread_key, density, gap in threads:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT source, source_id, ts, actor, text, transcript,
                          ocr_text, reply_to, person_id
                   FROM event WHERE thread_key = %s ORDER BY ts""", (thread_key,))
            rows = cur.fetchall()
        if not rows:
            continue

        if density == "narrative":
            events = [Event(message_id=i, chat_id=0, sender_id=r[8],
                            sender_name=r[3] or "?", ts=r[2],
                            text=r[4] or "", reply_to_id=None)
                      for i, r in enumerate(rows)]
            groups = [[rows[e.message_id] for e in ep.messages]
                      for ep in segment_chat(events, gap_seconds=gap)]
        else:
            # pre-aggregated: one event, one episode
            # Pre-aggregated: one event, one episode. The adapter already did
            # the filtering, so these are substantive by construction.
            groups = [[r] for r in rows]

        for g in groups:
            raw = "\n".join(f"{r[3] or '?'}: {r[4] or r[5] or r[6] or ''}" for r in g)
            header = (f"[thread: {thread_key}] [source: {g[0][0]}] "
                      f"[date: {g[0][2]:%Y-%m}]")
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO episode
                         (thread_key, sources, started_at, ended_at, event_count,
                          source_event_ids, raw_text, embed_text, is_substantive,
                          segmenter_version)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (thread_key, list({r[0] for r in g}), g[0][2], g[-1][2], len(g),
                     [f"{r[0]}:{r[1]}" for r in g], raw,
                     f"{header}\n{raw}",
                     # Filler-burst detection is a NARRATIVE concern. A wakapi
                     # coding session or a dawarich stay is substantive by
                     # construction; applying the text heuristic marked all of
                     # them False and hid them from every query.
                     _substantive(g) if density == "narrative" else True,
                     SEGMENTER_VERSION))
            made += 1
        conn.commit()

    log.info("created %d episodes", made)
    return 0


def _substantive(group) -> bool:
    """A burst of 'ок' is not a memory."""
    if len(group) < 3:
        return len(group) == 1 and len((group[0][4] or "")) > 80
    content = sum(len(r[4] or "") for r in group)
    distinct = {(r[4] or "").strip().lower() for r in group}
    return content >= 80 and len(distinct) >= 3


# ---------------------------------------------------------------------------
#  4. EMBED
# ---------------------------------------------------------------------------

def cmd_embed(args) -> int:
    from .embed import EMBEDDER_VERSION, Embedder, Lemmatizer

    conn = connect()
    emb = Embedder()
    lem = Lemmatizer()
    done = 0

    while True:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT episode_id, embed_text FROM episode
                   WHERE embedding IS NULL
                   -- newest first, so recent data becomes useful while the
                   -- backfill is still grinding through 2019
                   ORDER BY started_at DESC
                   LIMIT %s""", (BATCH_SIZE,))
            rows = cur.fetchall()
        if not rows:
            break

        vecs = emb.encode([r[1] for r in rows])["dense"]
        with conn.cursor() as cur:
            cur.executemany(
                """UPDATE episode
                      SET embedding = %s, lemmatized_text = %s,
                          embedder_version = %s
                    WHERE episode_id = %s""",
                [(v.tolist(), lem(r[1]), EMBEDDER_VERSION, r[0])
                 for v, r in zip(vecs, rows)])
        conn.commit()
        done += len(rows)
        log.info("embedded %d", done)

    return 0


# ---------------------------------------------------------------------------
#  5. ENRICH  (slow, additive, safe to interrupt)
# ---------------------------------------------------------------------------

def cmd_enrich(args) -> int:
    """Local LLM pass: summary, topics, facts, commitments.

    Deliberately last and deliberately optional. Retrieval works without it,
    so ship first and let this grind. Newest-first for the same reason.
    """
    log.warning("enrich is not wired to a model yet — see docs/DEPLOY.md step 5. "
                "Run ingest/segment/embed first and measure against grep before "
                "spending weeks of CPU here.")
    return 0


# ---------------------------------------------------------------------------

COMMANDS = {
    "ingest": cmd_ingest,
    "fit-gaps": cmd_fit_gaps,
    "segment": cmd_segment,
    "embed": cmd_embed,
    "enrich": cmd_enrich,
}


def cmd_all(args) -> int:
    for name in ("ingest", "fit-gaps", "segment", "embed"):
        log.info("=== %s ===", name)
        rc = COMMANDS[name](args)
        if rc:
            return rc
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chronicle.worker")
    ap.add_argument("command", choices=[*COMMANDS, "all", "doctor"])
    ap.add_argument("--tier", type=int, default=1, choices=[1, 2, 3, 4])
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "doctor":
        from .doctor import main as doctor_main
        return doctor_main([f"--tier={args.tier}"])
    if args.command == "all":
        return cmd_all(args)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
