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
    keywords: list[str] = field(default_factory=list)   # for the grep baseline
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


def score(questions: list[Question], answer: Callable[[Question], list[str]]
          ) -> dict[str, Result]:
    """Evidence recall per question kind.

    recall = |retrieved ∩ gold| / |gold|.  p@1 = gold item in first position.
    Questions with no gold evidence are skipped rather than counted as 0 —
    an unlabeled question measures nothing.
    """
    out: dict[str, Result] = {}
    for qn in questions:
        if not qn.evidence:
            continue
        got = answer(qn)
        gold = set(qn.evidence)
        r = out.setdefault(qn.kind, Result(qn.kind))
        r.n += 1
        r.recall_sum += len(set(got) & gold) / len(gold)
        if got and got[0] in gold:
            r.hit_at_1 += 1
    return out


# ---------------------------------------------------------------------------
#  baseline: ripgrep over a flat dump
# ---------------------------------------------------------------------------

#: Event ids each side may return per question. Equal on purpose: chronicle
#: answers with whole segments (~13 events each), so 20 segments flattened is
#: ~260 ids against grep's 20 lines — a 13x reading budget that makes recall
#: incomparable. 200 is about 15 segments, or 200 grep hits; p@1 is unaffected.
BUDGET = 200


def grep_answerer(dump: Path, limit: int = BUDGET) -> Callable[[Question], list[str]]:
    """The bar Chronicle has to clear.

    The dump is one line per event: `source:source_id\\tISO_TS\\ttext`.
    Produce it with:
        psql -c "COPY (SELECT source||':'||source_id, ts, text FROM event
                       ORDER BY ts) TO STDOUT" > eval/dump.tsv
    """
    def answer(qn: Question) -> list[str]:
        terms = qn.keywords or [qn.question]
        pattern = "|".join(re.escape(t) for t in terms)
        try:
            proc = subprocess.run(
                ["rg", "-i", "-m", str(limit), pattern, str(dump)],
                capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise SystemExit("ripgrep (rg) not installed — that IS the baseline")
        ids = [ln.split("\t", 1)[0] for ln in proc.stdout.splitlines() if "\t" in ln]
        return ids[:limit]
    return answer


def chronicle_answerer(base_url: str) -> Callable[[Question], list[str]]:
    import httpx
    client = httpx.Client(base_url=base_url, timeout=120.0)

    def answer(qn: Question) -> list[str]:
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
            return [f"{c['source']}:{c['source_id']}" for c in cands][:BUDGET]
        r = client.post("/recall", json={"query": qn.question, "limit": 20})
        r.raise_for_status()
        out: list[str] = []
        for hit in r.json()["results"]:
            out.extend(hit["evidence"])
        return out[:BUDGET]
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
    ap.add_argument("command", choices=["init", "grep", "chronicle", "compare"])
    ap.add_argument("--dump", default="eval/dump.tsv")
    ap.add_argument("--api", default="http://localhost:8030")
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

    qs = load()
    labeled = sum(1 for q in qs if q.evidence)
    if labeled < 10:
        print(f"  ! only {labeled} questions have gold evidence. "
              "Below ~30 the numbers are noise.")

    if args.command in ("grep", "compare"):
        res_grep = score(qs, grep_answerer(Path(args.dump)))
        report("ripgrep baseline", res_grep)
    if args.command in ("chronicle", "compare"):
        res_chr = score(qs, chronicle_answerer(args.api))
        report("chronicle", res_chr)

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
