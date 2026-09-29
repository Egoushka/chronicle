---
title: "Measure it against grep"
description: "Score chronicle against ripgrep on your own labelled questions: how the harness scores, how to write the questions, and how to run it."
order: 4
section: "Guides"
---

The project's rule is that chronicle has to beat `grep` on its owner's own questions before it grows ([ROADMAP.md](../../ROADMAP.md)). [evaluate.py](../../chronicle/evaluate.py) measures that: it asks chronicle and ripgrep the same questions and scores both on the evidence they return, never with an LLM as judge.

## What it scores

Every question carries its gold evidence: the `source:source_id` ids of the events that answer it, checked by hand. Each system answers with ranked groups in the unit it retrieves, one event per group for ripgrep and one segment per group for chronicle.

- **recall**: the share of a question's gold ids found anywhere in the answer.
- **p@1**: whether the top-ranked group holds a gold id. For chronicle that is the top segment, not its first message ([CLAUDE.md](../../CLAUDE.md#hard-won-facts--do-not-relearn-these), fact 49).
- **Budget**: each side returns at most 200 event ids per question (`BUDGET`). A segment holds about 13 events, so without the cap chronicle would read far more text than grep.
- **Unlabelled questions** are skipped, not scored as zero.

Results are reported per question kind, plus an overall recall across every labelled question. [test_evaluate.py](../../tests/test_evaluate.py) covers the scoring rules.

| Kind | Chronicle calls | Ripgrep gets |
|---|---|---|
| `lookup` | `/recall`, top 20 segments | the keywords |
| `first_mention` | `/first-mention` per keyword, oldest first | the keywords |
| `evolution` | `/evolution`, each period's best hit first | the keywords |
| `aggregate` | `/recall`, top 20 segments | the keywords |

Ripgrep runs `rg -i -m 200` with the question's keywords joined by `|`, or the whole question when it has none, over a dump with one line per event.

## Write the questions

```bash title="Write the question template"
make eval-init
```

This writes `eval/questions.json` with four template questions; `CHRONICLE_EVAL` points it at another path. Git ignores the file: it holds real questions about a real archive. Replace the templates with 30 to 50 real questions. With fewer than 10 labelled questions the harness warns that below about 30 the numbers are noise.

```json title="eval/questions.json, one entry"
[
  {
    "question": "When did I first mention Kubernetes?",
    "kind": "first_mention",
    "evidence": ["telegram:CHAT:MSGID"],
    "keywords": ["kubernetes", "k8s"],
    "note": null
  }
]
```

| Field | What goes in it |
|---|---|
| `question` | the question as you would ask it |
| `kind` | `lookup`, `first_mention`, `evolution` or `aggregate` |
| `evidence` | the event ids that answer it, as `source:source_id` |
| `keywords` | ripgrep's search terms: the question's own words |
| `note` | anything, optional |

Two rules keep the set honest:

- **Write the question from memory, before you look anything up.** A question written after reading the answer is one you already know is findable ([README](../../README.md#measure-it)).
- **Keywords hold only the question's words**: stems, other spellings, the Russian or Ukrainian form of the same word, never a word from the answer. Chronicle gets only the question, so grep gets only its words ([`Question.keywords`](../../chronicle/evaluate.py), [CHANGELOG 0.3.0](../../CHANGELOG.md#030---2026-09-29)).

Then find the evidence ids. [eval-candidates.sh](../../scripts/eval-candidates.sh) searches segments by regular expression over SSH and prints each hit as a question block with its evidence ids filled in. It searches with SQL, not `/recall`, so the benchmark is not chosen by the path it measures. It puts your search pattern into `keywords`; replace that with the question's own words, since you picked the pattern because it appears in the answer. On the host itself, the same search is:

```bash title="Find candidate segments and their evidence ids"
docker compose exec chronicle-db psql -U chronicle -d chronicle -c \
  "SELECT started_at::date, array_to_string(source_event_ids, ','), left(raw_text, 200)
     FROM segment WHERE is_substantive AND raw_text ~* 'your pattern'
    ORDER BY started_at LIMIT 10"
```

A Telegram evidence id reads `telegram:<chat_id>:<message_id>`.

## Run it where the archive lives

Ripgrep's side reads a dump of every event as plain text, so run the comparison on the host that holds the archive.

[eval-homelab.sh](../../scripts/eval-homelab.sh) does it in one go. On the host that runs the stack, from the checkout:

```bash title="Run the comparison on the host"
./scripts/eval-homelab.sh
```

It writes the dump with `COPY` inside `chronicle-db` into a temporary directory, runs a throwaway Python container on `chronicle_default` with ripgrep and `httpx` installed, calls `python -m chronicle.evaluate compare` against `http://chronicle-api:8030`, and deletes the dump on exit. It reads `eval/questions.json` from the checkout. From another machine, `make eval-homelab` runs the same script over SSH; it needs `CHRONICLE_DOCTOR_HOST` and `CHRONICLE_STACKS`.

To run `make eval` yourself instead, you need `rg` and Python with `httpx` on the host. It reads `eval/dump.tsv` and the api at `http://localhost:8030`. Write the dump as the script does, one line per event, `source:source_id`, a tab, the timestamp, a tab, the text with its own newlines and tabs flattened:

```bash title="Dump, compare, delete the dump"
docker exec chronicle-db psql -U chronicle -d chronicle -Atc \
  "COPY (SELECT source||':'||source_id, ts, replace(replace(text, E'\\n', ' '), E'\\t', ' ') FROM event ORDER BY ts) TO STDOUT" > eval/dump.tsv
make eval
rm eval/dump.tsv
```

> [!WARNING]
> `eval/dump.tsv` is the whole archive in plain text, and `.gitignore` covers `eval/questions.json` but not the dump. Delete it after the run and never commit it.

## Read the report

`compare` prints two tables, `ripgrep baseline` and `chronicle`. Each has a row per kind with `n` (labelled questions), `recall` and `p@1`, and an `OVERALL` recall row. Then it prints `delta:`, chronicle's overall recall minus ripgrep's. Below +10% it adds that chronicle is not clearly beating grep and that segmentation comes before features.

An error status from the api on one question counts as a miss for chronicle rather than stopping the run, so one broken endpoint cannot hide the rest.

The runs recorded for the reference deployment are in [CHANGELOG.md](../../CHANGELOG.md) and under goal 4 of [ROADMAP.md](../../ROADMAP.md#stage-0--worth-using-over-grep-for-its-owner-v0x).

## Keep it honest

- Measure before and after every change that could move retrieval: a new tier, enrichment, a segment cap. Use the same questions both times.
- Judge each system in the unit it retrieves, and give both the same budget. That is what `score` and `BUDGET` do; do not change one side alone.
- `make eval-threads` prints the threads your gold evidence lives in, which is the scope for a segment-cap sweep ([Deploy and operate](operate.md#rebuild-threads-at-a-new-segment-cap)).
