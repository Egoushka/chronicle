"""Source policy — what Chronicle ingests, in what order, and why.

"Use all possible information channels" is the right instinct and it has a
specific failure mode. Chronicle exists because 681,331 Telegram messages were
indexed at the wrong UNIT — 65% of them under 20 characters. Turning on twenty
sources without a policy reproduces that mistake one level up, at the wrong
SOURCE MIX: the archive grows to millions of events of which most carry no
evidence you ever encountered them, and precision collapses again.

So every source declares three things:

    density    how much meaning one row carries (drives aggregation)
    tier       when it gets turned on (drives sequencing)
    bank       where its promoted facts route in Hindsight

The tiers are ordered by signal-per-unit-of-work, not by how interesting the
source sounds. Tier 1 is four sources and covers most of the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .adapters import Density


class Tier(IntEnum):
    """Rollout order. Ship a tier, measure, then open the next."""

    CORE = 1        # the archive is worth having with only these
    BEHAVIOUR = 2   # what you did, as opposed to what you said
    ARTIFACT = 3    # things you made, saved, or were sent
    AMBIENT = 4     # weak signal; on last, off first if precision drops


@dataclass(frozen=True)
class SourcePolicy:
    source: str
    density: Density
    tier: Tier
    bank: str | None            # Hindsight routing for promoted facts
    why: str
    caution: str | None = None


POLICIES: list[SourcePolicy] = [
    # ---------------- Tier 1 — CORE -----------------------------------------
    SourcePolicy(
        "telegram", Density.NARRATIVE, Tier.CORE, "personal",
        "681k messages over 7.6 years. The richest single channel by a wide "
        "margin and the only one with relationship depth.",
        "65% under 20 chars — must be segmented, never indexed per message.",
    ),
    SourcePolicy(
        "wakapi", Density.TELEMETRY, Tier.CORE, "projects",
        "Answers 'what was I working on' exactly, from data, with zero NLP. "
        "Cheapest high-value adapter in the set.",
        "SQLite, not Postgres. Heartbeats roll up in the adapter or they "
        "become 10^6 meaningless rows.",
    ),
    SourcePolicy(
        "dawarich", Density.TELEMETRY, Tier.CORE, "personal",
        "Where you were. The missing axis on every timeline — and behavioural "
        "signal you cannot curate, which makes it more honest than chat.",
        "Overlaps owntracks. Run ONE of them or every trip is double-counted "
        "and the duplicate reads as corroboration.",
    ),
    SourcePolicy(
        "calendar", Density.DISCRETE, Tier.CORE, "personal",
        "The structure of time, with names attached. Answers 'what was I "
        "doing that week' for periods where you said nothing in chat.",
    ),

    # ---------------- Tier 2 — BEHAVIOUR ------------------------------------
    SourcePolicy(
        "firefly", Density.DISCRETE, Tier.BEHAVIOUR, "finance",
        "Spending is uncurated behaviour, precisely dated. Often answers "
        "'what changed' better than anything that was said out loud.",
        "MariaDB, and amounts live on `transactions` (two signed rows per "
        "journal) — take the positive leg or you double-count.",
    ),
    SourcePolicy(
        "lastfm", Density.TELEMETRY, Tier.BEHAVIOUR, "personal",
        "Continuously recorded, second-resolution, nobody curates it. One of "
        "the few honest proxies for mood and daily rhythm.",
    ),
    SourcePolicy(
        "forgejo", Density.NARRATIVE, Tier.BEHAVIOUR, "projects",
        "Commit messages are deliberate text. Unlike wakapi (which knows you "
        "typed) this knows what you finished.",
    ),
    SourcePolicy(
        "jira", Density.DISCRETE, Tier.BEHAVIOUR, "employer",
        "Day-job ticket transitions are a precise work timeline.",
        "Routes to an `employer` bank, not `work` — day-to-day delivery is a "
        "different bank from career.",
    ),

    # ---------------- Tier 3 — ARTIFACT -------------------------------------
    SourcePolicy(
        "immich", Density.TELEMETRY, Tier.ARTIFACT, "personal",
        "Photos anchor segments visually and corroborate travel.",
        "Use EXIF dateTimeOriginal, NOT createdAt — a 2019 photo imported in "
        "2024 would otherwise land five years out. Burst shots must cluster.",
    ),
    SourcePolicy(
        "paperless", Density.DISCRETE, Tier.ARTIFACT, "personal",
        "Already OCR'd. Contracts and receipts are official life events, and "
        "one row here is genuinely one meaningful thing.",
    ),
    SourcePolicy(
        "gmail", Density.NARRATIVE, Tier.ARTIFACT, "personal",
        "Deliberate written communication outside Telegram.",
        "The channel most polluted by third-party automated mail. Filter in "
        "the query, not after ingest.",
    ),
    SourcePolicy(
        "notion", Density.NARRATIVE, Tier.ARTIFACT, "learning",
        "Thinking-out-loud, study notes, project docs.",
        "Pages are EDITED, so last_edited_time is not when the thought "
        "happened. Timeline uses created_time.",
    ),
    SourcePolicy(
        "karakeep", Density.DISCRETE, Tier.ARTIFACT, "learning",
        "A bookmark is deliberate, timestamped interest — much stronger "
        "signal than an RSS fetch.",
    ),
    SourcePolicy(
        "github", Density.NARRATIVE, Tier.ARTIFACT, "projects",
        "Public work; complements forgejo.",
    ),
    SourcePolicy(
        "linkedin", Density.DISCRETE, Tier.ARTIFACT, "social",
        "Posts plus engagement — the only source with an outcome metric "
        "attached to something you wrote.",
    ),
    SourcePolicy(
        "slack", Density.NARRATIVE, Tier.ARTIFACT, "employer",
        "Work conversation. Segments like Telegram.",
        "Contains colleagues' words. Keep local; never promote quotes.",
    ),

    # ---------------- Tier 4 — AMBIENT --------------------------------------
    SourcePolicy(
        "miniflux", Density.AMBIENT, Tier.AMBIENT, None,
        "Information diet — weak but real interest signal.",
        "ONLY read/starred entries. The unread firehose is exactly the noise "
        "that would drown the archive.",
    ),
    SourcePolicy(
        "nytka", Density.NARRATIVE, Tier.AMBIENT, None,
        "Speech around the owner, from a wearable: what was said in rooms the "
        "owner was in, with no chat to have written it down.",
        "Other people's words, several times Telegram's volume a day, and a transcriber's "
        "errors. No Hindsight bank: nothing from it is ever promoted. Last tier "
        "because it is the likeliest to crowd out the archive; see CLAUDE.md.",
    ),
    SourcePolicy(
        "owntracks", Density.TELEMETRY, Tier.AMBIENT, None,
        "Raw location, if you prefer it to dawarich.",
        "Mutually exclusive with dawarich — same underlying GPS.",
    ),
]

BY_SOURCE: dict[str, SourcePolicy] = {p.source: p for p in POLICIES}


#: Homelab stacks that carry NO life signal. Listed explicitly so "all
#: channels" has a documented boundary and nobody wires them in later by
#: mistake. These are infrastructure: they describe the machine, not the life.
NOT_SOURCES = {
    "adguard", "alerting", "apprise", "autoheal", "beszel", "changedetection",
    "cloudflared", "diun", "dozzle", "gatus", "glance", "goflow2", "headroom",
    "headscale", "homepage", "host", "komodo", "langfuse", "librechat",
    "lichylnyk", "litellm", "metamcp", "mikrotik", "mktxp", "monitoring",
    "netalertx", "ntfy", "pangolin", "pocket-id", "qdrant", "searxng",
    "skysend", "sonarqube", "tinyauth", "uptime-kuma", "vaultwarden",
    "watchtower", "website", "whisperx", "zerobyte", "n8n", "activepieces",
    "agent-runner", "tg-assistant", "khoj", "hindsight", "forgejo-runner",
    "nextcloud",   # file store, not an event stream — index via paperless
    "ghostfolio",  # positions, not events — read live for finance questions
    "mono-firefly",  # a sync script feeding firefly, not a source itself
}


def enabled(max_tier: Tier = Tier.CORE) -> list[SourcePolicy]:
    """Policies up to and including `max_tier`.

    Default is CORE deliberately. Open the next tier only after measuring that
    retrieval precision did not drop — adding sources is not monotonically
    good, and the point of the tiers is to make a regression attributable.
    """
    return [p for p in POLICIES if p.tier <= max_tier]


def conflicts(active: set[str]) -> list[str]:
    """Source pairs that must not both be on."""
    out = []
    if {"dawarich", "owntracks"} <= active:
        out.append("dawarich and owntracks read the same GPS signal — "
                   "enabling both double-counts every trip")
    return out
