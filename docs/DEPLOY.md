# Deploying chronicle into homelab-gitops

## Before first deploy

1. **Resolve the `chronicle-db` digest.** `compose.yaml` carries a placeholder
   and `check-pins.sh` will fail until it is real:
   ```bash
   docker pull pgvector/pgvector:pg16
   docker inspect --format='{{index .RepoDigests 0}}' pgvector/pgvector:pg16
   ```
   Put it in `compose.yaml` and add a row to the homelab `PINS.md`.

2. **Register the subnet.** Add `10.211.71.0/24 — chronicle` to `NETWORKS.md`.
   70 is telegram-sync; 71 was free at VERSION 1.4.0. Re-check before using it.

3. **Secrets.** `cp .env.example .env`, fill in, then from the homelab repo
   `make encrypt STACK=chronicle`. Never commit plaintext.

4. **`make validate`** in the homelab repo.

## Install

```bash
ssh <vps>
cd /srv/stacks
git clone <chronicle repo> chronicle
./decrypt-all.sh                      # the classic mistake is skipping this
cd chronicle && docker compose up -d
```

`gitops-sync.sh` polls `origin/main` of the *homelab* repo. Chronicle is a
separate repo, so either add it as a submodule or pull it explicitly — decide
this deliberately, because a stack that gitops-sync doesn't know about will
silently stop receiving updates.

## Backfill order

Deliberately sequenced so each step is useful alone and nothing is wasted if
you stop.

**0. `whisperx` → `large-v3-turbo` first.** One line in `whisperx/compose.yaml`.
It currently runs `ASR_MODEL: small`, the weakest useful tier, and Slavic
languages is where that gap is widest. Unlocks 18,638 voice and video notes
that are invisible to every search today. The 5 GB `mem_limit` is already
allocated. Highest value per keystroke in the entire plan.

**1. Ingest Telegram.** Read-only against telegram-sync.
```bash
docker compose run --rm chronicle-worker python -m chronicle.worker ingest --source telegram
```

**2. Fit gaps, then segment.**
```bash
docker compose run --rm chronicle-worker python -m chronicle.worker fit-gaps
docker compose run --rm chronicle-worker python -m chronicle.worker segment
```
Then **read 100 random episodes before trusting anything downstream.** If
segmentation is wrong, everything inherits the damage — and it is much cheaper
to find that out now than after a two-week enrichment run.

**3. Embed.** ~50k episodes at ~60/s on CPU is roughly 20 minutes.

**4. Measure against grep.** Write 30–50 real questions, score `ripgrep` over a
text dump, then score Chronicle. Letta hit 74% on LoCoMo with nothing but a
filesystem and `grep`, beating Mem0's 68.5%. If Chronicle doesn't clearly win,
stop and fix segmentation rather than adding features.

**5. Enrich, incrementally, newest-first.** Schedule the worker from n8n so
recent data becomes useful while the backfill grinds. Expect weeks on CPU.

**6. Add wakapi and dawarich.** Pure SQL, no NLP, and they immediately improve
every timeline. This is the step that proves the event-store generalization
was worth building.

## After the episode index is live

**Delete something.** Drop the `telegram_personal` Qdrant collection and the
embedding path in `telegram-sync/app.py`. Stack count goes +1; responsibility
count stays roughly flat. A brain that only adds is debt.

## Rollback

Chronicle is read-only against every source. Nothing it does can corrupt
telegram-sync, wakapi or dawarich. `docker compose down` plus dropping the
`chronicle_pg` volume returns the box to its prior state.
