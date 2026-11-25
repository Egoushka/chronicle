# Lifeline: spec

One question across oura, wakapi, telegram and mail, as chronicle sources plus a tool. No new store.

- Surface: `POST /lifeline/ask {question, since?, until?}` and MCP `lifeline_ask`. Returns `{answer, citations[{source, event_id, ts, snippet}]}`.
- Contract (tests/test_lifeline.py): at least one citation; every cited id was retrieved; invented ids and empty citations are errors (422); at most two model calls (plan, answer).
- Window: explicit `since/until`, else "yesterday", "last week" (previous Mon-Sun), "this week", "last month", "this month", "last N days", else the last 30 days, in `LIFELINE_TZ`.
- Sources: keyword hints pick them; none matched reads all four.
- Acceptance on the live box: kiwi's `docs/specs/lifeline-questions.json` via `scripts/lifeline-check.mjs`: each answer cites at least 2 distinct sources and every `event_id` resolves at `GET /event/{id}`.
