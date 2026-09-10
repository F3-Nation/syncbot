# API Reference

## HTTP Endpoints (Lambda Function URL / Cloud Run)

A single public HTTPS base serves every path. On AWS that is the Lambda Function URL; on GCP it is the Cloud Run URL. After you deploy, point Slack at the `/slack/*` URLs. The `/api/federation/*` endpoints are for cross-instance communication when External Connections are enabled.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness probe used by GCP Cloud Scheduler keep-warm and operators; returns a simple OK response |
| `POST` | `/slack/events` | Receives all Slack events (messages, actions, and view submissions) |
| `GET` | `/slack/install` | Starts OAuth: sets Bolt's state cookie and redirects the browser to Slack's authorization screen |
| `GET` | `/slack/oauth_redirect` | OAuth callback after the user approves. On success, SyncBot publishes that user's Home tab so **Authorize SyncBot** can disappear without a Refresh |

There are no slash commands.

### Federation inbound (this instance)

These paths exist only when **Federation** is on in Settings. Otherwise Lambda and Cloud Run return **404** for every `/api/federation*` path, including ping.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/federation/pair` | Accept an incoming external connection request (Ed25519-signed body) |
| `POST` | `/api/federation/message` | Receive and apply a message-create envelope; JSON may include public `images` and boolean `reply_broadcast` |
| `POST` | `/api/federation/message/edit` | Receive and apply a message-edit envelope; JSON may include public `images` |
| `POST` | `/api/federation/message/delete` | Receive and apply a message-delete envelope from a connected instance |
| `POST` | `/api/federation/message/react` | Receive and apply a reaction add or remove using the target channel's reaction type |
| `POST` | `/api/federation/users` | Exchange user directory with a connected instance |
| `GET` | `/api/federation/ping` | Health check for connected instances (only when federation is on) |

Inbound message and reaction endpoints resolve the addressed `SyncChannel` as a target and apply the envelope through the same target logic as local fan-out. If that channel does not subscribe, the request is accepted without writing to Slack. Federation copies never become a new source or create another hop.

### Federation outbound (this instance → peer)

When a channel sync includes a federated workspace, this instance POSTs to the peer's advertised federation endpoint and appends the resource subpath (`/pair`, `/message`, `/message/edit`, `/message/delete`, `/message/react`, `/users`). Every outbound federation call is a POST. The `GET /api/federation/ping` health check listed above is inbound only, so this instance answers a peer's ping but never sends one. The connection code's `webhook_url` is that full endpoint (this instance serves it at `/api/federation`), so the mount path travels in the code and is not assumed by the sender. Connection codes are signed JSON (webhook URL, instance id, public key, and `sig`). The instance id is the SHA-256 fingerprint of the Ed25519 public key. Hosted Slack file bytes are not sent on the wire; public GIF/image URLs are.

## Subscribed Slack Events

| Event | Handler | Description |
|-------|---------|-------------|
| `app_home_opened` | `handle_app_home_opened` | Publishes the Home tab with workspace groups, channel syncs, and user mapping. |
| `app_uninstalled` | `handle_app_uninstalled` | Workspace uninstall: Bolt `InstallationStore.delete_all` (bot + every user install row), then pause groups and channel syncs. |
| `member_joined_channel` | `handle_member_joined_channel` | Detects when SyncBot is added to an unconfigured channel; posts a message and leaves. |
| `message.channels` / `message.groups` | `respond_to_message_event` | Fires on new messages, thread broadcasts, `/me`, edits, deletes, and file shares in public/private channels. |
| `reaction_added` / `reaction_removed` | `_handle_reaction` | Syncs emoji reactions from publishing channels to subscribing targets; skips user-token echo events SyncBot applied on a target. |
| `team_join` | `handle_team_join` | Fires when a new user joins a connected workspace. Adds the user to the directory and re-checks unmapped user mappings. |
| `tokens_revoked` | `handle_tokens_revoked` | User-token revoke: Bolt `delete_installation` for that person, then republish Home. A `tokens.bot` array is treated as uninstall only when the stored bot token fails `auth.test`. |
| `user_profile_changed` | `handle_user_profile_changed` | Detects display name or email changes and updates the user directory and mappings. |
