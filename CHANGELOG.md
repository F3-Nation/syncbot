# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- version list -->


## [Unreleased]

### Added

- Synced messages and ordinary file shares can post as the mapped person in the other workspace when they have authorized there
- A Channel can publish only, subscribe only, or both; a second publisher stays in the same channel sync until someone subscribes
- One Channel can subscribe to more than one distinct published Channel
- Reaction type Off does not apply incoming reaction add or remove on a subscribing Channel

### Changed

- Authorize SyncBot no longer asks to send DMs as you
- New publishes are visible to the whole group; a Channel may be published in more than one group
- Reaction send and receive follow publish and subscribe; Reaction type is Hybrid, Direct, or Off
- Home Synced Channels lists each partner as a code-ticked `#channel (Workspace)`
- Home Synced Channels Status includes participation, for example Active (Publish and Subscribe)
- Home action buttons and in-channel sync notices lead with an emoji

### Fixed

- Thread replies, files, and reactions stay on the original message's sync
- Unmapped people in synced messages render as Slack code, not literal backticks

## [1.5.4] - 2026-09-09

### Fixed

- Keep changelog tests aligned with shortened 1.2.0-length notes (#43)


## [1.5.3] - 2026-09-05

### Changed

- User Mapping leftover match names are gone (`matched_at` is `mapped_at`; old backup keys still import)
- `TOKEN_ENCRYPTION_KEY` still loads when `DATA_ENCRYPTION_KEY` is unset and logs a deprecation warning

## [1.5.2] - 2026-09-03

### Changed

- Synced message `#channel` mentions become a code-ticked `#name (Workspace)`, not a dest twin or a deep link

### Fixed

- Mentions, source permalinks, and federation inbound follow the same mapping rules
- Dest Block Kit posts stay within Slack's block and section limits
- Threaded file shares use the matching dest timestamp
- AWS and GCP deploy keep previous secrets, default `syncbot_${stage}`, and pass Slack scopes from env

## [1.5.1] - 2026-09-03

### Changed

- User Mapping uses map wording, a 20-row page, and `last_auto_map`
- Federation instance id is a SHA-256 fingerprint of the Ed25519 public key; connection codes carry the peer's full endpoint URL
- Sync hot paths reuse per-request and 60s process caches

### Fixed

- Also-send-to-channel replies, hosted files, and file shares sync at the right thread level
- Block Kit bot posts keep newlines and emoji; unmapped mentions use a code-ticked display name
- On-the-fly mapping uses dest directory email then one `lookupByEmail` and persists a none stub
- Federation connection codes, image blocks, Lambda Function URLs, and mention rewrite work across instances
- AWS migrate reports FunctionError, Lambda timeout is 120s, and MySQL column CHANGE includes the existing type
- Leftover `SYNCBOT_INSTANCE_ID` is ignored

## [1.5.0] - 2026-09-02

### Added

- User Mapping modal with Auto Map Now
- SyncBot posts in a Channel when it is published, even before anyone subscribes

### Changed

- User Mapping opens from saved mappings; Auto Map Now runs in the background of the modal
- Failure DMs include a copyable details block
- New Channel syncs default to Hybrid reactions, listed first on Publish, Subscribe, and Edit

### Fixed

- Users with the same email across Workspaces are mapped automatically
- Saving Edit on a Channel sync no longer times out the Lambda while listing every member


## [1.4.1] - 2026-09-01

### Fixed

- Hybrid reaction notices delete on unreact, including child notices under them
- Deleting a Hybrid notice on one target stays local to that channel
- Bot token refresh no longer rewrites the database on every Home publish
- User OAuth tokens encrypt at rest; they were stored in plaintext
- Leftover `REQUIRE_ADMIN`, `SYNCBOT_FEDERATION_ENABLED`, and `SYNCBOT_PUBLIC_URL` removed from deploy; enable federation in Settings if it was never saved there
- Edit Channel sync keeps the saved reaction type when direction is send only or off


## [1.4.0] - 2026-09-01

### Added

- Federation is enabled from the Settings modal on the primary workspace
- Extra workspace managers can configure groups and syncs without opening Settings
- Per-workspace Allow private Channels in Settings on every installed workspace
- Per-channel send/receive reaction direction and direct vs hybrid reaction type
- Home **Edit** on synced Channel rows for Any vs Specific and this workspace’s reactions

### Changed

- Settings is available on every workspace for Slack admins; instance fields stay primary-only
- Leftover `SYNCBOT_FEDERATION_ENABLED`, `REQUIRE_ADMIN`, and `ALLOW_PRIVATE_CHANNELS` env is ignored; one-time seed copies federation and private-channel policy

### Fixed

- Reaction sync ignores user-token echo events and no longer threads on `already_reacted` or missing custom emoji
- Workspace-settings upgrade quotes the reserved MySQL `key` column
- Schema errors are not retried, and view acks still return if a handler raises


## [1.3.3] - 2026-09-01

### Changed

- Home no longer keeps leftover DeSync or Create new / Join existing Sync routes; Publish, Subscribe, Unpublish, and Stop Syncing are the Channel Sync path

### Fixed

- Data Migration export/import looks up Workspaces by integer id via `get_workspace_by_id`


## [1.3.2] - 2026-08-31

### Added

- **Authorize SyncBot** on Home for anyone missing user permissions (starts at `/slack/install`)

### Changed

- `REQUIRE_ADMIN` still limits configuration; every user can open Home, authorize, and Refresh
- `SYNCBOT_PUBLIC_URL` is ignored; Authorize and federation use the request Host

### Fixed

- Private Channels can be published and subscribed: SyncBot is invited in, stays when added by hand, and a failed invite rolls back with a DM
- OAuth Allow no longer fails with `invalid_browser`; Home updates after Authorize
- Revoking your own token no longer pauses the workspace; Authorize returns, and uninstall still clears all tokens
- Home lists available Channels by name, not Slack ID
- Private-Channel invites no longer fail with `user_not_found` after use in another workspace


## [1.3.1] - 2026-08-31

### Fixed

- Channel pickers search the whole workspace instead of stopping at 100 channels
- Picking a channel already in a Channel Sync explains the problem in the dialog

### Changed

- A channel may belong to only one Channel Sync instance-wide
- Retention, private-channel publishing, and the broadcast allow-list are set only in **Settings**
- Private channels are off by default; the dialog warns that messages will be copied
- **Publish Channel** and **Subscribe** replace Sync Channel and Start Syncing
- A channel SyncBot cannot read is rejected rather than failing later


## [1.3.0] - 2026-08-30

### Added

- Group ownership: **Promote to Owner** and **Give Up Ownership**, keeping at least one owner
- **Disband Group** for a sole owner that is also the sole publisher
- Group owners can cancel a pending invite
- Operator **Settings** for retention, private channels, and the broadcast allow-list

### Changed

- `SOFT_DELETE_RETENTION_DAYS`, `ALLOW_PRIVATE_CHANNELS`, and `BROADCAST_ALLOWED_WORKSPACES` are seed values; Settings wins once saved
- Retention changes apply without a restart
- Ownership survives uninstall until the workspace's data is deleted
- Destructive confirmations use a red button


## [1.2.6] - 2026-08-30

### Fixed

- Channel unpublish and publisher teardown no longer leave a broken sync

## [1.2.5] - 2026-08-30

### Fixed

- Group invite accept, decline, and cancel require authorization

## [1.2.4] - 2026-08-30

### Fixed

- GitHub Actions trusts the immutable OIDC subject claim

## [1.2.3] - 2026-08-30

### Fixed

- Lambda builds from source so SAM can reach `syncbot/`

## [1.2.2] - 2026-08-30

### Changed

- Cloud deploy uses `DATABASE_BACKEND` and `CLOUD_PROVIDER`; leftover alias names warn until 2.0.0
- GitHub Actions picks provider and stage from the job; the first AWS deploy creates the bootstrap stack
- First-time deploy docs and `.env.deploy.example` match the AWS and GCP paths

### Fixed

- `./deploy.sh` no longer looks up leftover Secrets Manager IDs

## [1.2.1] - 2026-08-27

### Fixed

- AWS stack no longer creates RDS; SQLite + Litestream replicas go to S3

## [1.2.0] - 2026-08-27

### Added

- GCP: SQLite + Litestream to GCS as the free Cloud Run default; existing MySQL/TiDB remains opt-in
- Canonical sprocktech/syncbot release automation (python-semantic-release, Dependabot auto-merge)

### Changed

- Cloud Run image updates are CI-only; later `terraform apply` does not revert the image
- `./deploy.sh` does not run `poetry update`; local and GitHub deploys install committed pins
- Bumped Python and GitHub Actions dependencies (patch/minor)

### Fixed

- Slack message and reaction sync is idempotent on envelope `event_id`, so retries do not double-post

## [1.1.0] - 2026-04-21

### Added

- Deploy flags (`--bootstrap`, `--setup-github`, `--update-stack`, `--verbose`), `.env.deploy.example`, and a summary with the OAuth redirect URL
- CI bootstrap sync, `workflow_dispatch`, concurrency groups, and `pip-audit`

### Changed

- AWS uses Lambda Function URLs (no API Gateway or Secrets Manager); GCP secrets are Terraform variables
- `TOKEN_ENCRYPTION_KEY` renamed to `DATA_ENCRYPTION_KEY` (legacy fallback kept)
- Deploy env uses `DATABASE_*`; `DATABASE_USER` is a GitHub variable, not a secret

### Fixed

- Interactive GitHub push sets Lambda SG ID and `SLACK_CLIENT_ID` correctly

## [1.0.2] - 2026-03-28

### Added

- External DB deploy parameters for TiDB Cloud and other managed providers (cluster-prefixed usernames, 32-char limits)

### Changed

- Synced message author shows the local display name and avatar for mapped users
- Default DB usernames shortened to `sbadmin_{stage}` and `sbapp_{stage}`; existing RDS master names stay
- AWS Lambda migrations run once post-deploy (not on cold start); memory is 256 MB; re-run bootstrap for `lambda:InvokeFunction`

## [1.0.1] - 2026-03-26

### Changed

- Cross-workspace `#channel` links resolve to native local channels when the channel is part of the same sync; otherwise use workspace archive URLs with a code-formatted fallback
- `@mentions` and `#channel` links in federated messages are now resolved on the receiving instance (native tags when mapped/synced, fallbacks otherwise)
- `ENABLE_DB_RESET` is now a boolean (`true` / `1` / `yes`) instead of a Slack Team ID; requires `PRIMARY_WORKSPACE` to match

### Added

- `PRIMARY_WORKSPACE` env var: must be set to a Slack Team ID for backup/restore to appear. Also scopes DB reset to that workspace.

## [1.0.0] - 2026-03-25

### Added

- Multi-workspace message sync: messages, threads, edits, deletes, reactions, images, videos, and GIFs
- Cross-workspace @mention resolution (email, name, and manual matching)
- Workspace Groups with invite codes (many-to-many collaboration; direct and group-wide sync modes)
- Pause, resume, and stop per-channel sync controls
- App Home tab for configuration (no slash commands)
- Cross-instance federation (optional, HMAC-authenticated)
- Backup/restore and workspace data migration
- Bot token encryption at rest (Fernet)
- AWS deployment (SAM/CloudFormation) with optional CI/CD via GitHub Actions
- GCP deployment (Terraform/Cloud Run) with interactive deploy script; GitHub Actions workflow for GCP is not yet fully wired
- Dev Container and Docker Compose for local development
- Structured JSON logging with correlation IDs and CloudWatch alarms (AWS)
- PostgreSQL, MySQL, and SQLite database backends
- Alembic-managed schema migrations applied at startup
