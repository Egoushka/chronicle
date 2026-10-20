---
title: "Develop"
description: "Run the unit tests and the database smoke test, and enable the hooks that keep private terms and secrets out of commits."
order: 8
section: "Project"
---

## Tests

```bash
make test     # unit tests; bootstraps .venv, no database, no models
make smoke    # every migration and SQL function, plus the integration tests, against a throwaway PostgreSQL
make lint
```

CI runs both, plus `bash -n` on every script, gitleaks, and a check that every commit uses a GitHub noreply address ([ci.yml](../../.github/workflows/ci.yml)).

## Hooks

Before committing, enable the hooks: `git config core.hooksPath .githooks`, then copy `.private-terms.example` to `.private-terms` and list what must never appear here. The hooks block private terms in files and commit messages, run gitleaks, and require a GitHub noreply address; CI checks the same.
