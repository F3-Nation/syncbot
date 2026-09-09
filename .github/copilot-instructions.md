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

- Bump `pyproject.toml` `version` or add a `## [X.Y.Z]` CHANGELOG heading in a feature PR. You may add `## [Unreleased]` notes **below** `<!-- version list -->` (the release job retitles that heading). Do not hand-edit `*requirements.txt` (exports handle those).
- Commit `.env` secrets or `.aws-sam/` build output.
- Link OAuth at `slack.com/oauth/v2/authorize`, or treat `SYNCBOT_PUBLIC_URL` as required. Use `/slack/install` and `get_public_base_url`.

## PR rules

- Title must be a **Conventional Commit** (squash merge), subject only, about 72 characters.
- Changelog bullets are one short line like **1.2.0** (what changed, not why).
- Link issues with `Fixes #n` when fixing bugs.

## User scopes on Home

Do not show Slack API scope names on **Authorize SyncBot**. Add new user scopes to `USER_SCOPES` and to `USER_PERMISSION_GROUPS` in `syncbot/slack_manifest_scopes.py` (plain 2–4 word labels; fold read/write twins; keep `groups:write` separate). See that constant's comment and [docs/AI_AGENTS.md](../docs/AI_AGENTS.md).

## Gotchas (short)

- Route handlers through `routing.py` only — do not add `@app.action` / `@app.event`. When renaming an `action_id` or handler, update the constant, Block Kit id, handler, and single mapper row together; do not keep leftover action ids for in-flight clicks (reopen Home/modal is enough). Leftover env still warn-and-coalesce; old backup keys still import.
- Inside `helpers/*.py`, import submodules only (`from helpers._cache import …`); never `import helpers`.
- `DbManager.get_record` uses each model's `get_id()` (e.g. `Workspace` → Slack `team_id`), not always the integer PK. Only positional or `id=`.
- Federation on/off is `helpers.federation_enabled()` (Settings DB), not env. Leftover `SYNCBOT_FEDERATION_ENABLED` is warned and ignored after a one-time upgrade seed. Leftover `REQUIRE_ADMIN` and `SYNCBOT_INSTANCE_ID` are warned and ignored.
- `is_workspace_admin` (Slack admin/owner) opens Settings, Backup, Reset, External Connections. `is_workspace_manager` (admin or extra list) configures groups and syncs.
- Direct reactions use `get_user_token(dest_team_id, mapped_user_id)`, never the event team. Never put `xoxp` on federation payloads. Do not store reverse-map results as dest `xoxp` lookup keys. User-token echo: `remember_user_action` / `take_user_action_echo` inside `run_claimed` (`helpers/user_action_echo.py`). Hybrid dest-name probe before a thread notice in another workspace (same-instance or federation inbound); same-workspace Hybrid skips probe. Origin having the emoji does not mean dest has it. Do not use `emoji.list`. Unreact deletes dest Hybrid notices; dest user deleting a notice is local only. OAuth tokens encrypt at rest via `EncryptedSQLAlchemyInstallationStore`; never compare two Fernet blobs.
- User Mapping opens from DB only; Auto Map Now updates the open modal via `view_id` (not Home `views.publish`); do not `users.list` on open/Refresh List/Auto Map Now/join or to fan out Home. On-the-fly author map is one person, email only (`ensure_mapped_target_user_id`).
- Sync Block Kit from `event.blocks`, not truncated `event.text`. Drop `actions`/`input`. Do not probe dest emoji for message bodies.
- Message-body `#channel` is a source code-tick; permalinks stay labeled source `archives/C…/p…` URLs (mobile opens them; Slack web Private chip is accepted). Never dest twin / `slack://` / `app.slack.com/client`.

## Optional: CI parity check

Repository workflow `.github/workflows/copilot-setup-steps.yml` mirrors installing Poetry deps like CI.
