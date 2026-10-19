# API Reference

## HTTP Endpoints

A single public HTTPS origin serves every path. After you deploy, point Slack at the `/slack/*` URLs. The `/api/federation/*` endpoints are for cross-instance communication when External Connections are enabled. `/health` and `/ready` are served on the long-running HTTP listener (Cloud Run and local). AWS keep-warm is a Lambda invoke, not those paths.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness probe used by GCP Cloud Scheduler keep-warm and operators; pulses External Connection allowlists and returns JSON `{"status":"ok"}` |
| `GET` | `/ready` | Post-deploy: same pulse as `/health`, then republishes remembered Home tabs |
| `POST` | `/slack/events` | Receives all Slack events (messages, actions, and view submissions) |
| `GET` | `/slack/install` | Starts OAuth: sets Bolt's state cookie and redirects the browser to Slack's authorization screen |
| `GET` | `/slack/oauth_redirect` | OAuth callback after the user approves. On success, SyncBot publishes that user's Home tab so **Authorize SyncBot** can disappear without a Refresh |

There are no slash commands.

### Federation inbound (this instance)

These paths exist only when **Federation** is on in Settings. Otherwise the instance returns **404** for every `/api/federation*` path, including ping.

Once the request has the `SyncBot-Federation` User-Agent and federation is on, missing or malformed headers are **400** and a well-formed but untrusted signature is **401**. **404** is for no User-Agent, federation off, an unknown path, an unknown pairing code, or a missing channel/group/sync after a good verify.

| Status | When |
|--------|------|
| **404** | No `SyncBot-Federation` User-Agent, federation off, unknown path or method. After a good verify: missing channel, group, sync, or allowlist team. Unknown pairing code (do not leak). |
| **400** | User-Agent present; a required header is missing or malformed (`X-Federation-Signature`, `X-Federation-Timestamp` as an integer, `X-Federation-Instance` as 64 hex). `/file` also needs File-Sha256, Index, Total, and Size. Pair: header instance id does not match the body instance id. |
| **401** | Headers are well-formed but not trusted: unknown instance, `untrusted`, bad signature, or timestamp outside 5 minutes. |
| **413 / 409 / 410 / 503** | Oversize; already connected / incomplete file / assemble failed / parent missing; expired pairing code; identity or database not ready. |

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/federation/pair` | Accept an incoming external connection request (Ed25519-signed body) |
| `POST` | `/api/federation/message` | Receive and apply a message-create envelope (`file_refs`, public `images`, optional `reply_broadcast`); 409 `incomplete_file`, `assemble_failed`, or `parent_missing` |
| `POST` | `/api/federation/message/edit` | Receive and apply a message-edit envelope; JSON may include public `images` |
| `POST` | `/api/federation/message/delete` | Receive and apply a message-delete envelope from a connected instance |
| `POST` | `/api/federation/message/react` | Receive and apply a reaction add or remove using the target channel's reaction type |
| `POST` | `/api/federation/file/offer` | Announce a hashed file so the peer can skip or accept parts |
| `POST` | `/api/federation/file` | Receive one encrypted file part (raw bytes) |
| `POST` | `/api/federation/users` | Exchange user directory with a connected instance |
| `POST` | `/api/federation/teams` | Peer announces allowed Workspaces (`team_id` + name); this install heals stubs or records a pending claim while still live, and refreshes the primary Workspace name |
| `GET` | `/api/federation/ping` | Health check for connected instances (only when federation is on) |

Inbound message and reaction endpoints resolve the addressed `SyncChannel` as a target and apply the envelope through the same `apply_target` path as local fan-out. Thread creates include `target_ts` (the Slack ts on that Channel) so a reply can apply without looking the parent up again. Edits, deletes, and reactions need PostMeta on that SyncChannel. If that channel does not subscribe, the request is accepted without writing to Slack. Federation copies never become a new source or create another hop.

### Federation outbound (this instance → peer)

When a channel sync includes a federated workspace, this instance POSTs to the peer's federation endpoint and appends the resource subpath (`/pair`, `/message`, `/message/edit`, `/message/delete`, `/message/react`, `/users`, `/teams`, `/file/offer`, `/file`, and group/sync replication routes).

- Every outbound federation call is a POST. `GET /api/federation/ping` is inbound only: this instance answers a peer's ping but never sends one.
- The connection code's `webhook_url` is that full endpoint (this instance serves it at `/api/federation`). The mount path travels in the code and is not assumed by the sender.
- Connection codes are signed JSON (webhook URL, instance id, public key, optional connection `label` and `primary_team_id`, and `sig`). `primary_workspace_name` is display only and is not signed. The instance id is the SHA-256 fingerprint of the Ed25519 public key.
- Hosted Slack file bytes are offered and posted as raw parts (`/file/offer`, `/file`) before the envelope. Public GIF/image URLs still travel on the envelope. Parts are encrypted at rest until assembled and are omitted from backup and export.
- JSON federation bodies follow this hop's injected receive cap in [INFRA_CONTRACT.md](INFRA_CONTRACT.md) (`json_chunk_mb` on pair, file offer, and `/users`). Inbound file-part size uses the same contract (`file_chunk_mb` on offer). An older peer that omits `json_chunk_mb` is paged at 1 MiB.

## Subscribed Slack Events

| Event | Handler | Description |
|-------|---------|-------------|
| `app_home_opened` | `handle_app_home_opened` | Publishes the Home tab with workspace groups, channel syncs, and user mapping. When the per-user content hash is unchanged, republishes the cached blocks instead of rebuilding. |
| `app_uninstalled` | `handle_app_uninstalled` | Workspace uninstall: Bolt `InstallationStore.delete_all` (bot + every user install row), then pause groups and channel syncs. |
| `member_joined_channel` | `handle_member_joined_channel` | Detects when SyncBot is added to an unconfigured channel; posts a message and leaves. |
| `message.channels` / `message.groups` | `respond_to_message_event` | Fires on new messages, thread broadcasts, `/me`, edits, deletes, and file shares in public/private channels. |
| `reaction_added` / `reaction_removed` | `handle_reaction` | Syncs emoji reactions from publishing channels to subscribing targets; skips user-token echo events SyncBot applied on a target. |
| `team_join` | `handle_team_join` | Fires when a new user joins a connected workspace. Adds the user to the directory and re-checks unmapped user mappings. |
| `tokens_revoked` | `handle_tokens_revoked` | User-token revoke: Bolt `delete_installation` for that person, then republish Home. A `tokens.bot` array is treated as uninstall only when the stored bot token fails `auth.test`. |
| `user_profile_changed` | `handle_user_profile_changed` | Detects display name or email changes and updates the user directory and mappings. |
