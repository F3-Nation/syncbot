# Copilot instructions — SyncBot

Short directives for GitHub Copilot (coding agent / workspace). Full context: [AGENTS.md](../AGENTS.md) and [docs/AI_AGENTS.md](../docs/AI_AGENTS.md).

## Stack

- Python 3.12+, Poetry, Slack Bolt, SQLAlchemy, Alembic.
- App code in `syncbot/` must stay **free of AWS/GCP SDK imports** (`boto3`, `google.cloud`) — put cloud logic under `infra/` only.

## Commands (run after edits)

```bash
poetry install --with dev
pre-commit run --all-files
poetry run ruff check .
poetry run pytest -q tests/ infra/aws/tests infra/gcp/tests
```

## Do not

- Bump `pyproject.toml` `version` or `syncbot/constants.py` `__version__`, or add a `## [X.Y.Z]` CHANGELOG heading in a feature PR. You may add `## [Unreleased]` notes **below** `<!-- version list -->` (the release job retitles that heading). Do not hand-edit `*requirements.txt` (exports handle those).
- Commit `.env` secrets or `.aws-sam/` build output.
- Link OAuth at `slack.com/oauth/v2/authorize`, or treat `SYNCBOT_PUBLIC_URL` as required. Use `/slack/install` and `get_public_base_url`.
- Copy operator screenshot names or community-specific names into tests or docs. Use generic fixtures (`Workspace A`, `#announcements`, `Ada Lovelace`). `Partner Org` is a connection label, not a workspace name.
- Skip tests or use `filterwarnings` to hide pytest warnings. Fix the source.

## PR rules

- Title must be a **Conventional Commit** (squash merge), subject only, about 72 characters.
- Changelog bullets are one short line like **1.2.0** (what changed, not why).
- Link issues with `Fixes #n` when fixing bugs.

## User scopes on Home

Do not show Slack API scope names on **Authorize SyncBot**. Add new user scopes to `USER_SCOPES` and to `USER_PERMISSION_GROUPS` in `syncbot/slack_manifest_scopes.py` (plain 2–4 word labels; fold read/write twins; keep `groups:write` separate). See that constant's comment and [docs/AI_AGENTS.md](../docs/AI_AGENTS.md).

## Gotchas (short)

- Route handlers through `routing.py` only — do not add `@app.action` / `@app.event`. Rename Slack IDs, handlers, and routes together; do not keep leftover IDs.
- Inside `helpers/*.py`, import submodules only (`from helpers._cache import …`); never `import helpers`. Callers may use `helpers.X`.
- Build one source-canonical envelope and use `run_sync_pipeline` / `iter_publish_targets` (not `get_sync_list`). Same-instance uses `apply_target`; cross-instance uses `deliver_remote`. If you move those functions, retarget the **Message Sync Flow** `#L` links in `docs/ARCHITECTURE.md`. The mermaid there is Slack → SyncBot → Slack.
- OAuth starts at this instance's `/slack/install`; leftover `SYNCBOT_PUBLIC_URL` is ignored.
- Sync Block Kit from `event.blocks`; message-body `#channel` stays a source code-tick; unlabeled permalinks get `message in #channel (Workspace)`.
- Slack API research is Free-tier only; ignore Connect, Enterprise Grid, and `check_file_info`.
- Federation `file_chunk_mb` is this hop's injected HTTP receive cap (`FEDERATION_HTTP_MAX_MB`). Tests take the cap as an input. Slack files stay 1 GB.
- See [AGENTS.md](../AGENTS.md) **Common pitfalls** for Hybrid probe, user-token echo, Home push, User Mapping, and participation.

## Optional: CI parity check

Repository workflow `.github/workflows/copilot-setup-steps.yml` mirrors installing Poetry deps like CI.
