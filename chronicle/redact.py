"""Secret redaction — before text reaches the index, the MCP or an LLM.

Every event's text becomes a segment's raw_text, which is embedded, full-text
indexed, returned verbatim by /recall and every MCP tool, and sent to the
enrich model. A credential in one event is therefore in five places and one
of them is a third-party API. Tier 3 made this concrete: a karakeep TEXT
bookmark on the reference deployment holds a plaintext credential. But the
rule is not per source — a token pasted into a chat is the same leak — so
`worker._write_events` applies it to every event of every tier.

Redaction, not rejection: the event keeps its place in the timeline and its
surrounding text ("sent Anna the wifi password") still answers questions.
Only the value is replaced, by `[REDACTED:<kind>]`, so a reader can tell a
redaction from a gap and the kind survives for counting.

Deliberately pattern-based, no entropy heuristic. High-entropy strings in a
7-year chat archive are mostly links, hashes and base64 stickers; flagging
them would redact real content in bulk. Each pattern below is a documented
token format or a secret explicitly labelled as one.

    python -m chronicle.redact                # dry run: counts per kind, no values
    python -m chronicle.redact --apply        # rewrite stored events + their segments

The backfill is the other half, as `purge` is to a tightened filter: ingest
redacts what arrives from now on and does nothing to what is already stored.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from typing import Any

log = logging.getLogger("chronicle.redact")

#: (kind, pattern). A pattern with a group named `v` redacts only that group,
#: keeping the label around it; otherwise the whole match goes.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [(k, re.compile(p)) for k, p in [
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ("aws_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})"),
    ("gitlab_token", r"\bglpat-[A-Za-z0-9_-]{20,}"),
    # OpenAI, Anthropic and LiteLLM virtual keys all start `sk-`.
    ("api_key", r"\bsk-[A-Za-z0-9_-]{20,}"),
    ("slack_token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("stripe_key", r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{16,}"),
    # A bot token is a bot id, a colon, and 35 characters starting `AA`.
    ("telegram_bot_token", r"\b\d{8,10}:AA[0-9A-Za-z_-]{33}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    # user:password@host inside a URL. The host stays: it is the useful half.
    # `[REDACTED:url_password]` has the same user:pass shape, so without the
    # guard a second pass re-redacts it and every re-read churns the segment.
    ("url_password", r"(?<=://)(?!\[REDACTED)(?P<v>[^\s/:@]+:[^\s/@]+)(?=@)"),
    # A value explicitly labelled as a secret, in the languages of the
    # reference archive. Only `:` or `=` after the label — a dash would take
    # "token-based" — and one whitespace-free value of 4+ characters, so
    # "пароль: скину пізніше" loses one word, not the sentence. Never a value
    # an earlier pattern already replaced.
    # `(?<![^\W_])`: a word start, or after `_`, so `DB_PASSWORD=` counts.
    ("labelled", r"(?i)(?<![^\W_])(?:password|passwd|pwd|passphrase|пароль|пасс|"
                 r"secret|api[ _-]?key|token|токен)\s*[:=]\s*"
                 r"(?!\[REDACTED)(?P<v>[^\s,;]{4,})"),
]]

# The shape the reference deployment's karakeep actually held, five times:
# a bare secret alone on one line and its label on ANOTHER, after it —
# "<64 chars>\n\n<service> api token". No token format matches and nothing
# precedes the value, so no pattern above can see it. The rule is contextual:
# once a text names a secret anywhere, every long whitespace-free token in it
# that mixes letters and digits and is not a URL is taken to be one — 20+
# characters: the five real values were 23-64, and at 12 "Claude35Sonnet"
# next to the words "api key" was redacted — and ALONE ON ITS LINE: anywhere
# in a line, a live-archive audit hit MusicBrainz ids in lastfm's one-line
# JSON and long identifiers in pasted C# near "foreign key". Never a URL
# (XAML's `xmlns:x="http://…"` lines) and never code shape (`_CODE`). Without
# the label, the same token is a hash or an id and stays — which is what keeps
# a chat archive full of links and commit SHAs from being redacted in bulk.
_LABEL = re.compile(r"(?i)(?:key|token|pass|passwd|password|passphrase|pwd|secret|"
                    r"парол\w*|ключ\w*|токен\w*)\b")
_BARE = re.compile(r"(?m)^[ \t]*(?!\[REDACTED)(?!\S*://)(?!www\.)"
                   r"(?=\S*\d)(?=\S*[A-Za-z])(?P<v>\S{20,})[ \t]*$")
# Code, not a credential: a call, a statement end, a block, an attribute.
# Characters alone cannot tell them apart — two of the five real passwords
# hold punctuation that a character blocklist dropped.
_CODE = re.compile(r"\(.*\)|[;{}]$|=[\"']")


def redact(text: str | None) -> tuple[str | None, Counter]:
    """`text` with every secret replaced, and what was found, by kind.

    Idempotent: redacting redacted text finds nothing. Ingest depends on it —
    the overlap window re-reads rows, and a second redaction that differed
    from the stored text would rewrite the event and clear its segment's
    embedding on every run.
    """
    found: Counter = Counter()
    if not text:
        return text, found
    for kind, pat in PATTERNS:
        def sub(m: re.Match, kind: str = kind) -> str:
            found[kind] += 1
            if "v" in pat.groupindex:
                s, e = m.span("v")
                return (m.string[m.start():s] + f"[REDACTED:{kind}]"
                        + m.string[e:m.end()])
            return f"[REDACTED:{kind}]"
        text = pat.sub(sub, text)
    if _LABEL.search(text):
        def bare(m: re.Match) -> str:
            if _CODE.search(m.group("v")):
                return m.group(0)
            found["bare_secret"] += 1
            s, e = m.span("v")
            return m.string[m.start():s] + "[REDACTED:bare_secret]" + m.string[e:m.end()]
        text = _BARE.sub(bare, text)
    return text, found


def redact_obj(value: Any, found: Counter | None = None) -> tuple[Any, Counter]:
    """`redact` over every string in a JSON-shaped payload, recursively."""
    found = Counter() if found is None else found
    if isinstance(value, str):
        out, f = redact(value)
        found.update(f)
        return out, found
    if isinstance(value, dict):
        return {k: redact_obj(v, found)[0] for k, v in value.items()}, found
    if isinstance(value, list):
        return [redact_obj(v, found)[0] for v in value], found
    return value, found


# ---------------------------------------------------------------------------
#  backfill
# ---------------------------------------------------------------------------

def scan(conn) -> list[tuple[str, str, Any, str, dict, Counter]]:
    """Stored events that would change, as (source, source_id, ts, text,
    payload, found). A named server-side cursor: the archive is ~700k rows."""
    hits = []
    with conn.cursor(name="redact_scan") as cur:
        cur.itersize = 5_000
        cur.execute("SELECT source, source_id, ts, text, payload FROM event")
        for source, sid, ts, text, payload in cur:
            new_text, found = redact(text)
            new_payload, found = redact_obj(payload, found)
            if found:
                hits.append((source, sid, ts, new_text, new_payload, found))
    return hits


def apply(conn, hits) -> int:
    """Rewrite the events and every segment citing them, in one transaction.

    Segments go through the worker's own in-place refresh, so raw_text and
    embed_text are rebuilt from the redacted events and the embedding and
    enrichment are cleared for the next `embed`/`enrich`. Facts that enrich
    already extracted stay until that segment is enriched again.
    """
    from .worker import _refresh_segments

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE event SET text = %s, payload = %s::jsonb"
            " WHERE source = %s AND source_id = %s AND ts = %s",
            [(t, json.dumps(p, default=str), s, i, ts) for s, i, ts, t, p, _ in hits])
    refreshed = _refresh_segments(conn, [f"{s}:{i}" for s, i, *_ in hits])
    kinds = sum((h[5] for h in hits), Counter())
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO erasure_log (scope, scope_ref, completed_at, projections_pruned)
               VALUES ('secret_redaction', %s, now(), %s)""",
            (json.dumps({"events": len(hits), "segments_refreshed": refreshed,
                         "kinds": dict(kinds),
                         "sources": dict(Counter(h[0] for h in hits))}),
             refreshed))
    return refreshed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chronicle.redact")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite stored events; without it this only counts")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from .worker import connect
    conn = connect()
    try:
        hits = scan(conn)
        # Counts only. Printing a match would put the secret in a terminal
        # and a log, which is the leak this exists to close.
        by_source = Counter(h[0] for h in hits)
        kinds = sum((h[5] for h in hits), Counter())
        print(f"{len(hits):,} event(s) hold a secret")
        for source, n in sorted(by_source.items()):
            print(f"    {source:<24} {n:>8,}")
        for kind, n in kinds.most_common():
            print(f"    [{kind}]{'':<{22 - len(kind)}} {n:>8,}")

        if not args.apply:
            print("\nDRY RUN — nothing changed. Re-run with --apply to commit.")
            return 0
        if not hits:
            print("\nnothing to redact; already clean.")
            return 0
        refreshed = apply(conn, hits)
        conn.commit()
        print(f"\nredacted {len(hits):,} event(s), refreshed {refreshed:,} segment(s)."
              " erasure_log written; run `embed` next.")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
