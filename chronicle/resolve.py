"""Cross-script entity resolution for Russian / Ukrainian / Latin.

Two tiers, because one is not enough — see docs/ADR-002-entity-resolution.md.
Telegram sender_id is ground truth for people; this is for entities named
in text, where Graphiti-style BM25 resolution fails entirely across scripts.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
#  Cross-script entity keys. «Егор» / «Єгор» / "Yehor" / "Egor" are ONE person.
#
#  Graphiti's resolution pipeline is cosine -> BM25 -> LLM, and the BM25 stage
#  FAILS ENTIRELY across scripts — these strings share no lexical surface.
#
#  IMPORTANT, learned the hard way: a straight ISO-9 transliteration does NOT
#  solve this. It produces ehor / iehor / yehor / egor — four distinct keys for
#  one person, because RU г->h vs UK г->h/ґ->g, є->ie vs ye, and Latin-script
#  input passes through untransliterated. Exact key matching is not enough.
#
#  The fix is a COARSE PHONETIC key: transliterate, then collapse the
#  equivalence classes that actually differ across RU/UK/Latin conventions.
#  This over-merges (it is deliberately lossy) — treat a key collision as a
#  CANDIDATE for resolution, then confirm with embedding similarity and, only
#  as a last resort, an LLM. Block candidate generation by chat and time
#  window: any O(N^2) resolution dies between 10^4 and 10^5 items, and you
#  have 6.8x10^5.
#
#  For the 457 PEOPLE you skip all of this — Telegram's sender_id is ground
#  truth the literature does not have. This is for entities named in text.
# ---------------------------------------------------------------------------

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e",
    "є": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "і": "i", "ї": "i",
    "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e",
    "ю": "u", "я": "a",
})

# Applied in order. Each rule collapses a distinction that RU, UK and the
# various Latin romanizations disagree about.
_PHONETIC_RULES: list[tuple[str, str]] = [
    (r"ye|ie|je",  "e"),    # Yehor / Iehor / Jehor  -> ehor
    (r"yu|iu|ju",  "u"),
    (r"ya|ia|ja",  "a"),
    (r"kh",        "h"),    # Kharkiv / Harkiv
    (r"ts",        "c"),
    (r"shch|sch",  "sh"),
    (r"ck|q",      "k"),
    (r"^[hg]",     "g"),    # Hrushevskyi / Grushevskiy — leading г is h or g
    (r"[hg]",      "g"),
    (r"[yj]",      "i"),    # Yehor/Iehor, -skyi/-skii/-sky
    (r"w",         "v"),
    (r"(.)\1+",    r"\1"),  # collapse doubled letters
    (r"[aeiou]+$", ""),     # drop trailing vowels (case endings, diminutives)
]


def translit_key(name: str) -> str:
    """Coarse, deliberately lossy phonetic key for cross-script matching.

    >>> {translit_key(n) for n in ("Егор", "Єгор", "Yehor", "Egor", "Ehor")}
    {'egor'}
    """
    s = re.sub(r"[^a-z0-9]", "", name.lower().translate(_TRANSLIT))
    for pattern, repl in _PHONETIC_RULES:
        s = re.sub(pattern, repl, s)
    return s


def skeleton_key(name: str) -> str:
    """Consonant skeleton — a COARSER second blocking key.

    The phonetic key alone cannot merge Russian/Ukrainian cognates, because
    the languages differ by systematic vowel alternation, not by spelling
    convention:

        Киев / Kiev      -> kev      Київ  / Kyiv    -> kiv
        Харьков / Kharkov-> garkov   Харків/ Kharkiv -> garkiv

    That o/i and e/i alternation is morphophonology. No transliteration table
    fixes it. Dropping vowels does:

        kev, kiv       -> kv
        garkov, garkiv -> grkv

    This DELIBERATELY over-merges (Дина/Дон both -> dn). It is a candidate
    GENERATOR for the resolution pipeline, never a decision. Confirm with
    embedding similarity, then an LLM only if still ambiguous.
    """
    return re.sub(r"[aeiou]", "", translit_key(name))


def resolution_candidates(
    name: str,
    phonetic_index: dict[str, list[int]],
    skeleton_index: dict[str, list[int]] | None = None,
    chat_id: int | None = None,
    chat_scope: dict[int, set[int]] | None = None,
) -> list[tuple[int, str]]:
    """Entity IDs that MIGHT be this name, with the tier that matched.

    Two tiers, most confident first:
        "phonetic"  exact coarse-phonetic match  — usually correct
        "skeleton"  consonant-skeleton match     — needs confirmation

    Blocking by chat is not an optimization, it is a correctness requirement.
    A-MEM's 15-hour build at LoCoMo scale is what unblocked O(N^2) resolution
    looks like, and that corpus is ~900x smaller than yours. Any algorithm
    comparing each new item against all existing ones dies between 10^4 and
    10^5 items; you have 6.8x10^5.
    """
    seen: set[int] = set()
    out: list[tuple[int, str]] = []

    skel = skeleton_key(name)
    tiers = [("phonetic", phonetic_index, translit_key(name))]

    # Guard: short names produce degenerate skeletons that collide with
    # everything ("Аня" -> "n"). A 1-consonant skeleton carries no
    # information, so fall back to the phonetic tier alone.
    # The threshold is 2, not 3: "Київ"/"Киев" -> "kv" is precisely the
    # RU/UK cognate case this tier exists to catch.
    if len(skel) >= 2:
        tiers.append(("skeleton", skeleton_index or {}, skel))

    for tier, index, key in tiers:
        for eid in index.get(key, []):
            if eid not in seen:
                seen.add(eid)
                out.append((eid, tier))

    if chat_id is not None and chat_scope is not None:
        allowed = chat_scope.get(chat_id, set())
        out = [(eid, tier) for eid, tier in out if eid in allowed]
    return out


