"""Session segmentation — the highest-value stage in the pipeline.

681,331 Telegram messages -> ~50,000 segments.

Evidence (SeCom, ICLR 2025 — LoCoMo GPT4Score by memory unit):
    segment-level  71.57   <- what this module targets
    turn-level     65.58
    session-level  63.16
    summaries      53.87-56.25   <- worst. Do not build a summary pyramid.

Those numbers are at ~30 tokens/turn. The reference archive's median
Telegram message is ~14 characters, so the gap is larger, not smaller.

Signals used, in order of reliability:
    1. adaptive time gap   deterministic, free, 100% reliable as a boundary
    2. hard caps           <=30 events, <=250 tokens
    3. reply edges         only 7.9% coverage, but a reply crossing a proposed
                           boundary is strong evidence the boundary is wrong

Explicitly NOT used: learned/embedding topic segmentation. SOTA dialogue
segmentation reaches Pk 38.11 on realistic data (DialSTART/Doc2Dial) — it
misclassifies boundary pairs 38% of the time. Conversation disentanglement
peaks at 36.2 F1 with 77k human annotations. Time gaps beat both, for free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

import numpy as np

#: Bump when segmentation behaviour changes. Segments record this so you can
#: A/B a new segmenter and rebuild only what the change affects.
SEGMENTER_VERSION = "seg-2026.07-timegap-v1"


@dataclass
class GapFit:
    chat_id: int
    n_samples: int
    p50: float
    p90: float
    valley_seconds: float
    threshold_seconds: int


def fit_gap_threshold(
    timestamps: Sequence[datetime],
    chat_id: int,
    min_samples: int = 100,
    clamp_low: int = 600,      # 10 min
    clamp_high: int = 21600,   # 6 h
) -> GapFit:
    """Find the bimodal valley in log-spaced inter-message gaps.

    Falls back to the p90 gap when the distribution isn't cleanly bimodal,
    which is the common case for low-volume chats.
    """
    if len(timestamps) < min_samples:
        return GapFit(chat_id, len(timestamps), 0.0, 0.0, 0.0, 1800)

    ts = np.sort(np.array([t.timestamp() for t in timestamps]))
    gaps = np.diff(ts)
    gaps = gaps[gaps > 0]
    if gaps.size < min_samples:
        return GapFit(chat_id, gaps.size, 0.0, 0.0, 0.0, 1800)

    p50 = float(np.percentile(gaps, 50))
    p90 = float(np.percentile(gaps, 90))

    # Histogram in log space — gaps span seconds to months, so linear bins
    # would put everything in one bucket.
    log_gaps = np.log10(gaps)
    counts, edges = np.histogram(log_gaps, bins=60)

    # Smooth, then look for the deepest local minimum between the two largest
    # peaks. Search window is bounded to the plausible threshold range.
    kernel = np.array([1, 2, 3, 2, 1], dtype=float)
    kernel /= kernel.sum()
    smooth = np.convolve(counts.astype(float), kernel, mode="same")

    lo_idx = int(np.searchsorted(edges, np.log10(clamp_low)))
    hi_idx = int(np.searchsorted(edges, np.log10(clamp_high)))
    lo_idx = max(1, min(lo_idx, len(smooth) - 2))
    hi_idx = max(lo_idx + 1, min(hi_idx, len(smooth) - 1))

    window = smooth[lo_idx:hi_idx]
    if window.size == 0:
        valley = p90
    else:
        valley_bin = lo_idx + int(np.argmin(window))
        valley = float(10 ** edges[valley_bin])

    # Guard: if the "valley" is not actually a dip (flat/unimodal
    # distribution), the p90 gap is the more honest fallback.
    if not _is_real_valley(smooth, lo_idx, hi_idx):
        valley = p90

    threshold = int(np.clip(valley, clamp_low, clamp_high))
    return GapFit(chat_id, int(gaps.size), p50, p90, valley, threshold)


def _is_real_valley(smooth: np.ndarray, lo: int, hi: int, ratio: float = 0.6) -> bool:
    """A dip counts only if it is meaningfully below the peaks on both sides."""
    window = smooth[lo:hi]
    if window.size < 3:
        return False
    v = float(window.min())
    left_peak = float(smooth[:lo].max()) if lo > 0 else 0.0
    right_peak = float(smooth[hi:].max()) if hi < smooth.size else 0.0
    if left_peak <= 0 or right_peak <= 0:
        return False
    return v < ratio * min(left_peak, right_peak)

@dataclass
class Event:
    message_id: int
    chat_id: int
    sender_id: int | None
    sender_name: str
    ts: datetime
    text: str
    reply_to_id: int | None = None
    media_type: str | None = None
    transcript: str | None = None
    ocr_text: str | None = None

    @property
    def searchable_text(self) -> str:
        parts = [self.text or ""]
        if self.transcript:
            parts.append(f"[voice] {self.transcript}")
        if self.ocr_text:
            parts.append(f"[image text] {self.ocr_text}")
        if not any(p.strip() for p in parts) and self.media_type:
            parts.append(f"[{self.media_type}]")
        return " ".join(p for p in parts if p.strip())


@dataclass
class Segment:
    chat_id: int
    messages: list[Event] = field(default_factory=list)
    segmenter_version: str = SEGMENTER_VERSION

    @property
    def started_at(self) -> datetime:
        return self.messages[0].ts

    @property
    def ended_at(self) -> datetime:
        return self.messages[-1].ts

    @property
    def message_ids(self) -> list[int]:
        return [m.message_id for m in self.messages]

    @property
    def participant_ids(self) -> list[int]:
        return sorted({m.sender_id for m in self.messages if m.sender_id is not None})

    @property
    def raw_text(self) -> str:
        """What you RETURN to the user. Speaker-attributed, chronological."""
        return "\n".join(
            f"{m.sender_name}: {m.searchable_text}"
            for m in self.messages
            if m.searchable_text
        )

    @property
    def approx_tokens(self) -> int:
        # Cyrillic tokenizes at roughly 2.5 chars/token for mixed RU/UK/EN —
        # noticeably worse than the ~4 chars/token English assumption baked
        # into every paper cited in the research report.
        return max(1, len(self.raw_text) // 3)

    def is_substantive(self, min_chars: int = 80, min_messages: int = 3) -> bool:
        """Filler bursts ('ок' / 'ага' / '+' / stickers) are not memories."""
        if len(self.messages) < min_messages:
            return False
        content = sum(len(m.text or "") for m in self.messages)
        if content < min_chars:
            return False
        distinct = {(m.text or "").strip().lower() for m in self.messages}
        return len(distinct) >= 3


def segment_chat(
    messages: Iterable[Event],
    gap_seconds: int,
    max_messages: int = 30,
    max_tokens: int = 250,
    merge_below: int = 3,
) -> list[Segment]:
    """Segment one chat's message stream into segments.

    Splits on: time gap, hard message cap, hard token cap.
    Suppresses a split when an explicit reply edge crosses it.
    Then merges runt segments into whichever neighbour is closer in time.
    """
    evs = sorted(messages, key=lambda m: (m.ts, m.message_id))
    if not evs:
        return []

    segments: list[Segment] = []
    current = Segment(chat_id=evs[0].chat_id, messages=[evs[0]])
    open_ids = {evs[0].message_id}

    for ev in evs[1:]:
        gap = (ev.ts - current.messages[-1].ts).total_seconds()

        # A reply pointing back into the open session is direct evidence of
        # topical continuity — the only place the 7.9% reply coverage earns
        # its keep. Bounded so a stale reply can't create a giant session.
        reply_continues = (
            ev.reply_to_id is not None
            and ev.reply_to_id in open_ids
            and gap < gap_seconds * 4
        )

        split = (
            (gap > gap_seconds and not reply_continues)
            or len(current.messages) >= max_messages
            or current.approx_tokens >= max_tokens
        )

        if split:
            segments.append(current)
            current = Segment(chat_id=ev.chat_id, messages=[ev])
            open_ids = {ev.message_id}
        else:
            current.messages.append(ev)
            open_ids.add(ev.message_id)

    segments.append(current)
    return _merge_runts(segments, merge_below, max_messages)


def _merge_runts(segments: list[Segment], merge_below: int, max_messages: int) -> list[Segment]:
    """Fold sub-threshold segments into the temporally nearest neighbour.

    A 1-event segment is per-message indexing reintroduced through the back
    door — the exact failure this module exists to prevent.

    Single pass, O(n). An earlier version called `segments.index(s)` inside the
    loop, which is O(n^2) AND matches by dataclass equality rather than
    identity, so two structurally identical segments would resolve to the same
    index. At ~50k segments that is both slow and wrong.
    """
    if len(segments) <= 1:
        return segments

    out: list[Segment] = []
    for idx, seg in enumerate(segments):
        if len(seg.messages) >= merge_below or not out:
            out.append(seg)
            continue

        prev = out[-1]
        gap_prev = (seg.started_at - prev.ended_at).total_seconds()
        gap_next = float("inf")
        if idx + 1 < len(segments):
            gap_next = (segments[idx + 1].started_at - seg.ended_at).total_seconds()

        if gap_prev <= gap_next and len(prev.messages) + len(seg.messages) <= max_messages * 2:
            prev.messages.extend(seg.messages)
        else:
            # Leave it standing; the next iteration may absorb it forward.
            out.append(seg)
    return out
# ============================================================================
#  2. EMBED TEXT CONSTRUCTION
#
#  Two independent benchmarks agree on the shape:
#    LongMemEval: key = value + extracted facts  ->  +9.4% recall, +5.4% QA.
#                 And explicitly: "using these condensed forms ALONE does not
#                 enhance memory recall" — facts CONCATENATE, never substitute.
#    LoCoMo:      observations (38.0) > raw turns (35.8) > summaries (31.5).
#
#  The deterministic header is the cheap half of Anthropic's contextual
#  retrieval (-35% failure rate) at zero LLM cost and zero privacy exposure —
#  and it injects exactly the metadata real queries contain ("what did Anna
#  say about the apartment in 2021").
# ============================================================================

def build_embed_text(
    raw_text: str,
    started_at: datetime,
    chat_title: str,
    participant_names: Sequence[str],
    facts: Sequence[str] = (),
    topics: Sequence[str] = (),
) -> str:
    """Primitives, not a `Segment`: the worker builds segments from event
    rows, and until 2026-09-26 that is why nothing called this — the worker
    wrote its own weaker `[thread:] [source:]` header instead."""
    header = (
        f"[chat: {chat_title}] "
        f"[with: {', '.join(participant_names)}] "
        f"[date: {started_at:%Y-%m}] "
        f"[weekday: {started_at:%A}]"
    )
    parts = [header, raw_text]
    if topics:
        parts.append("|| topics: " + ", ".join(topics))
    if facts:
        parts.append("|| facts: " + " ; ".join(facts))
    return "\n".join(parts)


