"""Evaluation — the number that decides whether any of this earned its keep.

Letta scored 74.0% on LoCoMo using nothing but a filesystem and `grep`,
beating Mem0's 68.5%. On Mem0's own benchmark table the full-context baseline
(72.90) beats Mem0 (66.88) and Mem0-graph (68.44). The lesson generalizes:
**a memory system that cannot beat ripgrep on your own questions is not
earning its complexity.**

So this module measures both, on the same questions, and reports the delta.

Deliberately NOT LLM-as-judge. The LoCoMo audit measured gpt-4o-mini accepting
62.81% of deliberately wrong-but-topical answers — that metric rewards vague
answers that name the right topic, which is precisely the failure mode you
cannot tolerate over a personal archive. Evidence recall against known
source_event_ids is harder to game.

    python -m chronicle.evaluate init      # write a question template
    python -m chronicle.evaluate threads   # thread keys the gold lives in
    python -m chronicle.evaluate grep      # baseline over a text dump
    python -m chronicle.evaluate chronicle # the real pipeline
    python -m chronicle.evaluate compare   # both, side by side
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

QUESTIONS_PATH = Path(os.environ.get("CHRONICLE_EVAL", "eval/questions.json"))


@dataclass
class Question:
    """One question with known-good evidence.

    `evidence` is a list of `source:source_id` strings you verified by hand.
    Building 30-50 of these is a couple of hours and it is the only ground
    truth you will ever have — there is no multilingual memory benchmark and
    no ukMTEB.
    """

    question: str
    kind: str                      # lookup | first_mention | aggregate | evolution
    evidence: list[str] = field(default_factory=list)
    # For the grep baseline: the question's own words only — stems, other
    # spellings, the RU/UK form of the same word. Never a word from the
    # answer: chronicle is given only the question, and a keyword lifted from
    # the gold ("which street?" -> the street's name) grades grep on having
    # already found it. On 71 questions that alone was worth 14 points.
    keywords: list[str] = field(default_factory=list)
    note: str | None = None


TEMPLATE = [
    Question("Что мы решили насчёт квартиры?", "lookup",
             evidence=["telegram:CHAT:MSGID"], keywords=["квартир"],
             note="replace with real ids — grep for the message, take its id"),
    Question("When did I first mention Kubernetes?", "first_mention",
             evidence=[], keywords=["kubernetes", "k8s", "к8с"]),
    Question("Как менялось моё мнение об инвестициях?", "evolution",
             evidence=[], keywords=["инвестиц", "етф", "etf"]),
    Question("Сколько раз я писал Ане в 2022?", "aggregate", evidence=[]),
]


@dataclass
class Result:
    kind: str
    n: int = 0
    recall_sum: float = 0.0
    hit_at_1: int = 0

    @property
    def recall(self) -> float:
        return self.recall_sum / self.n if self.n else 0.0

    @property
    def p_at_1(self) -> float:
        return self.hit_at_1 / self.n if self.n else 0.0


def load() -> list[Question]:
    if not QUESTIONS_PATH.exists():
        raise SystemExit(f"{QUESTIONS_PATH} not found — run `evaluate init` first")
    return [Question(**q) for q in json.loads(QUESTIONS_PATH.read_text())]


Groups = list[list[str]]


def score(questions: list[Question], answer: Callable[[Question], Groups]
          ) -> dict[str, Result]:
    """Evidence recall per question kind.

    `answer` returns RANKED GROUPS, each in the unit its system retrieves:
    one event per group for ripgrep, one SEGMENT per group for chronicle.

    recall = |retrieved ∩ gold| / |gold| over the flattened ids.
    p@1 = the top-ranked GROUP contains gold. Flattened, p@1 asked whether
    the first event of chronicle's top segment was the gold one — false for
    almost every ~13-event segment even when that segment ranked first and
    held the answer. Measured on 17 lookups: 11.8% flattened vs 29.4% by
    segment, with retrieval unchanged. Judge a thing in the unit it is built
    to serve (hard-won fact 28, one level up).

    Questions with no gold evidence are skipped rather than counted as 0 —
    an unlabeled question measures nothing.
    """
    out: dict[str, Result] = {}
    for qn in questions:
        if not qn.evidence:
            continue
        groups = answer(qn)
        gold = set(qn.evidence)
        r = out.setdefault(qn.kind, Result(qn.kind))
        r.n += 1
        found = len({e for g in groups for e in g} & gold) / len(gold)
        r.recall_sum += found
        if not found:
            log.info("miss [%s] %s", qn.kind, qn.question)
        if groups and set(groups[0]) & gold:
            r.hit_at_1 += 1
    return out


def _within_budget(groups: Groups, budget: int) -> Groups:
    """Ranked groups, cut so the flattened ids total at most `budget`."""
    out: Groups = []
    left = budget
    for g in groups:
        if left <= 0:
            break
        out.append(g[:left])
        left -= len(out[-1])
    return out


# ---------------------------------------------------------------------------
#  baseline: ripgrep over a flat dump
# ---------------------------------------------------------------------------

#: Event ids each side may return per question. Equal on purpose: chronicle
#: answers with whole segments (~13 events each), so 20 segments flattened is
#: ~260 ids against grep's 20 lines — a 13x reading budget that makes recall
#: incomparable. 200 is about 15 segments, or 200 grep hits; p@1 is unaffected.
BUDGET = 200


def grep_answerer(dump: Path, limit: int = BUDGET) -> Callable[[Question], Groups]:
    """The bar Chronicle has to clear.

    The dump is one line per event: `source:source_id\\tISO_TS\\ttext`.
    Produce it with:
        psql -c "COPY (SELECT source||':'||source_id, ts, text FROM event
                       ORDER BY ts) TO STDOUT" > eval/dump.tsv
    """
    def answer(qn: Question) -> Groups:
        terms = qn.keywords or [qn.question]
        pattern = "|".join(re.escape(t) for t in terms)
        try:
            proc = subprocess.run(
                ["rg", "-i", "-m", str(limit), pattern, str(dump)],
                capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise SystemExit("ripgrep (rg) not installed — that IS the baseline")
        ids = [ln.split("\t", 1)[0] for ln in proc.stdout.splitlines() if "\t" in ln]
        # One event per group: ripgrep's unit is the matching line.
        return [[i] for i in ids[:limit]]
    return answer


def chronicle_answerer(base_url: str, enrich: bool = False,
                       rerank: bool = False) -> Callable[[Question], Groups]:
    import httpx
    client = httpx.Client(base_url=base_url, timeout=120.0)
    #: Which source holds each /recall result, over every lookup question. A
    #: new source that outnumbers the gold's can lower recall without ever
    #: being wrong, by taking slots; this is the number that shows it.
    slots: Counter = Counter()

    def answer(qn: Question) -> Groups:
        # An api error is a miss, not a crash: the question still counts
        # against chronicle, and one bad endpoint cannot hide the rest.
        try:
            return _answer(qn)
        except httpx.HTTPStatusError as exc:
            log.warning("chronicle failed %r: %s", qn.question, exc)
            return []

    def _answer(qn: Question) -> Groups:
        if qn.kind == "first_mention":
            # The TERM, as the MCP tool is called — not the question. Sent the
            # whole sentence, the api lemmatised every word into a pattern
            # ("when", "did", "i", ...) and matched nearly any message. Each
            # spelling the grep baseline gets, merged earliest-first.
            cands = []
            for term in qn.keywords or [qn.question]:
                r = client.post("/first-mention", json={"term": term})
                r.raise_for_status()
                cands += r.json()["candidates"]
            cands.sort(key=lambda c: c["date"])
            # /first-mention answers from `event`, so its unit is the event.
            return [[f"{c['source']}:{c['source_id']}"] for c in cands][:BUDGET]
        if qn.kind == "evolution":
            # The endpoint the MCP tells an agent to use for these. Routed to
            # /recall, the harness measured top-k similarity DENSITY — the
            # failure /evolution exists to fix — and stratified_search was
            # never measured at all. Round-robin by rank within bin, so the
            # budget cut keeps every period's best hit before any second-best.
            r = client.post("/evolution", json={"topic": qn.question})
            r.raise_for_status()
            bins = list(r.json()["bins"].values())
            ranked = [b[i]["evidence"] for i in range(max(map(len, bins), default=0))
                      for b in bins if i < len(b)]
            return _within_budget(ranked, BUDGET)
        r = client.post("/recall", json={"query": qn.question, "limit": 20,
                                        "enrich": enrich, "rerank": rerank})
        r.raise_for_status()
        hits = [hit["evidence"] for hit in r.json()["results"]]
        slots.update(g[0].split(":")[0] for g in hits if g)
        # One SEGMENT per group, in rank order. See `score`.
        return _within_budget(hits, BUDGET)
    answer.slots = slots
    return answer


def report(title: str, res: dict[str, Result]) -> None:
    print(f"\n  {title}")
    print(f"  {'kind':<15} {'n':>3}  {'recall':>7}  {'p@1':>6}")
    for kind, r in sorted(res.items()):
        print(f"  {kind:<15} {r.n:>3}  {r.recall:>7.1%}  {r.p_at_1:>6.1%}")
    tot_n = sum(r.n for r in res.values())
    tot_r = sum(r.recall_sum for r in res.values()) / tot_n if tot_n else 0
    print(f"  {'OVERALL':<15} {tot_n:>3}  {tot_r:>7.1%}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chronicle.evaluate")
    ap.add_argument("command",
                    choices=["init", "threads", "grep", "chronicle", "compare"])
    ap.add_argument("--dump", default="eval/dump.tsv")
    ap.add_argument("--api", default="http://localhost:8030")
    ap.add_argument("--enrich", action="store_true",
                    help="fuse the enrichment list into /recall (goal 7 A/B)")
    ap.add_argument("--rerank", action="store_true",
                    help="have a chat model reorder /recall's pool (rerank.py)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    if args.command == "init":
        QUESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        QUESTIONS_PATH.write_text(
            json.dumps([asdict(q) for q in TEMPLATE], ensure_ascii=False, indent=2))
        print(f"wrote {QUESTIONS_PATH}")
        print("Now write 30-50 REAL questions with verified evidence ids.\n"
              "This is a couple of hours and it is the only ground truth you\n"
              "will ever have — there is no multilingual memory benchmark.")
        return 0

    if args.command == "threads":
        # The threads the gold lives in — the scope a `worker resegment`
        # sweep should rebuild, instead of every thread and ~10 h of
        # re-embedding. An evidence id is `{source}:{source_id}`; telegram's
        # source_id is `{chat}:{msg}`, so the first two fields are the thread
        # key (fact 32). A source whose source_id has no colon would need
        # its adapter's own rule.
        print(" ".join(sorted({":".join(e.split(":")[:2])
                               for q in load() for e in q.evidence})))
        return 0

    qs = load()
    labeled = sum(1 for q in qs if q.evidence)
    if labeled < 10:
        print(f"  ! only {labeled} questions have gold evidence. "
              "Below ~30 the numbers are noise.")

    if args.command in ("grep", "compare"):
        res_grep = score(qs, grep_answerer(Path(args.dump)))
        report("ripgrep baseline", res_grep)
    if args.command in ("chronicle", "compare"):
        answerer = chronicle_answerer(args.api, args.enrich, args.rerank)
        res_chr = score(qs, answerer)
        report("chronicle", res_chr)
        total = sum(answerer.slots.values())
        if total:
            print("  /recall results by source: " + ", ".join(
                f"{s} {n} ({n / total:.1%})" for s, n in answerer.slots.most_common()))

    if args.command == "compare":
        tot = lambda r: (sum(x.recall_sum for x in r.values())      # noqa: E731
                         / max(1, sum(x.n for x in r.values())))
        delta = tot(res_chr) - tot(res_grep)
        print(f"\n  delta: {delta:+.1%}")
        if delta < 0.10:
            print("  Chronicle is not clearly beating grep. Fix segmentation\n"
                  "  before adding features — everything downstream inherits it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
