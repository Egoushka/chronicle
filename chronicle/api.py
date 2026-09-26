"""FastAPI handlers backing the MCP tool surface.

One endpoint per query class, because "when did I first mention X" and "how
did my view evolve" are different systems. Collapsing them into one
`search_memory` endpoint is how you get 15-35% accuracy on temporal questions
(four independent benchmarks agree on that cliff).

Every response carries `source_event_ids`. An unattributed answer about your
own life is worse than no answer, because you have no way to check it.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .embed import Embedder, Lemmatizer
from .rank import reciprocal_rank_fusion  # noqa: F401  (used by SQL-side RRF parity tests)
from .route import route, window

log = logging.getLogger(__name__)
DB_URL = os.environ.get("CHRONICLE_DB_URL", "")

_state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    import psycopg_pool
    _state["pool"] = psycopg_pool.ConnectionPool(DB_URL, min_size=1, max_size=4,
                                                 open=True)
    _state["tally_dsn"] = _rotate_tally_password()
    # Models load lazily: the healthcheck has a 180s start_period but must
    # answer before BGE-M3 is warm, or autoheal restarts us in a loop.
    _state["embedder"] = Embedder()
    _state["lemmatizer"] = Lemmatizer()
    yield
    _state["pool"].close()


app = FastAPI(title="chronicle", lifespan=lifespan)


def q(sql: str, params: tuple = ()) -> list[tuple]:
    with _state["pool"].connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


TALLY_ROLE = "chronicle_tally"


def _rotate_tally_password() -> str | None:
    """Give chronicle_tally a fresh random password; return its DSN.

    Set by the superuser pool on every start, so the credential lives only in
    this process and there is nothing to add to .env. None when 004 has not
    been applied: /tally then refuses rather than falling back to superuser.
    """
    import secrets

    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    password = secrets.token_urlsafe(32)
    try:
        with _state["pool"].connection() as conn:
            conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(TALLY_ROLE), sql.Literal(password)))
    except Exception as exc:                                # noqa: BLE001
        log.warning("/tally disabled: cannot set %s password: %s", TALLY_ROLE, exc)
        return None
    return make_conninfo(DB_URL, user=TALLY_ROLE, password=password)


def q_tally(query: str) -> list[tuple]:
    """Run caller-supplied SQL as chronicle_tally, on a connection of its own.

    Not the pool: statement_timeout and default_transaction_read_only are
    user-settable, and a `SELECT set_config(..., false); COMMIT; ...` would
    leave a pooled connection loosened for the next caller. A fresh connection
    that is closed without committing takes any such change with it. /tally
    served 0 calls in the 72 h before 2026-09-26, so a connect per call is free.
    """
    import psycopg
    if not _state.get("tally_dsn"):
        raise HTTPException(503, "tally role unavailable; apply migrations/004_tally_role.sql")
    conn = psycopg.connect(_state["tally_dsn"], connect_timeout=5)
    try:
        return conn.execute(query).fetchall()
    except psycopg.Error as exc:
        # The agent wrote this SQL; the error is what lets it fix the query.
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()   # never commit: closing discards the transaction


# The browser surface. Served from this app rather than a static host so it
# shares the API's origin: no CORS middleware, no preflight, and nothing to
# widen on an API that holds the entire archive. 8030 binds 127.0.0.1 on the
# box, so reaching this at all means an SSH tunnel.
_UI = (Path(__file__).parent / "ui.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def ui():
    return _UI


@app.get("/health")
def health():
    try:
        q("SELECT 1")
    except Exception as exc:                                # noqa: BLE001
        raise HTTPException(503, f"db unreachable: {exc}") from exc
    return {"ok": True}


@app.get("/stats")
def stats():
    rows = q("""SELECT s.source, s.density, s.enabled, s.last_ingested_at,
                       s.last_error, count(e.*) AS events
                FROM source s LEFT JOIN event e ON e.source = s.source
                GROUP BY s.source, s.density, s.enabled, s.last_ingested_at, s.last_error""")
    segs = q("""SELECT count(*), count(*) FILTER (WHERE embedding IS NOT NULL),
                      count(*) FILTER (WHERE enriched_at IS NOT NULL) FROM segment""")[0]
    return {
        "sources": [dict(zip(
            ("source", "density", "enabled", "last_ingested_at", "last_error", "events"), r))
            for r in rows],
        "segments": {"total": segs[0], "embedded": segs[1], "enriched": segs[2]},
    }


class RecallReq(BaseModel):
    query: str
    date_from: datetime | None = None
    date_to: datetime | None = None
    source: str | None = None
    limit: int = 20          # Anthropic measured top-20 > top-10 > top-5


@app.post("/recall")
def recall(req: RecallReq):
    intent = route(req.query)
    # A date the question names confines the search, unless the caller set
    # one. Dense retrieval is one query against 39k segments; the year it asks
    # about is the cheapest discriminator there is.
    named = window(req.query) if req.date_from is None and req.date_to is None else None
    if named:
        req.date_from, req.date_to = named
    vec = _state["embedder"].encode_one(req.query).tolist()
    rows = q("""SELECT h.segment_id, h.rrf_score, e.started_at, e.thread_key,
                       e.raw_text, e.source_event_ids, e.summary
                FROM hybrid_search(%s::halfvec, %s, %s, %s, NULL, %s, 100, %s) h
                JOIN segment e USING (segment_id)
                ORDER BY h.rrf_score DESC""",
             (vec, req.query, req.date_from, req.date_to,
              [req.source] if req.source else None, req.limit))
    return {
        "intent": intent.kind,
        "routed_because": intent.matched_pattern,
        "window_from_query": [d.isoformat() for d in named] if named else None,
        "results": [{"segment_id": r[0], "score": float(r[1]),
                     "date": r[2].isoformat(), "thread": r[3],
                     "text": r[4][:2000], "evidence": r[5], "summary": r[6]}
                    for r in rows],
    }


class FirstMentionReq(BaseModel):
    term: str


@app.post("/first-mention")
def first_mention(req: FirstMentionReq):
    """Argmin over timestamp, not top-k similarity.

    Top-k returns the k most SIMILAR events, which are almost never the
    EARLIEST — similarity and recency are uncorrelated, so no value of k makes
    'earliest' reachable. Runs against `event`, not `segment`: a first mention
    is typically a bare token in a 15-character message.
    """
    lemma = _state["lemmatizer"](req.term)
    # The term and its lemma, whole. Splitting a multi-word lemma into words
    # made "game of thrones" also search for "of", which every other message
    # contains (and each pattern is an ILIKE too). One word splits to itself.
    patterns = sorted({req.term.lower(), lemma})
    rows = q("SELECT * FROM first_mention(%s, 20)", (patterns,))
    return {
        "term": req.term,
        "patterns_tried": patterns,
        "candidates": [{"source": r[0], "source_id": r[1], "date": r[2].isoformat(),
                        "text": r[3], "thread": r[4]} for r in rows],
        "note": "candidates are in ascending time order; verify the earliest "
                "before reporting it as the first mention",
    }


class EvolutionReq(BaseModel):
    topic: str
    bin_width: str = "3 months"


@app.post("/evolution")
def evolution(req: EvolutionReq):
    """Stratified retrieval — coverage, not similarity density.

    Top-k would spend all 50 slots on a three-week burst in 2022 and return
    nothing from 2019. Retrieving independently within each bin guarantees
    coverage at the same total cost. Rows come back CHRONOLOGICAL: Test of
    Time measured sorted presentation at 71.95% vs shuffled 58.82%.
    """
    vec = _state["embedder"].encode_one(req.topic).tolist()
    rows = q("""SELECT s.bin_start, e.segment_id, e.started_at, e.raw_text,
                       e.source_event_ids
                FROM stratified_search(%s::halfvec, %s::interval, 10) s
                JOIN segment e USING (segment_id)
                ORDER BY s.bin_start, s.rank_in_bin""",
             (vec, req.bin_width))
    bins: dict[str, list] = {}
    for bin_start, eid, started, text, ev in rows:
        bins.setdefault(bin_start.isoformat()[:10], []).append(
            {"segment_id": eid, "date": started.isoformat(),
             "text": text[:800], "evidence": ev})
    return {"topic": req.topic, "bins": bins, "bin_count": len(bins)}


class TallyReq(BaseModel):
    question: str
    sql: str | None = None


@app.post("/tally")
def tally(req: TallyReq):
    """Counting and ranking are SQL, not retrieval.

    The caller supplies the SQL (the agent writes it). We execute it as the
    SELECT-only chronicle_tally role and return it alongside the result —
    text-to-SQL is ~80% accurate even on simple schemas, so a query the user
    cannot see is a number they cannot trust.

    No keyword filtering: it was never a boundary (`WITH x AS (DELETE ...)
    SELECT` passed it). The role is — see migrations/004_tally_role.sql.
    """
    if not req.sql:
        raise HTTPException(400, "supply `sql`; the agent writes it, chronicle runs it")
    rows = q_tally(req.sql)
    return {"question": req.question, "sql": req.sql,
            "row_count": len(rows), "rows": [list(map(str, r)) for r in rows[:200]]}


class TimelineReq(BaseModel):
    date_from: datetime
    date_to: datetime
    sources: list[str] | None = None


@app.post("/timeline")
def timeline(req: TimelineReq):
    """Everything that happened in a window, across ALL sources.

    This is the endpoint that uses more than Telegram — conversations plus
    where you were, what you coded, what you spent, what you photographed.
    The behavioural signals are more honest than the conversational ones,
    because you do not curate them.
    """
    rows = q("""SELECT e.started_at, e.sources, e.thread_key,
                       coalesce(e.summary, left(e.raw_text, 300)),
                       e.source_event_ids
                FROM segment e
                WHERE e.started_at BETWEEN %s AND %s
                  AND (%s::text[] IS NULL OR e.sources && %s::text[])
                  AND e.is_substantive
                ORDER BY e.started_at""",
             (req.date_from, req.date_to, req.sources, req.sources))
    return {"from": req.date_from.isoformat(), "to": req.date_to.isoformat(),
            "events": [{"date": r[0].isoformat(), "sources": r[1],
                        "thread": r[2], "text": r[3], "evidence": r[4]}
                       for r in rows]}


class CommitmentReq(BaseModel):
    older_than_days: int = 90


@app.post("/commitments")
def commitments(req: CommitmentReq):
    rows = q("""SELECT commitment_id, text, counterparty, stated_at, due_at, age
                FROM v_forgotten_commitments
                WHERE stated_at < now() - make_interval(days => %s)""",
             (req.older_than_days,))
    return {"open": [{"id": r[0], "text": r[1], "with": r[2],
                      "stated_at": r[3].isoformat(), "age_days": r[5].days}
                     for r in rows]}


class GroundReq(BaseModel):
    claim: str
    limit: int = 10


@app.post("/ground")
def ground(req: GroundReq):
    """Attach evidence to a Hindsight claim — including contradicting evidence.

    Hindsight holds ~5,200 curated facts with no evidence trail. Chronicle
    holds 681k events that are nothing but evidence. A fact is stale when the
    evidence says it changed, not when a timer expires.
    """
    vec = _state["embedder"].encode_one(req.claim).tolist()
    rows = q("""SELECT h.segment_id, h.rrf_score, e.started_at, e.raw_text,
                       e.source_event_ids
                FROM hybrid_search(%s::halfvec, %s, NULL, NULL, NULL, NULL, 100, %s) h
                JOIN segment e USING (segment_id)
                -- chronological, not by score: a change of position over time
                -- is the signal, and score-ordering would hide it
                ORDER BY e.started_at""",
             (vec, req.claim, req.limit))
    return {"claim": req.claim,
            "evidence": [{"segment_id": r[0], "date": r[2].isoformat(),
                          "text": r[3][:1500], "source_event_ids": r[4]}
                         for r in rows],
            "note": "presented chronologically so a change of position is visible; "
                    "later evidence contradicting earlier evidence is the signal, "
                    "not a retrieval error"}
