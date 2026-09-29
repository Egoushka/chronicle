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
    python -m chronicle.worker segment     # event -> segment
    python -m chronicle.worker resegment --thread K   # rebuild threads at a cap
    python -m chronicle.worker embed       # segment.embedding
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

#: Hard cap on events per narrative segment. A setting, not a constant, so it
#: can be SWEPT rather than argued about: on the first eval run every lookup
#: gold in a segment of <=12 events was found (mean rank 1.6, 0 misses), while
#: segments of >=16 events produced all 3 complete misses. One vector over 30
#: messages gives the clause that answers the question ~4% of the signal. 30
#: is not known to be wrong — it is unmeasured. `resegment` makes it runnable.
MAX_MESSAGES = int(os.environ.get("SEGMENT_MAX_MESSAGES", "30"))


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
    COMMITS. A crash mid-batch re-reads that batch, and the upsert in
    `_write_events` is a no-op on unchanged rows, so the replay is harmless.
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

        n = refreshed = 0
        try:
            for batch in _chunked(ad.fetch(since=since), BATCH_SIZE):
                changed = _write_events(conn, batch)
                # Same transaction as the event update: a crash between the
                # two would leave a segment citing text it no longer shows,
                # and nothing would ever revisit it.
                refreshed += _refresh_segments(conn, changed)
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
        log.info("%s: %d events, %d segments refreshed", policy.source, n, refreshed)
        total += n

    log.info("ingested %d events", total)
    return 0


def _write_events(conn, batch) -> list[str]:
    """Upsert a batch; return the keys of EXISTING events whose text changed.

    `DO NOTHING` was right while every source was append-only, and wrong the
    day a transcript landed on a voice note that had been ingested empty: the
    row existed, so the transcript was dropped. 4,637 voice/video notes sat
    empty in chronicle while telegram-sync held text for all but 93.

    Two guards on the update. Unchanged text is not an update, so re-reading
    the overlap window costs nothing. And an empty incoming text never
    overwrites a non-empty one: Telegram cannot edit a message to empty, so
    that only happens when a source loses data, and the index should not
    follow it down.

    Inserted vs updated is told apart by `ingested_at`: the column defaults to
    now(), which is fixed for the transaction, and an UPDATE leaves it alone.

    Secrets are redacted HERE, before the row exists — see `chronicle.redact`.
    A stored row that still holds one differs from its redacted re-read, so
    the overlap window rewrites it and its segment like any other edit.
    """
    import json

    from .redact import redact, redact_obj

    rows = [(e.source, e.source_id, e.ts, e.kind, e.actor, redact(e.text)[0],
             json.dumps(redact_obj(e.payload)[0], default=str),
             e.reply_to, e.thread_key) for e in batch]
    changed: list[str] = []
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO event (source, source_id, ts, kind, actor, text,
                                  payload, reply_to, thread_key)
               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
               ON CONFLICT (source, source_id, ts) DO UPDATE
                  SET text = EXCLUDED.text, payload = EXCLUDED.payload
                WHERE event.text IS DISTINCT FROM EXCLUDED.text
                  AND EXCLUDED.text <> ''
               RETURNING source || ':' || source_id, ingested_at < now()""",
            rows, returning=True)
        while True:
            changed += [key for key, updated in cur.fetchall() if updated]
            if not cur.nextset():
                break
    return changed


# Columns every segment builder reads, in this order. `chat_title` is in the
# payload for telegram and absent elsewhere, where the thread key stands in.
_EVENT_COLS = """e.source, e.source_id, e.ts, e.actor, e.text, e.transcript,
                 e.ocr_text, e.reply_to, e.person_id, e.payload->>'chat_title'"""


def _events_by_keys(cur, keys: list[str]) -> list[tuple]:
    """Event rows for `source:source_id` keys, oldest first.

    Split into (source, source_id) and joined against the primary key rather
    than compared as `source || ':' || source_id`, which no index covers.
    The prefix is the source (hard-won fact 32), and a source_id may itself
    contain ':' — telegram's is `chat:msg` — so split once, from the left.
    """
    if not keys:
        return []
    srcs, sids = zip(*(k.split(":", 1) for k in keys))
    cur.execute(
        f"""SELECT {_EVENT_COLS}
              FROM event e
              JOIN unnest(%s::text[], %s::text[]) AS k(src, sid)
                ON e.source = k.src AND e.source_id = k.sid
             ORDER BY e.ts, e.source_id""", (list(srcs), list(sids)))
    return cur.fetchall()


def _refresh_segments(conn, keys: list[str]) -> int:
    """Rebuild every segment that cites a changed event, in place.

    In place, not delete-and-reinsert: facts, commitments and entity mentions
    hang off `segment_id`, and one of those edges does not cascade (hard-won
    fact 31). Membership does not change — only the text — so the segment
    keeps its id and loses its embedding, which the next `embed` restores.
    """
    if not keys:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.segment_id, s.thread_key, s.source_event_ids, src.density
                 FROM segment s JOIN source src ON src.source = s.sources[1]
                WHERE s.source_event_ids && %s::text[]""", (keys,))
        hits = cur.fetchall()
        for segment_id, thread_key, ids, density in hits:
            rows = _events_by_keys(cur, ids)
            if rows:
                _update_segment(cur, segment_id, _segment_fields(rows, thread_key, density))
    return len(hits)


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
    """event -> segment, for events no segment cites yet. The highest-value
    stage in the system.

    Incremental. The first version re-read every thread and INSERTed with no
    conflict target, which is correct exactly once: a second run duplicates
    all 50,096 segments. So nothing re-ran it, and the 4,711 events ingested
    on 2026-09-14 sat unsegmented and unsearchable for twelve days.

    Only NARRATIVE sources are segmented. TELEMETRY and DISCRETE events arrive
    pre-aggregated from their adapters and map 1:1 to segments — running a
    time-gap segmenter over them produces meaningless thresholds.
    """
    cap = getattr(args, "max_messages", None) or MAX_MESSAGES
    conn = connect()
    with conn.cursor() as cur:
        # Materialised once per run: ~680k keys, a hash anti-join away from
        # "which events are unsegmented". Probing each segment's array per
        # event instead is thread-size x segment-count.
        cur.execute("""CREATE TEMP TABLE seg_key AS
                       SELECT DISTINCT unnest(source_event_ids) AS k FROM segment""")
        cur.execute("ANALYZE seg_key")
        cur.execute("""SELECT e.thread_key, s.density,
                              coalesce(tc.gap_seconds, 1800)
                       FROM event e
                       JOIN source s ON s.source = e.source
                       LEFT JOIN thread_config tc ON tc.thread_key = e.thread_key
                       WHERE NOT EXISTS (SELECT 1 FROM seg_key
                                         WHERE k = e.source || ':' || e.source_id)
                       GROUP BY e.thread_key, s.density, tc.gap_seconds""")
        threads = cur.fetchall()

    made = extended = 0
    for thread_key, density, gap in threads:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT {_EVENT_COLS} FROM event e
                     WHERE e.thread_key = %s
                       AND NOT EXISTS (SELECT 1 FROM seg_key
                                       WHERE k = e.source || ':' || e.source_id)
                     ORDER BY e.ts, e.source_id""", (thread_key,))
            new = cur.fetchall()
            if not new:
                continue

            if density != "narrative":
                # Pre-aggregated: one event, one segment. The adapter already
                # did the filtering, so these are substantive by construction.
                groups, last_id = [[r] for r in new], None
            else:
                groups, last_id = _segment_narrative(cur, thread_key, new, gap, cap)

            for i, g in enumerate(groups):
                fields = _segment_fields(g, thread_key, density, cap)
                if i == 0 and last_id is not None:
                    _update_segment(cur, last_id, fields)
                    extended += 1
                else:
                    _insert_segment(cur, thread_key, fields)
                    made += 1
        conn.commit()

    log.info("created %d segments, extended %d", made, extended)
    return 0


def cmd_resegment(args) -> int:
    """Throw away named threads' segments and rebuild them from their events.

    `segment` is incremental and never revisits a finished segment, so a
    changed cap (or segmenter) would otherwise apply only to new events. This
    is the experiment's other half: rebuild the threads the eval cites at a
    trial cap, embed, `make eval`, compare. Scoped by design — the whole
    archive is ~430 threads and ~10 h of re-embedding; the eval's threads are
    a couple of dozen and minutes.

    Each thread is one transaction: a crash mid-rebuild rolls back to the OLD
    segmentation rather than leaving the thread with neither. Rebuilt segments
    have no embedding and no enrichment; `embed` (and `enrich`, if on) redo
    them. Events are untouched, so nothing keyed on events changes.
    """
    threads = getattr(args, "thread", None)
    if not threads:
        raise SystemExit("resegment needs --thread (repeatable); "
                         "`make eval-threads` prints the eval's threads")
    cap = getattr(args, "max_messages", None) or MAX_MESSAGES

    conn = connect()
    made = 0
    for thread_key in threads:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.density, coalesce(tc.gap_seconds, 1800)
                     FROM event e
                     JOIN source s ON s.source = e.source
                     LEFT JOIN thread_config tc ON tc.thread_key = e.thread_key
                    WHERE e.thread_key = %s
                    LIMIT 1""", (thread_key,))
            row = cur.fetchone()
            if row is None:
                log.warning("no events for thread %s; skipped", thread_key)
                continue
            density, gap = row
            dropped = _clear_thread_segments(cur, thread_key)
            cur.execute(
                f"""SELECT {_EVENT_COLS} FROM event e
                     WHERE e.thread_key = %s
                     ORDER BY e.ts, e.source_id""", (thread_key,))
            rows = cur.fetchall()
            groups = (_cut(rows, gap, cap) if density == "narrative"
                      else [[r] for r in rows])
            for g in groups:
                _insert_segment(cur, thread_key,
                                _segment_fields(g, thread_key, density, cap))
        conn.commit()
        made += len(groups)
        log.info("%s: %d segments -> %d at cap %d",
                 thread_key, dropped, len(groups), cap)

    log.info("rebuilt %d thread(s), %d segments; run `embed` next",
             len(threads), made)
    return 0


def _clear_thread_segments(cur, thread_key: str) -> int:
    """Drop one thread's segments and everything keyed on them.

    Not `purge`: that deletes the EVENTS too. Here the events survive and only
    the aggregation layer is rebuilt, so the edges that hang off `event`
    rather than `segment` — `life_event.source_event_ids`, and
    `projection_dep` rows of projections that survive — stay strictly alone.

    Three of segment's four inbound edges cascade (`entity_mention`,
    `fact.source_segment_id`, `commitment.source_segment_id`). The fourth,
    `commitment.resolution_segment_id`, is NO ACTION (hard-won fact 31).
    """
    p = {"tk": thread_key}
    doomed = "(SELECT segment_id FROM segment WHERE thread_key = %(tk)s)"

    # A commitment RESOLVED BY a doomed segment would abort the DELETE on the
    # foreign key. Null it and reopen: the commitment's evidence is events
    # that still exist; only the link to its resolving segment goes, and
    # enrich re-derives it against the new segmentation.
    cur.execute(f"""UPDATE commitment SET resolution_segment_id = NULL,
                           status = 'open'
                     WHERE resolution_segment_id IN {doomed}""", p)

    # projection_dep has no foreign key, so the cascade into fact/commitment
    # would leave orphan rows. Keyed by PROJECTION here — purge keys it by
    # event because purge deletes the events.
    cur.execute(f"""
        DELETE FROM projection_dep pd
         WHERE (pd.projection_kind, pd.projection_id) IN (
                 SELECT 'fact', fact_id FROM fact
                  WHERE source_segment_id IN {doomed}
                 UNION ALL
                 SELECT 'commitment', commitment_id FROM commitment
                  WHERE source_segment_id IN {doomed})""", p)

    cur.execute("DELETE FROM segment WHERE thread_key = %(tk)s", p)
    return cur.rowcount


def _cut(rows: list[tuple], gap: int, max_messages: int) -> list[list[tuple]]:
    """Segment event rows (in `_EVENT_COLS` order) into groups of rows.

    Reply edges are real: `reply_to` is stored as `{chat}:{msg}`, the same
    shape as `source_id` (the source prefix exists only on
    segment.source_event_ids, hard-won fact 32), so the two join directly.
    This used to pass `reply_to_id=None`, which silently disabled
    segment_chat's reply-edge rule — 7.9% of messages carry one and none ever
    suppressed a split. A reply to a message outside `rows` has no position
    and is ignored, which is the rule's own bound anyway.
    """
    from .segment import Event, segment_chat

    pos = {r[1]: i for i, r in enumerate(rows)}
    events = [Event(message_id=i, chat_id=0, sender_id=r[8],
                    sender_name=r[3] or "?", ts=r[2], text=r[4] or "",
                    reply_to_id=pos.get(r[7]) if r[7] else None,
                    transcript=r[5], ocr_text=r[6])
              for i, r in enumerate(rows)]
    return [[rows[e.message_id] for e in seg.messages]
            for seg in segment_chat(events, gap_seconds=gap,
                                    max_messages=max_messages)]


def _segment_narrative(cur, thread_key: str, new: list[tuple], gap: int,
                       max_messages: int = MAX_MESSAGES):
    """Segment a thread's new events, continuing its last segment if they
    reach back into it.

    Returns (groups, last_id). When last_id is set, groups[0] REPLACES that
    segment: the new events are re-segmented together with its events, so a
    conversation that was still going when the previous run happened keeps
    growing instead of being cut at the run boundary. groups[0] always starts
    with the old segment's first event — the input is sorted and that event is
    its minimum — so updating in place is always a valid replacement.

    ponytail: events OLDER than the last segment (a late backfill of history)
    are segmented among themselves and never merged into the older segments
    they fall between. Rare — telegram-sync's history is synced — and fixing
    it means re-cutting arbitrary interior segments; revisit if backfills of
    old chats become routine.
    """
    cur.execute(
        """SELECT segment_id, started_at, source_event_ids FROM segment
            WHERE thread_key = %s ORDER BY started_at DESC, segment_id DESC
            LIMIT 1""", (thread_key,))
    last = cur.fetchone()

    def cut(rows):
        return _cut(rows, gap, max_messages)

    if last is None:
        return cut(new), None

    last_id, last_start, last_keys = last
    orphans = [r for r in new if r[2] < last_start]
    tail = [r for r in new if r[2] >= last_start]
    groups = cut(orphans) if orphans else []
    if not tail:
        return groups, None
    # Replacement first, so the caller's "groups[0] updates last_id" holds.
    return cut(_events_by_keys(cur, last_keys) + tail) + groups, last_id


def _segment_fields(g: list[tuple], thread_key: str, density: str,
                    max_messages: int = MAX_MESSAGES) -> tuple:
    """Everything a segment row derives from its events, in insert order."""
    from .segment import SEGMENTER_VERSION, build_embed_text

    raw = "\n".join(f"{r[3] or '?'}: {r[4] or r[5] or r[6] or ''}" for r in g)
    chat = next((r[9] for r in g if len(r) > 9 and r[9]), thread_key)
    people = list(dict.fromkeys(r[3] for r in g if r[3]))
    return (sorted({r[0] for r in g}), g[0][2], g[-1][2], len(g),
            [f"{r[0]}:{r[1]}" for r in g], raw,
            build_embed_text(raw, g[0][2], chat, people),
            # Filler-burst detection is a NARRATIVE concern. A wakapi coding
            # session or a dawarich stay is substantive by construction;
            # applying the text heuristic marked all of them False and hid
            # them from every query.
            _substantive(g) if density == "narrative" else True,
            # The cap is part of what produced this row; a sweep leaves
            # threads cut at different caps, and this is how to tell them apart.
            f"{SEGMENTER_VERSION}/m{max_messages}")


def _insert_segment(cur, thread_key: str, fields: tuple) -> None:
    cur.execute(
        """INSERT INTO segment
             (thread_key, sources, started_at, ended_at, event_count,
              source_event_ids, raw_text, embed_text, is_substantive,
              segmenter_version)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (thread_key, *fields))


def _update_segment(cur, segment_id: int, fields: tuple) -> None:
    """Replace a segment's content, keep its id, and invalidate everything
    derived from the old content: the embedding (re-encoded by `embed`) and
    the enrichment (redone by `enrich`, which replaces its facts)."""
    cur.execute(
        """UPDATE segment
              SET sources = %s, started_at = %s, ended_at = %s, event_count = %s,
                  source_event_ids = %s, raw_text = %s, embed_text = %s,
                  is_substantive = %s, segmenter_version = %s,
                  embedding = NULL, lemmatized_text = NULL,
                  embedder_version = NULL, enriched_at = NULL
            WHERE segment_id = %s""", (*fields, segment_id))


def _substantive(group) -> bool:
    """A burst of 'ок' is not a memory — but two long messages are.

    Below three messages the only test used to be `len == 1 and > 80 chars`,
    so EVERY two-message segment was hidden from /recall however long: 2,812
    telegram segments, 525 of them 200+ chars, and 5 of the 57 gold messages
    in the first eval (2026-09-26) — "got an offer", "we're being evicted"
    are exactly the short exchanges that matter. Short segments now pass on
    content alone; the distinct-text test still guards 3+ message bursts.
    """
    content = sum(len(r[4] or "") for r in group)
    if len(group) < 3:
        return content > 80
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
                """SELECT segment_id, embed_text FROM segment
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
                """UPDATE segment
                      SET embedding = %s, lemmatized_text = %s,
                          embedder_version = %s
                    WHERE segment_id = %s""",
                [(v.tolist(), lem(r[1]), EMBEDDER_VERSION, r[0])
                 for v, r in zip(vecs, rows)])
        conn.commit()
        done += len(rows)
        log.info("embedded %d", done)

    # The lexical branch weights terms by IDF over lemmatized_text, which this
    # stage writes, so the statistics follow it. ~7 s over 39k segments, run
    # even when nothing was embedded: segment/ingest can change is_substantive.
    with conn.cursor() as cur:
        cur.execute("SELECT refresh_lexeme_df()")
        log.info("lexeme_df refreshed over %d segments", cur.fetchone()[0])
    conn.commit()
    return 0


# ---------------------------------------------------------------------------
#  5. ENRICH  (slow, additive, safe to interrupt)
# ---------------------------------------------------------------------------

def cmd_enrich(args) -> int:
    """Cloud LLM pass: summary, topics, facts, commitments. See enrich.py.

    Additive and bounded: at most ENRICH_LIMIT segments per run (default
    2,000), newest first, so the nightly run finishes and the recent past is
    useful while the backfill grinds. Every enriched segment loses its
    embedding — embed_text now carries its topics and facts — so `all` runs
    this BEFORE `embed`, and the re-encode happens in the same run.

    A failed call leaves that segment unenriched and it is retried next run.
    ponytail: a segment the model can never answer is retried every night;
    add a failure counter if the log shows the same ids recurring.
    """
    from concurrent.futures import ThreadPoolExecutor

    from .enrich import Client

    client = Client.from_env()
    if client is None:
        log.warning("enrich skipped: set ENRICH_MODEL and LITELLM_API_KEY")
        return 0
    limit = int(os.environ.get("ENRICH_LIMIT", "2000"))
    workers = int(os.environ.get("ENRICH_CONCURRENCY", "8"))

    conn = connect()
    with conn.cursor() as cur:
        cur.execute("SELECT predicate FROM fact_predicate ORDER BY predicate")
        predicates = [r[0] for r in cur]
    if not predicates:
        raise SystemExit("fact_predicate is empty — apply migrations/005_incremental.sql")

    done = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while done + failed < limit:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT s.segment_id, s.thread_key, s.started_at, s.ended_at,
                              s.raw_text, s.source_event_ids
                         FROM segment s JOIN source src ON src.source = s.sources[1]
                        WHERE s.enriched_at IS NULL AND s.is_substantive
                          AND src.density = 'narrative'
                        ORDER BY s.started_at DESC
                        LIMIT %s OFFSET %s""",
                    (min(workers * 8, limit - done - failed), failed))
                rows = cur.fetchall()
                # (OFFSET skips this run's failures, which stay unenriched.)
                chats = {r[0]: _chat_and_people(cur, r[5], r[1]) for r in rows}
            if not rows:
                break

            def call(r):
                try:
                    return r, client.extract(chats[r[0]][0], r[2], r[4], predicates)
                except Exception as exc:                    # noqa: BLE001
                    log.warning("enrich segment %s failed: %s", r[0], exc)
                    return r, None

            batch_ok = 0
            for r, reply in pool.map(call, rows):
                if reply is None:
                    failed += 1
                    continue
                _write_enrichment(conn, r, reply, set(predicates), *chats[r[0]])
                done += 1
                batch_ok += 1
            if not batch_ok:
                # A whole batch failing is the endpoint, not the segments: a
                # rotated key (chronicle's died with the 2026-09-23 LiteLLM
                # rotation), a renamed model, an outage. Carrying on would
                # spend ENRICH_LIMIT calls learning the same thing.
                log.error("enrich: all %d calls in a batch failed — stopping; "
                          "check ENRICH_MODEL and the LiteLLM key", len(rows))
                return 1
            with conn.cursor() as cur:
                cur.execute("SELECT resolve_fact_conflicts()")
            conn.commit()
            log.info("enriched %d (%d failed)", done, failed)

    return 0


def _chat_and_people(cur, keys: list[str], thread_key: str) -> tuple[str, list[str]]:
    rows = _events_by_keys(cur, keys)
    chat = next((r[9] for r in rows if r[9]), thread_key)
    return chat, list(dict.fromkeys(r[3] for r in rows if r[3]))


def _write_enrichment(conn, seg: tuple, reply: dict, predicates: set[str],
                      chat: str, people: list[str]) -> None:
    """Replace whatever a previous enrichment of this segment produced.

    Replace, not add: `_update_segment` clears `enriched_at` when a segment's
    text changes, so a second pass is the normal case, and its facts must
    supersede the first pass's rather than sit beside them as corroboration.
    """
    from .enrich import EXTRACTOR_VERSION, clean, fact_line
    from .resolve import skeleton_key, translit_key
    from .segment import build_embed_text

    segment_id, _thread, started, ended, raw, ids = seg
    out = clean(reply, predicates)
    # Epoch millis of the segment START — the same instant as t_valid.
    # Versioning by the END made version order and time order disagree for
    # overlapping segments, and resolve_fact_conflicts() then closed a fact
    # before it began (fact_valid_order violation; first live run 2026-09-26).
    version = int(started.timestamp() * 1000)
    refs = [k.split(":", 1) for k in ids]

    with conn.cursor() as cur:
        for kind, table in (("fact", "fact"), ("commitment", "commitment")):
            cur.execute(
                f"""DELETE FROM projection_dep WHERE projection_kind = %s
                      AND projection_id IN (SELECT {kind}_id FROM {table}
                                            WHERE source_segment_id = %s)""",
                (kind, segment_id))
            cur.execute(f"DELETE FROM {table} WHERE source_segment_id = %s",
                        (segment_id,))
        cur.execute("DELETE FROM entity_mention WHERE segment_id = %s", (segment_id,))

        for f in out["facts"]:
            entity_id = _entity(cur, f["subject"], started, translit_key, skeleton_key)
            cur.execute("""INSERT INTO entity_mention (entity_id, segment_id, ts)
                           VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                        (entity_id, segment_id, started))
            cur.execute(
                """INSERT INTO fact (subject_id, predicate, object_text, t_valid,
                                     version, confidence, source_segment_id,
                                     source_event_ids, extractor_version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING fact_id""",
                (entity_id, f["predicate"], f["object"], started, version,
                 f["confidence"], segment_id, ids, EXTRACTOR_VERSION))
            _cite(cur, "fact", cur.fetchone()[0], refs)

        for c in out["commitments"]:
            cur.execute(
                """INSERT INTO commitment (text, direction, stated_at, due_at,
                                          confidence, source_segment_id,
                                          source_event_ids, extractor_version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING commitment_id""",
                (c["text"], c["direction"], started, c["due"], c["confidence"],
                 segment_id, ids, EXTRACTOR_VERSION))
            _cite(cur, "commitment", cur.fetchone()[0], refs)

        cur.execute(
            """UPDATE segment
                  SET summary = %s, topics = %s, importance = %s, sentiment = %s,
                      embed_text = %s, extractor_version = %s, enriched_at = now(),
                      embedding = NULL, lemmatized_text = NULL, embedder_version = NULL
                WHERE segment_id = %s""",
            (out["summary"], out["topics"], out["importance"], out["sentiment"],
             build_embed_text(raw, started, chat, people,
                              facts=[fact_line(f) for f in out["facts"]],
                              topics=out["topics"]),
             EXTRACTOR_VERSION, segment_id))


def _entity(cur, name: str, seen: datetime, translit_key, skeleton_key) -> int:
    """Find-or-create a person by coarse phonetic key.

    Tier 1 only (docs/ADR-002): the phonetic key is "usually correct" and
    merges Егор/Єгор/Yehor. The skeleton tier deliberately over-merges
    (Дина/Дон -> dn) and is a candidate generator, never a decision, so it is
    stored for later resolution and not acted on here.
    """
    key = translit_key(name)
    cur.execute("""SELECT entity_id FROM entity
                    WHERE entity_type = 'person' AND %s = ANY(phonetic_keys)
                    ORDER BY mention_count DESC LIMIT 1""", (key,))
    row = cur.fetchone()
    if row is None:
        skel = skeleton_key(name)
        cur.execute(
            """INSERT INTO entity (entity_type, canonical_name, aliases,
                                   phonetic_keys, skeleton_keys, extractor_version)
               VALUES ('person', %s, %s, %s, %s, 'enrich-2026.09-v1')
               ON CONFLICT (entity_type, canonical_name)
                 DO UPDATE SET phonetic_keys = entity.phonetic_keys
               RETURNING entity_id""",
            (name, [name], [key], [skel] if len(skel) >= 2 else []))
        row = cur.fetchone()
    cur.execute(
        """UPDATE entity SET mention_count = mention_count + 1,
                  first_seen_at = least(first_seen_at, %s),
                  last_seen_at = greatest(last_seen_at, %s),
                  aliases = CASE WHEN %s = ANY(aliases) THEN aliases
                                 ELSE aliases || %s::text END
            WHERE entity_id = %s""", (seen, seen, name, name, row[0]))
    return row[0]


def _cite(cur, kind: str, projection_id: int, refs: list[list[str]]) -> None:
    """projection_dep rows: what `chronicle.purge` walks to delete a projection
    when the events it came from are erased. Refcounted from day one, because
    retrofitting it is the expensive part (backflow)."""
    cur.executemany(
        """INSERT INTO projection_dep (projection_kind, projection_id, source, source_id)
           VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        [(kind, projection_id, src, sid) for src, sid in refs])


# ---------------------------------------------------------------------------

COMMANDS = {
    "ingest": cmd_ingest,
    "fit-gaps": cmd_fit_gaps,
    "segment": cmd_segment,
    "resegment": cmd_resegment,
    "embed": cmd_embed,
    "enrich": cmd_enrich,
}


def cmd_all(args) -> int:
    # enrich BEFORE embed: enriching rewrites embed_text and clears the
    # embedding, so this order re-encodes it in the same run.
    #
    # enrich is the one stage whose failure must not stop the run: it depends
    # on a cloud endpoint, and new segments are searchable without it but not
    # without `embed`. So embed still runs, and the failure is the exit code,
    # which is what makes nightly.sh send its ntfy.
    deferred = 0
    for name in ("ingest", "fit-gaps", "segment", "enrich", "embed"):
        log.info("=== %s ===", name)
        if name == "enrich":
            # An exception here must not skip embed either: the first live run
            # died on a constraint violation and left new segments unembedded.
            try:
                rc = COMMANDS[name](args)
            except Exception:                              # noqa: BLE001
                log.exception("enrich crashed; continuing to embed")
                rc = 1
            deferred = deferred or rc
            continue
        rc = COMMANDS[name](args)
        if rc:
            return rc
    return deferred


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chronicle.worker")
    ap.add_argument("command", choices=[*COMMANDS, "all", "doctor"])
    ap.add_argument("--tier", type=int, default=1, choices=[1, 2, 3, 4])
    ap.add_argument("--max-messages", type=int, default=MAX_MESSAGES,
                    help="events per segment cap (default $SEGMENT_MAX_MESSAGES or 30)")
    ap.add_argument("--thread", action="append", metavar="THREAD_KEY",
                    help="resegment: a thread to rebuild (repeatable)")
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
