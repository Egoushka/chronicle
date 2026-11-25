"""Mail adapter: the owner's mailbox, one event per human-written message.

Reads the Maildir that mail-sync (mbsync, pull-only) keeps: `<root>/<account>/
<folder>/{cur,new}/<file>`. Read-only against the files, headers first, so a
10k-message folder costs a header parse per file and a body read only for the
messages that pass the filters.

What it emits per message: sender, subject and a one- or two-line snippet of
the first text part. Never an attachment, never a quoted reply, never the
raw header block. Secrets in the snippet are redacted by the worker like any
other event (`chronicle.redact`).

Filters, because "all mail" is the wrong source mix (a mailbox is mostly
machines): only the folders in `folders` (default INBOX), and a message with
List-Unsubscribe, `Precedence: bulk|list|junk` or `Auto-Submitted` other than
`no` is skipped. `exclude_senders` drops addresses by substring.

Resume: mbsync names a file `<download epoch>.<n>_<m>.<host>,U=<uid>:2,<flags>`.
The download epoch is the watermark, so mail that arrives late with an old
Date header is still seen; `ts` is the Date header.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesHeaderParser, BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Iterator

from .base import Density, FileAdapter, SourceEvent, register

log = logging.getLogger(__name__)

#: First ingest reaches back this far. A mailbox is years deep and most of it
#: is not life signal; a deliberate backfill passes `since`.
BACKFILL = timedelta(days=365)
SNIPPET_CHARS = 240

_QUOTE = re.compile(r"^\s*(>|On .+ wrote:|Le .+ a écrit|-----Original Message)", re.I)
_EPOCH = re.compile(r"^(\d{9,11})\.")


def _snippet(msg) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is None:
        return ""
    try:
        body = part.get_content()
    except Exception:                                    # noqa: BLE001 (bad charset)
        return ""
    lines: list[str] = []
    for ln in body.splitlines():
        if _QUOTE.match(ln):
            break                                        # everything below is the thread
        ln = ln.strip()
        if ln and not ln.startswith(("--", "http")):
            lines.append(ln)
        if len(lines) == 2:
            break
    return " ".join(lines)[:SNIPPET_CHARS]


@register
class MailAdapter(FileAdapter):
    source = "mail"
    density = Density.DISCRETE
    # Other people's mail: a preflight check must not echo it.
    show_sample = False

    def __init__(self, root: str | Path, folders: tuple[str, ...] = ("INBOX",),
                 exclude_senders: tuple[str, ...] = ()):
        super().__init__(root)
        self.folders = folders
        self.exclude_senders = tuple(s.lower() for s in exclude_senders)

    def _files(self) -> Iterator[tuple[datetime, Path, str, str]]:
        if not self.root.exists():
            raise FileNotFoundError(f"{self.root} not mounted")
        for account in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for folder in self.folders:
                for sub in ("cur", "new"):
                    d = account / folder / sub
                    if not d.is_dir():
                        continue
                    for f in d.iterdir():
                        m = _EPOCH.match(f.name)
                        if m:
                            yield (datetime.fromtimestamp(int(m.group(1)), timezone.utc),
                                   f, account.name, folder)

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        now = datetime.now(timezone.utc)
        floor = since or (now - BACKFILL)
        cands = sorted((c for c in self._files() if c[0] > floor
                        and (until is None or c[0] <= until)),
                       key=lambda c: (c[0], c[1].name))
        for arrived, path, account, folder in cands:
            ev = self._event(arrived, path, account, folder, now)
            if ev is not None:
                yield ev

    def _event(self, arrived, path, account, folder, now) -> SourceEvent | None:
        try:
            with path.open("rb") as fh:
                head = BytesHeaderParser(policy=policy.default).parse(fh)
            if (head.get("List-Unsubscribe")
                    or str(head.get("Precedence", "")).lower() in ("bulk", "list", "junk")
                    or str(head.get("Auto-Submitted", "no")).lower() != "no"):
                return None
            name, addr = parseaddr(str(head.get("From", "")))
            addr = addr.lower()
            if not addr or any(s in addr for s in self.exclude_senders):
                return None
            with path.open("rb") as fh:
                msg = BytesParser(policy=policy.default).parse(fh)
            snippet = _snippet(msg)
            subject = " ".join(str(head.get("Subject", "")).split())[:160]
            try:
                ts = parsedate_to_datetime(str(head.get("Date", "")))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                ts = arrived
            if ts > now + timedelta(days=1):             # a clock-skewed sender
                ts = arrived
        except Exception as exc:                          # noqa: BLE001
            log.warning("mail: skipped %s: %s", path.name, type(exc).__name__)
            return None

        mid = str(head.get("Message-ID", "")) or path.name.split(":")[0]
        sender = name or addr
        text = f"From {sender}: {subject}" + (f" - {snippet}" if snippet else "")
        return SourceEvent(
            source=self.source,
            source_id=hashlib.sha1(f"{account}/{mid}".encode()).hexdigest()[:20],
            ts=ts.astimezone(timezone.utc), text=text, actor=addr, kind="message",
            payload={"folder": folder, "subject": subject, "from": addr},
            thread_key=f"mail:{account}:{folder}", watermark_ts=arrived)
