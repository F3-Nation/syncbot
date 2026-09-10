# AI agents on SyncBot

This repository is set up so coding agents (Cursor, GitHub Copilot, Codex, Claude) can work with clear boundaries. Start with [AGENTS.md](../AGENTS.md) for commands, pitfalls, and **docs voice**. This page is CI guardrails, issue templates, and how we review agent-authored PRs.

## Read these first

| File | Purpose |
|------|---------|
| [AGENTS.md](../AGENTS.md) | Primary guardrails, commands, pitfalls, docs voice |
| [.github/copilot-instructions.md](../.github/copilot-instructions.md) | Short Copilot-specific checklist |
| [.cursor/rules/](../.cursor/rules/) | Cursor rules (architecture, short commits/changelog, helpers imports, tests, infra) |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Short Conventional Commits + workflow |

## Plan files (never commit)

Write working plans only under **`.plans/`** at the repo root. That folder is gitignored and is not documentation. Do not park plans under `docs/` (even ignored `docs/PLAN_*.md` names look like operator docs). Cursor UI plans live outside the tree; do not copy them into `docs/`. `git status` at PR time must not show a plan file.

## How CI guards agents

On pull requests, [.github/workflows/ci.yml](../.github/workflows/ci.yml) includes:

- **`forbidden-edits`** — blocks removing the CHANGELOG `<!-- version list -->` marker, hand bumps of `version` in `pyproject.toml`, and `*requirements.txt` changes that are not paired with `poetry.lock` / `pyproject.toml`.
- **`forbidden-imports`** — blocks `boto3` / `google.cloud` imports under `syncbot/`.
- **`ruff`** — `ruff check` and `ruff format --check`.
- **`pip-audit`** — exports from `poetry.lock` and audits (runs when Python dependency files change).
- **`sam-lint`** — `sam validate --lint` (runs when AWS templates / SAM workflow pins change).
- **`terraform-validate`** — `terraform init -backend=false`, `validate`, and `fmt -check` in `infra/gcp` (path-filtered; skipped is OK).
- **`docker-build-gcp`** — `docker build -f infra/gcp/Dockerfile --platform linux/amd64 .` from the repo root, no push (path-filtered).
- **`test`** — `pytest` over `tests/` and infra tests (same as local command in [AGENTS.md](../AGENTS.md)).
- **`ci-gate`** — aggregator; the check to require on `main`. Skipped path-filtered jobs count as success.
- **`requirements-sync`** — on same-repo PRs to F3Nation-Community/slack-syncbot, may commit `*requirements.txt` from `poetry.lock` as **`f3n-community-automation[bot]`** **without** `[skip ci]` so `ci-gate` / `conventional` re-run on HEAD. Forks must export themselves.

Release automation and signed bot commits are described in [DEVELOPMENT.md](DEVELOPMENT.md) — Releases & Versioning and **Automation GitHub App**. `release.yml` uses the App token for `git.updateRef`; `GITHUB_TOKEN` cannot bypass the `main` ruleset. Dependabot auto-merge also uses the App token so merge can retrigger workflows.

## Filing an AI-friendly issue

Use **AI-eligible task** in GitHub’s issue templates. Include goal, acceptance criteria, how to test, and what’s out of scope.

## Reviewing AI-authored PRs

- Confirm the PR title matches **Conventional Commits** (required for squash merges). Subject only; no essay body.
- Changelog bullets stay **1.2.0** length (one short line). Reject paragraph dumps.
- Look for forbidden-file edits; CI should fail them, but reviewers should still watch for secrets.
- Ensure tests cover behavior changes; spot-check Slack/event flows when touching handlers.
- Pytest `pythonpath` is `syncbot/` and `infra/aws/lambda` only. Import test helpers as `from tests.event_fixtures import …`; do not add `tests` to `pythonpath`.

## When changing Slack user scopes

`USER_SCOPES` in [`syncbot/slack_manifest_scopes.py`](../syncbot/slack_manifest_scopes.py) must stay in lockstep with the manifests and `SLACK_USER_SCOPES` defaults (see that module's header). The Home tab **Authorize SyncBot** section does not list those API names. It lists `USER_PERMISSION_GROUPS`.

The 1.3.2 list was built as follows; keep new rows on the same rails:

1. Start from the manifest **user** scopes, not bot scopes.
2. Look up each scope on [Slack's scopes reference](https://docs.slack.dev/reference/scopes) for the *user-token* meaning, then write a 2–4 word label. Never paste `channels:history`. Do not start with "Can" or "Allow".
3. Fold scopes people experience as one capability (history+read of the same channel type, files read+write, reactions, users.read + email). Keep `groups:write` as its own line because inviting the bot into a private Channel is not the same as viewing one.
4. A group counts as already allowed only when every scope in it is on the stored token. First-time authorize hides the already-allowed list; a later scope add shows it with checkmarks so re-authorize does not look like a redo.

`tests/test_slack_manifest_scopes.py` asserts every `USER_SCOPE` sits in exactly one group. The recipe also lives as comments on `USER_PERMISSION_GROUPS`.

## Public origin and OAuth install

This instance's public HTTPS origin is the Host of incoming Slack requests (the same URL Slack already uses for events). `helpers.oauth.get_public_base_url` / `capture_public_base` serve Authorize SyncBot (`/slack/install?team=`) and federation webhooks. Do not read `SYNCBOT_PUBLIC_URL`; if that leftover env var is set, the app logs a warning and ignores it. Federation instance id is the SHA-256 fingerprint of this instance's Ed25519 public key; leftover `SYNCBOT_INSTANCE_ID` is warned and ignored. **Authorize SyncBot** stores that person's user token for the target workspace: private-channel invitations, native reactions, and target messages/files as them. DMs remain bot-authored. Look up tokens with `get_user_token(target_team_id, mapped_user_id)`; never send `xoxp` on federation payloads. Leftover Settings env (`ALLOW_PRIVATE_CHANNELS`, `BROADCAST_ALLOWED_WORKSPACES`, `SOFT_DELETE_RETENTION_DAYS`, `SYNCBOT_FEDERATION_ENABLED`, `REQUIRE_ADMIN`) is warned and ignored.

Bolt OAuth must start at **this instance's** `GET /slack/install`. A Home-tab URL that points at Slack's authorize page skips the state cookie and fails after Allow with `invalid_browser`. On Lambda, Function URL payload 2.0 needs the OAuth cookie in the `cookies` array, and stray GETs such as `/favicon.ico` must not be treated as a second install. After a successful callback, call `refresh_home_after_oauth_install` so that user's Home tab updates without a manual Refresh.

Token rows live in Bolt's `EncryptedSQLAlchemyInstallationStore` (`slack_bots` / `slack_installations`); OAuth tokens are encrypted at rest when `DATA_ENCRYPTION_KEY` is active. Use those store methods; do not hand-edit token columns. A personal revoke is `delete_installation(..., user_id=...)`. A workspace uninstall is `delete_all` (same as Bolt's `app_uninstalled` listener) plus SyncBot's workspace pause. Do not call `App.enable_token_revocation_listeners()`: that deletes the bot whenever Slack fills `tokens.bot`, including on a personal revoke, which blanks Home. `tokens_revoked` with a live bot token is user-only even if `tokens.bot` is set. Do not leave a tokenless per-user `slack_installations` row; Bolt authorize looks that user up first and will skip the workspace bot token.

## Slack request lifecycle

All Slack traffic enters through [`syncbot/app.py`](../syncbot/app.py). Bolt matches `.*` once for events and actions; handlers are looked up in [`syncbot/routing.py`](../syncbot/routing.py) (`MAIN_MAPPER` → `ACTION_MAPPER` / `EVENT_MAPPER` / `VIEW_MAPPER`, plus `VIEW_ACK_MAPPER` for the fast ack). Do not add a second `@app.action` / `@app.event` — it double-fires. Prefixed destructive `action_id`s go through the confirmation flow in [`.cursor/rules/60-slack-confirmations.mdc`](../.cursor/rules/60-slack-confirmations.mdc). When an `action_id` or handler is renamed, change the constant, the emitted Block Kit id, the handler name, and the single mapper row together — do not keep the old Slack `action_id` mapped for in-flight Home/modal clicks; reopen is enough. Leftover env still warn-and-coalesce; old backup keys still import.

When you rename a Slack control, rename the ID, constant, handler, and route together. Do not keep leftover Slack IDs as aliases. A stale Home tab may `no_handler` until the user taps Refresh; that is expected. This app is low traffic. Prefixed Home buttons (`join_sync_{id}`) must not share a prefix with a modal picker (`select_join_sync`, not `join_sync_select`).

View submissions: ack-phase handlers in `VIEW_ACK_MAPPER` may return field errors (`{"response_action": "errors", ...}`) within Slack's ~3s budget and should avoid Slack/DB of consequence. After ack, the modal is gone — work-phase failures DM the user. Production wires `view_ack` then lazy `main_response`; local often runs one-shot.

Link buttons still fire `block_actions`. Register a no-op in `ACTION_MAPPER` (Authorize SyncBot is the example) or the click shows up as `no_handler`.

**Target pipeline:** Build one source-canonical envelope with `kind` (`message` or `reaction`) and its kind-specific `action`, then call `run_sync_pipeline` for new messages, threads, edits, deletes, files, and reactions. Origins must publish and targets must subscribe. Use `iter_publish_targets`; do not reintroduce `get_sync_list`. Dedupe by workspace and channel. Follow-ups (`thread_post_id` or the message `post_id`) apply only on those PostMeta records. `PostMeta` copies and federation inbound writes never originate, so there is no second hop. Channels may participate in multiple syncs for fan-in, and a Channel may be published in more than one group; one workspace cannot subscribe to the same published source twice. Participation controls reaction direction; reaction UI keeps Hybrid, Direct, or Off and must not retain reaction-direction action IDs. Off is a no-op for reaction add and remove.

**Naming:** Verb-led helpers (`build_`, `find_`, `get_`, `_format_`, `_parse_`, `_handle_`). Reuse source/target, origin, PostMeta, post_records, envelope, people, channel_ref. Do not invent graph, dest, member_ref, or other synonyms.

**Slack action icons:** Lead action buttons and in-channel/DM notices with one shortcode from [`.cursor/rules/85-slack-icons.mdc`](../.cursor/rules/85-slack-icons.mdc). Do not icon form labels, context metadata, or Authorize.

**User Mapping** opens with `views.open` of the current DB list only — never seed/map before open, never Home `views.publish`. **Auto Map Now** uses the button’s `view_id` to `views.update` Mapping users..., then directory map, then the results and last-run line (`last_auto_map`). An unmapped message/reaction author may be mapped on the fly by target directory email, then one `users.lookupByEmail` (`ensure_mapped_target_user_id`) — one person, email only. **Home push** publishes only the acting user after invalidating `home_tab_hash` / `home_tab_blocks` for that team; do not `users.list` admins to fan out.

**User-token echo:** Slack does not mark an `xoxp` write as a bot action. After a successful user-token side effect, `remember_user_action` in [`syncbot/helpers/user_action_echo.py`](../syncbot/helpers/user_action_echo.py); matching handlers call `take_user_action_echo` inside `run_claimed` before fan-out. Do not store echo rows in `processed_events`. Import the helper submodule directly (`from helpers.user_action_echo import …`), not via `helpers/__init__.py`.

**Hybrid emoji probe:** `_target_reaction_name_is_invalid` in [`syncbot/helpers/reaction.py`](../syncbot/helpers/reaction.py) runs only when Hybrid is about to post a thread notice (no target user token, or that token hit an auth error). Skip the probe only when the source and target are the **same Slack workspace**. Same-instance cross-workspace and **federation inbound** still probe: target custom emoji are per workspace, and the origin having the name does not mean the target does. Direct-only, Off, and a successful native `reactions_add` must not probe. Do not use `emoji.list`; the target bot `reactions_add` / `reactions_remove` probe is the current method.

**Source `#channel` and permalinks:** Target message bodies never remap `#channel` to a target twin. `resolve_channel_references` in [`syncbot/helpers/user_map.py`](../syncbot/helpers/user_map.py) turns `<#C>` and channel-only archive URLs into `` `#name (Workspace)` `` (federation too). Message permalinks (`/archives/C…/p…`) keep the source URL with a `Message in #channel (Workspace)` label. That opens the source message in the **Slack mobile app**. Slack **web** treats the same URL as the target and shows a Private chip; accepted — do not switch to `slack://` or `app.slack.com/client` to fix the desktop browser. Target `chat.postMessage` / `chat.update` calls use `unfurl_links=false`. Home `_format_channel_ref(is_local=True)` may still use native `<#C>` for this workspace's own channel. Do not put mrkdwn `<url|label>` inside a rich_text text node.

## DB identity and deletes

`DbManager.get_record(Model, id)` filters on that model's `get_id()` column, not always the integer primary key. Pass the matching value positional or as `id=` only (extra keywords such as `team_id=` raise `TypeError`). `Workspace.get_id()` is Slack `team_id`; `SyncChannel.get_id()` is its integer primary key; `PostMeta.get_id()` is `post_id`. Returned objects are expunged; each `DbManager` call opens and commits its own session, so there is no multi-call transaction.

Active rows are soft-delete aware: filter `deleted_at.is_(None)`. There is no `ON DELETE CASCADE` on sync graphs. Hard deletes go through `purge_sync` / `purge_workspace` in [`syncbot/helpers/sync_cleanup.py`](../syncbot/helpers/sync_cleanup.py) (children first, including soft-deleted rows). Leave Sync by the last publisher is a full `purge_sync`; otherwise Leave Sync removes only that workspace's channel. Pause/resume only toggles that workspace's channel.

Inside `helpers/*.py`, import submodules only (`from helpers._cache import …`, `from db import …`). Never `import helpers` from a helper submodule — `helpers/__init__.py` would circular-import at Lambda cold start.

## Fork compatibility

`release.yml`, Dependabot auto-merge, and semantic-release config apply to **F3Nation-Community/slack-syncbot** only. Deploy forks keep `test`/`prod` Environments and must not mint duplicate GitHub Releases. CODEOWNERS handles are organization-specific; replace `@sprocktech-dev` on other orgs. See [INFRA_CONTRACT.md](INFRA_CONTRACT.md) Fork Compatibility Policy.

## Branch protection (F3Nation-Community/slack-syncbot)

Configure in GitHub **Settings → Rulesets / Branches** for `main`:

- Require a pull request before merging
- Required checks: **`ci-gate`**, **`conventional`**
- Do **not** require review from Code Owners (that would block Dependabot auto-merge)
- Do **not** add Dependabot, Write, or Maintain to the ruleset bypass list; `github-actions[bot]` cannot be added here. Organization admin bypass is for humans. Add GitHub App **`f3n-community-automation`** so Release `updateRef` and leftover requirements-sync pushes can land on `main` (see [DEVELOPMENT.md](DEVELOPMENT.md)).
- Allow auto-merge; squash only; do not require conversation resolution

Exact job names come from `.github/workflows/ci.yml` and `pr-title.yml`.
