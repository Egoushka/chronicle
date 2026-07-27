"""Source adapter interface.

The point of this file is that Chronicle is NOT a Telegram tool. Telegram is
the richest source, not the only one. Every adapter turns some existing
homelab Postgres into the same `Event` shape, and everything downstream —
segmentation, embedding, facts, retrieval — is source-agnostic.

Hard rule: every adapter must be independently droppable. If the wakapi
adapter rots, Chronicle loses one source and keeps working. Anything that
makes a source mandatory is a design error.

Adding a source is: subclass, implement `fetch`, register in ADAPTERS.
Typically 50-150 lines.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

log = logging.getLogger(__name__)


@dataclass
class SourceEvent:
    """One timestamped thing that happened, from any source.

    `text` is what gets indexed. `payload` is everything else, kept verbatim
    so a future extraction schema can mine it without re-ingesting.
    """

    source: str                       # telegram | wakapi | dawarich | immich | ...
    source_id: str                    # stable id WITHIN that source
    ts: datetime
    text: str = ""
    actor: str | None = None          # who did it (sender, device, account)
    kind: str | None = None           # message | heartbeat | location | photo | txn
    payload: dict = field(default_factory=dict)

    # Only Telegram populates this today (7.9% of rows). Kept on the base
    # class because any threaded source could use it.
    reply_to: str | None = None

    def dedupe_key(self) -> str:
        return f"{self.source}:{self.source_id}"


class Adapter(ABC):
    """Pull events from one upstream system.

    Adapters are READ-ONLY against their source. Chronicle never writes back
    into telegram-sync, wakapi, dawarich or anything else — those systems own
    their data and Chronicle owns its projection of it.
    """

    source: str
    #: Whether this source produces conversational text worth segmenting into
    #: episodes, or discrete events that only need timeline placement.
    conversational: bool = False

    @abstractmethod
    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        """Yield events in ascending timestamp order.

        Must be resumable: called with `since` = the last successfully
        ingested timestamp, it returns everything after that and nothing
        before. The batch worker relies on this after an OOM kill.
        """

    def healthcheck(self) -> tuple[bool, str]:
        """Return (ok, detail). A failing adapter must never fail the run."""
        try:
            next(iter(self.fetch(until=datetime.min)), None)
            return True, "reachable"
        except Exception as exc:                       # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


ADAPTERS: dict[str, type[Adapter]] = {}


def register(cls: type[Adapter]) -> type[Adapter]:
    ADAPTERS[cls.source] = cls
    return cls
