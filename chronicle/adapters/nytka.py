"""Nytka — ambient speech from a wearable, read in place from its server.

Nytka is a self-hosted server that turns a pendant's audio into transcripts.
Its PostgreSQL holds `conversations` (a run of speech closed after a 2-minute
gap) and `segments` (one utterance each, with a speaker label). Unlike every
other source here, most of what it holds is not the owner's: it is whoever
stood near the owner, and none of them wrote it down. So:

  * The adapter keeps three exits closed: a muted stretch is never read, a
    conversation deleted upstream is purged, and any one conversation can be
    left out by id (`NYTKA_EXCLUDE_CONVERSATIONS`).
  * `doctor` must not print an utterance (`show_sample = False`).
  * Facts about these people never leave for a curated memory: the source has
    no Hindsight bank (sources.py) and migration 008 keeps it out of
    `v_promotable_facts`.

The unit is the same as telegram's: one utterance is one event, a
conversation is one thread, and chronicle's segmenter groups the utterances
(65% of Telegram messages are under 20 chars; here 37% are, so the same
reasoning holds). Measured 2026-10-04: 4,448 utterances in 105 conversations
over five days became 456 segments at the default cap.

Two facts about the upstream that the queries below depend on:

  * `updated_at` moves whenever speech is added or the conversation closes
    (ConversationStore assigns a batch with `status = 'open', updated_at = now`
    and `CloseIdleAsync` sets `closed`), and an AI title or summary does NOT
    move it. So it is a complete change cursor for the text, and only closed
    conversations are read: an open one is still growing.
  * Deleting a conversation is a hard DELETE that cascades to its segments —
    there is no tombstone — and two conversations bridged by late audio are
    MERGED: the survivor takes the others' segments, the others are deleted.
    So "gone upstream" cannot be judged by conversation id alone; see
    `excluded_thread_keys`.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Iterator

from .base import Density, SourceEvent, SqlAdapter, register

log = logging.getLogger(__name__)

#: How far back each resume re-reads, on top of the watermark. `updated_at` is
#: stamped by the app before its transaction commits, so a conversation stamped
#: at T can become visible after one stamped T+1 was read. Re-reading is free:
#: the worker's upsert is a no-op on unchanged text.
SYNC_LAG = timedelta(minutes=10)

#: What the segment header says instead of a title. A conversation's own title
#: (`ai_title`) is a model's inference about the speech, and the header is
#: embedded; the thread key is a UUID, which is only noise in a vector.
CHAT_TITLE = "Nytka"

Window = tuple[frozenset[int], time, time]     # ISO weekdays of the start day, start, end


def parse_mute_windows(text: str | None) -> list[Window]:
    """The `mute.windows` setting: a JSON list of weekly windows.

    Raises on anything malformed, with a fixed sentence and never the value:
    unreadable windows must stop the source, not silently let muted speech in.
    Nytka validates the setting when it is saved, so this only fires on drift.
    """
    if not text or not text.strip():
        return []
    try:
        raw = json.loads(text)
        out: list[Window] = []
        for w in raw:
            days = frozenset(int(d) for d in w["days"])
            if not days or not days <= set(range(1, 8)):
                raise ValueError
            out.append((days, time.fromisoformat(w["start"]), time.fromisoformat(w["end"])))
        return out
    except (ValueError, KeyError, TypeError):
        raise ValueError("Nytka setting mute.windows is not a list of "
                         "{days, start, end} windows") from None


def is_muted(ts: datetime, windows: list[Window], tz) -> bool:
    """Is this instant inside any window, by the owner's wall clock?

    Same reading as the server's: `days` name the day a window STARTS on, and an
    end at or before the start crosses midnight into the next day.
    """
    if not windows:
        return False
    local = ts.astimezone(tz).replace(tzinfo=None)
    for day in (local.date(), local.date() - timedelta(days=1)):
        for days, start, end in windows:
            if day.isoweekday() not in days:
                continue
            begin = datetime.combine(day, start)
            finish = datetime.combine(day, end)
            if end <= start:
                finish += timedelta(days=1)
            if begin <= local < finish:
                return True
    return False


def _zone(name: str | None):
    """The owner's zone; an unknown id falls back to UTC, as the server does."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(name) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def chronicle_index(db_url: str) -> Callable[[], dict[str, list[str]]]:
    """What chronicle holds of this source: thread_key -> its source_ids.

    Erasure compares this with the upstream, and the adapter has no other way
    to see chronicle's side, so `doctor.build` hands it a lookup rather than
    the adapter opening a second connection by itself.
    """
    def read() -> dict[str, list[str]]:
        import psycopg
        with psycopg.connect(db_url) as conn, conn.cursor() as cur:
            cur.execute("""SELECT thread_key, array_agg(source_id)
                             FROM event WHERE source = 'nytka' GROUP BY thread_key""")
            return {k: list(ids) for k, ids in cur}
    return read


@register
class NytkaAdapter(SqlAdapter):
    source = "nytka"
    dialect = "postgres"
    density = Density.NARRATIVE
    show_sample = False

    def __init__(self, dsn: str, exclude_conversation_ids: tuple[str, ...] = (),
                 indexed: Callable[[], dict[str, list[str]]] | None = None):
        super().__init__(dsn)
        # Parsed, not just split: the ids are bound as uuid[], and a typo
        # should fail at construction rather than as a SQL error mid-batch.
        self.exclude_conversation_ids = tuple(
            str(uuid.UUID(c)) for c in exclude_conversation_ids)
        self._indexed = indexed

    def _settings(self) -> tuple[list[Window], object]:
        rows = dict(self._stream(
            "SELECT key, value FROM settings"
            " WHERE key IN ('mute.windows', 'user.timeZone')", {}))
        return parse_mute_windows(rows.get("mute.windows")), _zone(rows.get("user.timeZone"))

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        """Every utterance of a closed conversation changed after `since`.

        `since`/`until` bound `conversations.updated_at`, which is also the
        watermark: rows stream in that order, so a committed batch is a prefix
        and resuming from its maximum skips nothing. The overlap re-reads the
        last conversation or two on every run, which is what keeps a crash
        between two batches of one long conversation from losing its tail.

        Not read: speech inside a mute window (the server drops such audio
        before transcription, but only from the moment the window was saved —
        on 2026-10-04 the 350 segments inside the configured window were all
        captured earlier), empty text, and the conversations in
        `exclude_conversation_ids`.
        """
        windows, zone = self._settings()
        sql = """
            SELECT s.id, s.conversation_id::text, c.source, c.updated_at,
                   s.started_at, s.text, s.is_user, s.speaker, s.speaker_id,
                   p.name
              FROM conversations c
              JOIN segments s ON s.conversation_id = c.id
              -- A named voice resolves at read time, as the server does: naming
              -- a voice renames its past utterances. An unnamed one keeps its
              -- raw label, which is only unique within one conversation.
              LEFT JOIN person_voices pv ON pv.speaker_id = s.speaker_id
              LEFT JOIN people p ON p.id = pv.person_id
             WHERE c.status = 'closed'
               AND (%(since)s::timestamptz IS NULL OR c.updated_at >= %(since)s::timestamptz)
               AND (%(until)s::timestamptz IS NULL OR c.updated_at <= %(until)s::timestamptz)
               AND c.id <> ALL(%(excluded)s::uuid[])
               AND btrim(s.text) <> ''
             ORDER BY c.updated_at, c.id, s.started_at, s.id
        """
        def aware(dt: datetime | None) -> datetime | None:
            return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        since = aware(since)
        params = {"since": since - SYNC_LAG if since else None,
                  "until": aware(until),
                  "excluded": list(self.exclude_conversation_ids)}
        muted = 0
        for (seg_id, conv, origin, updated_at, started_at, text, is_user,
             speaker, speaker_id, person) in self._stream(sql, params):
            if is_muted(started_at, windows, zone):
                muted += 1
                continue
            yield SourceEvent(
                source=self.source,
                source_id=str(seg_id),
                ts=started_at,
                text=text.strip(),
                # The wearer is `me`, as in telegram. Anyone else is the name
                # the owner gave their voice, else the label the transcriber
                # gave it, else unknown (rendered "?").
                actor="me" if is_user else (person or speaker or None),
                kind="utterance",
                payload={"conversation_id": conv, "origin": origin,
                         "speaker_id": speaker_id, "chat_title": CHAT_TITLE},
                thread_key=f"{self.source}:{conv}",
                watermark_ts=updated_at,
            )
        if muted:
            log.info("nytka: skipped %d utterance(s) inside mute windows", muted)

    def excluded_thread_keys(self) -> set[str]:
        """Conversations chronicle holds and should not.

        Two rules, the first of which needs no memory of chronicle's:

          * the ones named in `exclude_conversation_ids`;
          * the ones deleted upstream. A thread is gone only when its
            conversation is gone AND none of its segments survive anywhere. A
            merged-away conversation is also deleted upstream, but its
            segments live on in the survivor; chronicle keeps their events
            under the old thread key, and purging that key would erase speech
            that still exists and that no later ingest would bring back.

        Without a `chronicle_index` the second rule cannot see what chronicle
        holds and contributes nothing.
        """
        keys = {f"{self.source}:{c}" for c in self.exclude_conversation_ids}
        if self._indexed is None:
            return keys

        prefix = f"{self.source}:"
        indexed = {k: v for k, v in self._indexed().items() if k.startswith(prefix)}
        if not indexed:
            return keys

        live = {row[0] for row in self._stream(
            "SELECT id::text FROM conversations WHERE id = ANY(%(ids)s::uuid[])",
            {"ids": [k[len(prefix):] for k in indexed]})}
        gone = {k: v for k, v in indexed.items() if k[len(prefix):] not in live}
        if not gone:
            return keys

        alive = {row[0] for row in self._stream(
            "SELECT id::text FROM segments WHERE id = ANY(%(ids)s::bigint[])",
            {"ids": sorted({int(i) for v in gone.values() for i in v})})}
        return keys | {k for k, v in gone.items() if not alive.intersection(v)}
