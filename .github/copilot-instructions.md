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

- Route handlers through `routing.py` only — do not add `@app.action` / `@app.event`. Rename Slack IDs, handlers, and routes together; do not keep leftover IDs. Stale Home may `no_handler` until Refresh. Prefixed Home buttons must not share a prefix with a modal picker (`select_join_sync`, not `join_sync_select`). Leftover env still warn-and-coalesce; old backup keys still import.
- Inside `helpers/*.py`, import submodules only (`from helpers._cache import …`); never `import helpers`.
- `DbManager.get_record` uses each model's `get_id()` (for example, `Workspace` → Slack `team_id` and `SyncChannel` → integer primary key). Only positional or `id=`.
- Federation on/off is `helpers.federation_enabled()` (Settings DB), not env. Leftover `SYNCBOT_FEDERATION_ENABLED` is warned and ignored after a one-time upgrade seed. Leftover `REQUIRE_ADMIN` and `SYNCBOT_INSTANCE_ID` are warned and ignored.
- `is_workspace_admin` (Slack admin/owner) opens Settings, Backup, Reset, External Connections. `is_workspace_manager` (admin or extra list) configures groups and syncs.
- Authorize SyncBot stores a target-workspace user token for private-channel invitations, native reactions, and target messages/files as that person; DMs remain bot-authored. Direct reactions use `get_user_token(target_team_id, mapped_user_id)`, never the event team. Never put `xoxp` on federation payloads or store reverse-map results as target token lookup keys. User-token echo uses `remember_user_action` / `take_user_action_echo` inside `run_claimed` (`helpers/user_action_echo.py`). Probe the target emoji name before a Hybrid thread notice in another workspace; same-workspace Hybrid skips the probe. The origin having the emoji does not mean the target has it. Do not use `emoji.list`. Unreact deletes target Hybrid notices; a target user deleting a notice is local only. OAuth tokens encrypt at rest via `EncryptedSQLAlchemyInstallationStore`; never compare two Fernet blobs.
- Build one source-canonical `kind`/`action` envelope and use `run_sync_pipeline` for messages, threads, edits, deletes, files, and reactions. Origins publish; targets subscribe. Use `iter_publish_targets`, not the removed `get_sync_list`, and dedupe by workspace/channel. Follow-ups stay on the original message's PostMeta records. Copies and federation inbound writes never originate, so there is no second hop. Name helpers with verbs (`build_`, `find_`, `get_`, `_format_`); reuse source/target, origin, PostMeta, post_records, envelope, people, channel_ref.
- Action buttons and in-channel/DM notices lead with one Slack emoji from `.cursor/rules/85-slack-icons.mdc` (pause `:double_vertical_bar:`, resume `:arrow_forward:`, leave `:octagonal_sign:`, live sync `:arrows_counterclockwise:`). Do not icon form labels, context metadata, or Authorize.
- Channels may participate in multiple syncs for fan-in. A Channel may be published in more than one group. Reject a second subscription by one workspace to the same published source, but allow a target already used in another sync. Participation controls reaction direction; keep Hybrid, Direct, or Off reaction choices and do not revive reaction-direction action IDs. Off is a no-op for reaction add and remove.
- User Mapping opens from DB only; Auto Map Now updates the open modal via `view_id` (not Home `views.publish`); do not `users.list` on open/Refresh List/Auto Map Now/join or to fan out Home. On-the-fly author map is one person, email only (`ensure_mapped_target_user_id`).
- Sync Block Kit from `event.blocks`, not truncated `event.text`. Drop `actions`/`input`. Do not probe target emoji for message bodies.
- Message-body `#channel` is a source code-tick; permalinks stay labeled source `archives/C…/p…` URLs (mobile opens them; Slack web Private chip is accepted). Never use a target twin, `slack://`, or `app.slack.com/client`.

## Optional: CI parity check

Repository workflow `.github/workflows/copilot-setup-steps.yml` mirrors installing Poetry deps like CI.
