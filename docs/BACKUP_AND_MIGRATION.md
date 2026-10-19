# Backup, Restore, and Data Migration

## Full-Instance Backup and Restore

**`PRIMARY_WORKSPACE`** must be set to a Slack Team ID for backup and restore to show up. When it is set, the **Backup/Restore** button appears only in that workspace. When it is unset, backup and restore are hidden everywhere.

Use **Backup/Restore** on the Home tab (next to Refresh) to:

- **Download backup** — Generates a JSON file of the durable tables (workspaces, groups, syncs, channels, post meta, user directory, user mappings, instances, instance Settings, federation allowlists and pairing rows). Ephemeral Slack `event_id` claims, user-token echo rows, and in-flight federation file parts are not included. The file is sent to your DM. The backup includes an HMAC for integrity, a hash of the encryption key, and the SyncBot version that produced it (a label only; restore does not require that version to match). **Use the same `DATA_ENCRYPTION_KEY` on the target instance** so restored bot and user tokens decrypt; otherwise workspaces must reinstall the app to re-authorize.
- **Restore from backup** — Paste the backup JSON in the modal and submit. Restore is meant for an **empty or fresh database** (for example after a rebuild). If the encryption key hash or HMAC does not match, you will see a warning and can still proceed (for example if you edited the file on purpose).

After restore, Home tab caches are cleared so the next Refresh shows current data.

## Reset Database

Setting **`ENABLE_DB_RESET=true`** (with `PRIMARY_WORKSPACE` matching the current workspace) shows a **Reset Database** button on the Home tab. This is an advanced, destructive feature: it drops and reinitializes the entire database. The deploy scripts do not prompt for it. Set it in the env file (AWS `--setup-github` copies it when present) or in SAM / Terraform (`EnableDbReset` / `enable_db_reset`).

## Workspace Data Migration

**Data Migration** is on the SyncBot Configuration row for Slack admins. **Export** and **Import** are always there. **Export and Request Connection** only appears when Federation is on in Settings.

- **Export** — Download this Workspace's SyncBot data (syncs, sync channels, post meta, user directory, user mappings; no tokens, instance private key, or in-flight file parts) as a JSON file in your DM. The export modal stays open with Close while SyncBot DMs the file. Post meta covers every Channel in those Syncs, including copies on other Workspaces, so replies, edits, and reactions on older messages still find the twin after import. Channel names and other Workspace names travel with the file so Home can show `#name` instead of a Slack id. Use this when the other instance is already connected, or you only need the file. The file records the SyncBot version that produced it (a label only; import does not require that version to match) and is signed with this instance's Ed25519 key so the destination can detect tampering without sharing `DATA_ENCRYPTION_KEY`.
- **Export and Request Connection** — Same JSON file, and a request to the primary Workspace admins for a one-time connection code bundled with the export. Use this when you are moving this Workspace to a new SyncBot instance that is not connected yet. They get a DM with **Approve and Create** (opens the same Create Connection form: name the connection and pick Workspaces; the requesting Workspace is not on that list) or **Decline**. Creating the connection is slow because the export is signed then. The modal stays open with Close, and SyncBot DMs them the code. You get the file after they create. The waiting connection then appears under External Connections on the primary Home tab (Show, Edit, Cancel).
- **Import** — Upload a migration JSON file. SyncBot shows a review of the Groups, Mapped Users, Synced Channels, Synced Messages, and (when the file includes a connection code) the same connection details as Join. Import after that review. The modal stays open with Close, and SyncBot DMs you when import finishes (or if it fails or expires). If you are not yet connected, it joins using the bundled code, then merges Groups and Syncs by `uid`. If the code was already used, it still attaches this Workspace to the existing connection. A regular Export (no connection code) still attaches other Workspaces and their PostMeta when this instance already has exactly one External Connection. Other group members and their Channels are also filled in by the connected instance (on pair and keep-warm). Historical PostMeta for those Channels comes from the file, not from keep-warm. Import cannot invent a twin that was never recorded. User mappings are imported where both workspaces exist on the new instance. If the signature check fails, a warning is shown on that review; you can still Import.

After import, Home tab caches for that workspace are cleared. SyncBot also rejoins this Workspace's public synced Channels (pause notice, join, resume notice) and leaves private Channels paused with a pause notice. Twin Channels on Workspaces that stayed on the original instance also get a resume notice.

### Stub heal when a team is still live here

When a peer connects and sends a `team_id` that is still installed on this SyncBot, this install does **not** strip the live workspace. It records a pending stub claim, DMs admins once, and blocks remote federation traffic for that live team until SyncBot is uninstalled here. After uninstall, the pending claim converts the row into a stub for the trusted peer. Leave Connection pauses those remote Workspaces for the same retention window instead of deleting them; reconnecting, reinstalling on this instance, or importing that Workspace's migration file restores the row and keeps PostMeta. See [ARCHITECTURE.md](ARCHITECTURE.md) for details.
