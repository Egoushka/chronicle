"""Channels with no local database — pulled over HTTP or MCP.

gmail, google calendar, notion, slack, jira, linkedin, lastfm, github.

These share a problem the local sources do not have: credentials, rate limits,
and no ability to do a cheap full-table scan. So they take an injected
`fetch_page` callable (in this homelab, a thin wrapper over the corresponding
MCP tool), and the worker walks bounded time windows resuming from
`source.last_ingested_at`.

Credentials never live in this package. If an adapter can see a token, that is
a bug.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .base import ApiAdapter, Density, SourceEvent, register

log = logging.getLogger(__name__)


@register
class LastfmAdapter(ApiAdapter):
    """Last.fm scrobbles.

    Underrated as a life signal and nearly free to ingest: it is dense,
    continuously recorded, timestamped to the second, and it is one of the few
    honest proxies for mood and daily rhythm that nobody curates. What you
    listened to at 3am in March 2022 is evidence.

    Individual scrobbles are noise, so they roll up into LISTENING SESSIONS
    the same way wakapi heartbeats do.
    """

    source = "lastfm"
    density = Density.TELEMETRY
    page_window = timedelta(days=90)

    SESSION_GAP = timedelta(minutes=30)

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        for start, stop in self._windows(since, until):
            tracks = self.fetch_page(start=start, stop=stop) or []
            yield from self._rollup(tracks)

    def _rollup(self, tracks: list[dict]) -> Iterator[SourceEvent]:
        buf: list[dict] = []

        def flush():
            if len(buf) < 3:                    # a couple of tracks is not a session
                return None
            start = buf[0]["played_at"]
            end = buf[-1]["played_at"]
            artists = [t.get("artist") for t in buf if t.get("artist")]
            top = max(set(artists), key=artists.count) if artists else "unknown"
            return SourceEvent(
                source=self.source,
                source_id=f"listen:{start.isoformat()}",
                ts=start,
                text=f"listened to {len(buf)} tracks, mostly {top}",
                actor="me", kind="listening_session",
                thread_key="lastfm:listening",
                payload={"tracks": len(buf), "top_artist": top,
                         "artists": sorted(set(artists))[:30],
                         "ended_at": end.isoformat(),
                         "hour": start.hour},
                watermark_ts=end,
            )

        for t in sorted(tracks, key=lambda x: x["played_at"]):
            if buf and t["played_at"] - buf[-1]["played_at"] > self.SESSION_GAP:
                ev = flush()
                if ev:
                    yield ev
                buf = []
            buf.append(t)
        ev = flush()
        if ev:
            yield ev


def lastfm_pages(api_key: str, user: str, http=None):
    """`fetch_page` for LastfmAdapter over the public REST API.

    A closure, so the key stays in doctor.build and never on the adapter.
    Last.fm is the one API source that needs no MCP wiring: a read key and a
    username, both already on the box for the Hindsight weekly music log.

    `user.getRecentTracks` with from/to is inclusive and paged at 200; the
    currently-playing track has no `date` and is skipped — it is not a scrobble
    yet, and the next run picks it up once it is.
    """
    import time

    if http is None:
        import httpx
        http = httpx.Client(timeout=30)
    client = http

    def fetch_page(start: datetime, stop: datetime) -> list[dict]:
        tracks, page, pages = [], 1, 1
        while page <= pages:
            r = client.get("https://ws.audioscrobbler.com/2.0/", params={
                "method": "user.getrecenttracks", "user": user, "api_key": api_key,
                "format": "json", "limit": 200, "page": page,
                "from": int(start.timestamp()), "to": int(stop.timestamp())})
            r.raise_for_status()
            body = r.json()["recenttracks"]
            pages = int(body.get("@attr", {}).get("totalPages") or 0)
            items = body.get("track") or []
            for t in items if isinstance(items, list) else [items]:
                if "date" not in t:
                    continue
                tracks.append({
                    "played_at": datetime.fromtimestamp(int(t["date"]["uts"]), timezone.utc),
                    "artist": (t.get("artist") or {}).get("#text"),
                    "track": t.get("name")})
            page += 1
            time.sleep(0.25)      # Last.fm asks for <5 req/s per key
        return tracks

    return fetch_page


@register
class CalendarAdapter(ApiAdapter):
    """Google Calendar — the structure of time.

    DISCRETE and high value: a calendar event is unambiguous, dated, and names
    the people involved. It is also the cheapest way to answer "what was I
    doing that week" for periods where you said nothing in chat.
    """

    source = "calendar"
    density = Density.DISCRETE
    page_window = timedelta(days=180)

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        for start, stop in self._windows(since, until):
            for ev in self.fetch_page(start=start, stop=stop) or []:
                yield SourceEvent(
                    source=self.source,
                    source_id=str(ev["id"]),
                    ts=ev["start"],
                    text=" ".join(x for x in (ev.get("summary"),
                                              ev.get("description")) if x),
                    actor="me", kind="calendar_event",
                    thread_key=f"calendar:{ev.get('calendar', 'primary')}",
                    payload={"attendees": ev.get("attendees", []),
                             "location": ev.get("location"),
                             "ended_at": ev.get("end").isoformat() if ev.get("end") else None,
                             "recurring": bool(ev.get("recurring_event_id"))},
                )


@register
class GmailAdapter(ApiAdapter):
    """Gmail — deliberate written communication outside Telegram.

    NARRATIVE, so it segments. Bodies are truncated: the identifying content
    of an email is in its first screenful, and full bodies would balloon the
    store for very little retrieval gain.

    Note this is the source most likely to contain third-party personal data
    that has nothing to do with Yehor (newsletters, automated mail). Filter
    hard at the query level in `fetch_page`, not here.
    """

    source = "gmail"
    density = Density.NARRATIVE
    page_window = timedelta(days=60)

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        for start, stop in self._windows(since, until):
            for m in self.fetch_page(start=start, stop=stop) or []:
                yield SourceEvent(
                    source=self.source,
                    source_id=str(m["id"]),
                    ts=m["date"],
                    text=f"{m.get('subject','')}\n{(m.get('body') or '')[:3000]}",
                    actor=m.get("from"),
                    kind="email",
                    thread_key=f"gmail:{m.get('thread_id', m['id'])}",
                    reply_to=m.get("in_reply_to"),
                    payload={"to": m.get("to", []), "labels": m.get("labels", []),
                             "subject": m.get("subject")},
                )


@register
class GithubAdapter(ApiAdapter):
    """GitHub — public commits, PRs, issues. Complements forgejo."""

    source = "github"
    density = Density.NARRATIVE
    page_window = timedelta(days=365)

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        for start, stop in self._windows(since, until):
            for e in self.fetch_page(start=start, stop=stop) or []:
                yield SourceEvent(
                    source=self.source, source_id=str(e["id"]), ts=e["ts"],
                    text=f"[{e.get('repo')}] {e.get('title') or e.get('message','')}",
                    actor=e.get("actor"), kind=e.get("type", "github_event"),
                    thread_key=f"github:{e.get('repo')}",
                    payload={"repo": e.get("repo"), "url": e.get("url"),
                             "type": e.get("type")},
                )


@register
class JiraAdapter(ApiAdapter):
    """Jira — ACME work. Ticket transitions are a precise work timeline.

    Maps onto the `acme` Hindsight bank, so promoted facts from this source
    should route there rather than to `work`.
    """

    source = "jira"
    density = Density.DISCRETE
    page_window = timedelta(days=180)

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        for start, stop in self._windows(since, until):
            for i in self.fetch_page(start=start, stop=stop) or []:
                yield SourceEvent(
                    source=self.source, source_id=i["key"], ts=i["updated"],
                    text=f"{i['key']}: {i.get('summary','')}\n{(i.get('description') or '')[:2000]}",
                    actor=i.get("assignee"), kind="ticket",
                    thread_key=f"jira:{i.get('project', 'BSO')}",
                    payload={"status": i.get("status"), "key": i["key"],
                             "project": i.get("project"),
                             "resolution": i.get("resolution")},
                )


@register
class SlackAdapter(ApiAdapter):
    """Slack — work conversation. NARRATIVE, segments like Telegram."""

    source = "slack"
    density = Density.NARRATIVE
    page_window = timedelta(days=30)

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        for start, stop in self._windows(since, until):
            for m in self.fetch_page(start=start, stop=stop) or []:
                yield SourceEvent(
                    source=self.source, source_id=f"{m['channel']}:{m['ts']}",
                    ts=m["datetime"], text=m.get("text", ""),
                    actor=m.get("user"), kind="message",
                    thread_key=f"slack:{m['channel']}",
                    reply_to=(f"{m['channel']}:{m['thread_ts']}"
                              if m.get("thread_ts") else None),
                    payload={"channel": m["channel"], "channel_name": m.get("channel_name")},
                )


@register
class NotionAdapter(ApiAdapter):
    """Notion — written notes. Study notes, project docs, thinking-out-loud.

    NARRATIVE, but note that Notion pages are EDITED, so `last_edited_time`
    is not when the thought happened. Both timestamps are kept; `ts` uses
    creation so the timeline places the thought when you had it.
    """

    source = "notion"
    density = Density.NARRATIVE
    page_window = timedelta(days=365)

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        for start, stop in self._windows(since, until):
            for p in self.fetch_page(start=start, stop=stop) or []:
                yield SourceEvent(
                    source=self.source, source_id=p["id"], ts=p["created_time"],
                    text=f"{p.get('title','')}\n{(p.get('content') or '')[:4000]}",
                    actor="me", kind="note",
                    thread_key=f"notion:{p.get('parent_id', 'root')}",
                    payload={"title": p.get("title"), "url": p.get("url"),
                             "last_edited": (p.get("last_edited_time").isoformat()
                                             if p.get("last_edited_time") else None)},
                )


@register
class LinkedinAdapter(ApiAdapter):
    """LinkedIn — posts and their engagement. Maps to the `social` bank."""

    source = "linkedin"
    density = Density.DISCRETE
    page_window = timedelta(days=365)

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        for start, stop in self._windows(since, until):
            for p in self.fetch_page(start=start, stop=stop) or []:
                yield SourceEvent(
                    source=self.source, source_id=str(p["id"]), ts=p["posted_at"],
                    text=p.get("text", ""), actor="me", kind="post",
                    thread_key="linkedin:posts",
                    payload={"impressions": p.get("impressions"),
                             "reactions": p.get("reactions"),
                             "comments": p.get("comments"), "url": p.get("url")},
                )
