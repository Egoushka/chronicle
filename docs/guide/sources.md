---
title: "Connect a source"
description: "What each of the 18 adapters reads and emits, the variables it needs, how doctor checks it, and when to open the next tier."
order: 2
section: "Guides"
---

Chronicle has 18 adapters, one per source, each registered with a policy in [sources.py](../../chronicle/sources.py). Eleven of them can be switched on from `.env`. The other seven need code you write first ([Sources you cannot connect yet](#sources-you-cannot-connect-yet)).

## How a source is wired

A source takes three things, and then a check:

1. **Variables in `.env`.** `doctor` and the worker build each adapter from the environment in [`doctor.build`](../../chronicle/doctor.py). A source whose variables are missing or incomplete is skipped, never failed.
2. **Reachability from `chronicle-worker`.** In `compose.override.yaml`, bind-mount a SQLite file's directory, or attach the network of the stack whose database you read. [compose.sources.example.yaml](../../compose.sources.example.yaml) shows both for a host where every source runs as its own Compose stack beside chronicle. The worker is the only service that reaches sources; the api mounts none.
3. **A tier.** `--tier N` includes every source of tier N and below. The worker and `doctor` default to tier 1; [nightly.sh](../../scripts/nightly.sh) defaults to tier 2.
4. **Then `doctor`, then `ingest`**, both at that tier.

| Tier | Set up from `.env` | Needs a page fetcher first |
|---|---|---|
| 1, core | `telegram`, `wakapi`, `dawarich` | `calendar` |
| 2, behaviour | `firefly`, `lastfm`, `forgejo` | `jira` |
| 3, artifact | `immich`, `paperless`, `karakeep` | `gmail`, `notion`, `github`, `linkedin`, `slack` |
| 4, ambient | `miniflux`, `owntracks` | none |

Each source also has a density, which decides how its events are aggregated and whether they are segmented ([Source policy](how-it-works.md#source-policy)). The notes under each table name it.

> [!WARNING]
> `event` has one partition per year from 2018 and none before ([001_core.sql](../../migrations/001_core.sql)). PostgreSQL rejects an event dated before 2018-01-01, so the batch that holds it fails, `source.last_error` records the error (`/stats` shows it), and every later run retries the same batch. A photo library or a document archive that reaches back before 2018 stops at its first batch.

## Tier 1: core

| Source | Reads | One event per | Variables |
|---|---|---|---|
| `telegram` | SQLite: `messages`, `chats`, `chat_tags` | message | `TELEGRAM_DB_URL` |
| `wakapi` | SQLite: `heartbeats` | coding session | `WAKAPI_DB_PATH`, `WAKAPI_USER` |
| `dawarich` | PostGIS: `points` | stay of 20 minutes or more | `DAWARICH_DB_URL`, `DAWARICH_USER_ID` |
| `calendar` | a page fetcher you supply | calendar event | none yet |

**telegram** (narrative) reads the SQLite database of a Telegram sync service, opened read-only ([telegram.py](../../chronicle/adapters/telegram.py)). The thread key is `telegram:<chat_id>`, so every chat is segmented on its own.

- It reads only chats whose `chat_type` is `user`. That filter, `personal_only`, is not settable from `.env`.
- Bot chats stay out by default (`TELEGRAM_EXCLUDE_BOT_CHATS=1`): a chat whose username ends in `bot`, a chat the sync service tagged `chat:bot`, and Telegram's service account, `777000`. Neither bot signal alone catches every bot chat, so the adapter uses both. Set the variable to `0` to let bots in.
- `TELEGRAM_EXCLUDE_CHAT_IDS` takes more chat ids to leave out, separated by commas or spaces.
- It resumes on `synced_at`, the time the sync service wrote the row, and re-reads a 10-minute overlap. When the sync service rewrites a row, as it does when a voice-note transcript or an edit lands weeks later, the new text still reaches chronicle; the message's own date stays the event time.
- Outgoing messages carry the actor `me`.
- Mount the database's directory read-write: the file is in WAL mode, which cannot open through a `:ro` mount.

**wakapi** (telemetry) reads `heartbeats` for one user and rolls them into coding sessions ([wakapi.py](../../chronicle/adapters/wakapi.py)). `WAKAPI_USER` is `users.id`, the login, not the email. Each project keeps its own session, and a 15-minute pause in that project closes it. An event reads `coded on <project> for N min (<languages>)`, with the thread key `wakapi:<project>`. The adapter reads `time` stored either as text or as epoch milliseconds.

**dawarich** (telemetry) reads GPS points from the dawarich 1.8.x schema and emits stays ([dawarich.py](../../chronicle/adapters/dawarich.py)). It clusters points within about 150 m with PostGIS, cuts each cluster into consecutive visits in time, and keeps visits of 20 minutes or more: `stayed at <lat>,<lon> for N min`, thread key `dawarich:stays`. It emits no trips and no place names. `DAWARICH_USER_ID` is `users.id`; `.env.example` warns that the first account is rarely the one carrying the track. Run dawarich or owntracks, not both.

**calendar** is listed under [Sources you cannot connect yet](#sources-you-cannot-connect-yet).

## Tier 2: behaviour

| Source | Reads | One event per | Variables |
|---|---|---|---|
| `firefly` | MariaDB: journals and transactions | transaction | `FIREFLY_DB_URL` |
| `lastfm` | REST: `user.getrecenttracks` | listening session | `LASTFM_API_KEY`, `LASTFM_USER` |
| `forgejo` | PostgreSQL: `action` | repository activity | `FORGEJO_DB_URL`, `FORGEJO_EMAIL` |
| `jira` | a page fetcher you supply | ticket | none yet |

**firefly** (discrete) reads Firefly III's MariaDB with a `mysql://user:pass@host:3306/db` DSN ([firefly.py](../../chronicle/adapters/firefly.py)). Firefly stores every journal as two signed transactions; the adapter takes the positive one, so no journal counts twice, and skips soft-deleted journals and transactions. An event reads `<type>: <description> — <amount> <currency>`, with the thread key `firefly:<category>`.

**lastfm** (telemetry) calls the Last.fm REST API directly with a read key ([api_sources.py](../../chronicle/adapters/api_sources.py)). Scrobbles more than 30 minutes apart start a new session, and a session needs at least 3 tracks: `listened to N tracks, mostly <artist>`. It fetches 90-day windows, 200 tracks a page, a quarter second apart, and skips the track that is still playing. The key may instead live in a shared secrets file that `INGEST_SECRETS_FILE` names; `nightly.sh` reads it there.

**forgejo** (narrative) reads the `action` feed of a Forgejo database ([forgejo.py](../../chronicle/adapters/forgejo.py)). An event's text is the repository name in brackets and then, for a push, the commit messages pulled out of the action's JSON; for another action, its text (an issue title, a comment), or its name when it has none. `FORGEJO_EMAIL` keeps one author's actions. The thread key is `forgejo:<repo>`, and actions are segmented like chat messages.

## Tier 3: artifact

| Source | Reads | One event per | Variables |
|---|---|---|---|
| `immich` | PostgreSQL: `asset`, `asset_exif` | photo session | `IMMICH_DB_URL`, `IMMICH_OWNER_ID` |
| `paperless` | PostgreSQL: `documents_document` | document | `PAPERLESS_DB_URL` |
| `karakeep` | SQLite: `bookmarks` and their bodies | bookmark | `KARAKEEP_DB_PATH` |

**immich** (telemetry) groups photos into sessions split by 3-hour gaps ([immich.py](../../chronicle/adapters/immich.py)). Capture time comes from EXIF `dateTimeOriginal`, falling back to `fileCreatedAt`, never the upload time. It skips deleted assets, hidden ones (the video half of a live photo) and the locked folder, and reads the table names of immich v2. An event reads `took N photos at <places>`. `IMMICH_OWNER_ID` is optional and limits it to one owner.

**paperless** (discrete) emits one event per document at the document's own `created` date, not its scan date, with the title and the first 4,000 characters of the OCR text ([paperless.py](../../chronicle/adapters/paperless.py)).

**karakeep** (discrete) reads link and text bookmarks from Karakeep's SQLite file, with any note attached ([karakeep.py](../../chronicle/adapters/karakeep.py)). An event holds the title, description, body and note, or the URL when all of those are empty.

`gmail`, `notion`, `github`, `linkedin` and `slack` are listed under [Sources you cannot connect yet](#sources-you-cannot-connect-yet).

## Tier 4: ambient

| Source | Reads | One event per | Variables |
|---|---|---|---|
| `miniflux` | PostgreSQL: `entries`, `feeds` | article read or starred | `MINIFLUX_DB_URL` |
| `owntracks` | recorder files, `rec/**/*.rec` | stay of 20 minutes or more | `OWNTRACKS_STORE` |

**miniflux** (ambient) reads only entries you read or starred: the unread firehose carries no evidence you saw it ([miniflux.py](../../chronicle/adapters/miniflux.py)). The event time is the article's publication time.

**owntracks** (telemetry) reads the recorder's flat JSONL files under `OWNTRACKS_STORE` and emits stays where consecutive points stay within about 150 m for 20 minutes or more ([owntracks.py](../../chronicle/adapters/owntracks.py)). It reads the same GPS signal as dawarich, so every trip would count twice; `doctor` reports a conflict and exits 1 when both are configured.

## Sources you cannot connect yet

`calendar`, `jira`, `gmail`, `notion`, `github`, `linkedin` and `slack` are written as adapters over an injected `fetch_page` callable, so they never hold a credential ([api_sources.py](../../chronicle/adapters/api_sources.py)). Nothing in the repository supplies that callable. `doctor.build` constructs only `lastfm` among the API adapters, so `doctor` reports the other seven as not configured and `ingest` skips them. Connecting one means writing its page fetcher and adding it to `doctor.build`.

Stage 1 of [ROADMAP.md](../../ROADMAP.md#stage-1--someone-else-can-run-it-v1x) plans the calendar source, with OAuth, end to end.

## What doctor checks

`doctor` reads up to 25 events from the last 90 days of each configured source, or from its whole history when the last 90 days are empty, and checks them without writing anything ([doctor.py](../../chronicle/doctor.py)):

- timestamps are datetimes, not the text SQLite hands back;
- events arrive in ascending order of their resume point, or a resumed run would skip rows;
- `immich`, `paperless` and `karakeep` events do not all fall within one hour, the sign of import time posing as event time;
- no `source_id` repeats;
- telemetry arrives rolled up (a median gap under 60 seconds means raw points);
- narrative events have text, and a thread key other than `default`;
- no rolled-up event spans more than 3 days.

A source that answers but yields no events is a warning. So is one with nothing in the last 90 days, which on the reference deployment was the only sign that the phone had stopped reporting location ([CLAUDE.md](../../CLAUDE.md#hard-won-facts--do-not-relearn-these), fact 41). A source that cannot be built or read is a failure, with a hint for the common driver errors.

`doctor` exits 1 on any failure or on a source conflict. `python -m chronicle.doctor --json` prints the report as JSON. [test_adapters.py](../../tests/test_adapters.py) tests every check except the one for empty narrative text.

Stack-private databases publish no port, so `doctor` has to run where the sources are. [doctor-homelab.sh](../../scripts/doctor-homelab.sh) runs it over SSH in a throwaway container attached to each source stack's network, with passwords read from each stack's own `.env` into a mode-600 env file, never onto a command line. It is written for the sibling-stacks layout and needs `CHRONICLE_DOCTOR_HOST` and `CHRONICLE_STACKS`.

## Open the next tier

Adding sources does not make retrieval better by itself. [sources.py](../../chronicle/sources.py) opens the tiers one at a time so a drop in precision can be traced to the tier that caused it. Measure before and after ([Measure it against grep](evaluate.md)):

```bash title="Validate and ingest tier 2"
docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker doctor --tier 2
docker compose --profile batch run --rm chronicle-worker python -m chronicle.worker all --tier 2
```

A source that was never ingested starts from its oldest row, so the first run of a new tier reads its whole history.
