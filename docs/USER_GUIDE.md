# SyncBot User Guide

This guide is for **workspace admins and people using SyncBot in Slack**. If you are installing or hosting the app (AWS, GCP, Docker, GitHub Actions), see **[DEPLOY.md](DEPLOY.md)** and the root **[README](../README.md)**.

## Getting Started

1. Click the install link from a desktop browser (make sure you have selected the correct workspace in the upper right).
2. Open the **SyncBot** app from the sidebar and click the **Home** tab. Everyone can open it. Workspace admins and owners configure Settings; they can also name extra managers who may create groups, Create Sync, and Join Sync without opening Settings.
3. The Home tab shows everything in one view:
   - **Authorize SyncBot** — at the top, when this person still needs to grant user permissions. See **Authorize SyncBot** below.
   - **SyncBot Configuration** — directly under that. **Refresh** is for everyone, so you can reload Home after revoking your authorization. **Settings** is for Slack admins on every installed workspace: extra managers and whether private Channels may be published here. Federation, retention, and the broadcast allow-list stay on the primary workspace (`PRIMARY_WORKSPACE` set and redeployed). **Backup/Restore** is also primary-only. If you do not see those instance options, ask the operator.
   - **Workspace Groups** — create or join groups of workspaces that can sync channels together (admins).
   - **Per-group sections** — for each group you can **Create Sync**, open **User Mapping** (a modal), and see or manage channel syncs inline. Other workspaces in the group see those syncs as **Available Sync Relationships** and can **Join Sync**. Create, Join, and Edit each offer a participation choice, described under **Sync Modes**.
   - **Synced Channels** — each row shows the local channel, then **Status** as Active or Paused with participation in parentheses (for example ``Active (Publish and Subscribe)``), **Members** as each partner `` `#channel (Workspace)` ``, with **Edit Sync**, **Pause Sync** / **Resume Sync**, and **Leave Sync**, a synced-since date, and a tracked message count.
   - **External Connections** *(when federation is enabled)* — Generate or Enter a Connection Code, and **Data Migration** (export workspace data to another instance, or import a migration file).

## Things to Know

- Workspace **admins and owners** open Settings, Backup/Restore, Reset Database, and External Connections. **Extra managers** (chosen in Settings) can create groups, Create Sync, and Join Sync, but they cannot open those admin-only screens. Everyone can still open the Home tab, authorize SyncBot, and use **Refresh**.
- Messages, threads, edits, deletes, reactions, images, videos, GIFs, and other hosted files (PDFs, audio, zips) are all synced **between workspaces on this instance**. Federation still carries public GIF/image URLs; Slack-hosted file bytes stay on the same instance.
- **@mentions and #channel links** in synced messages are rewritten for each target: mapped users are tagged with the local Slack user, and `#channel` mentions become a code-ticked `#name (Workspace)` (for example `` `#ao-bridge-to-nowhere (F3 T-Town Test)` ``). SyncBot does not turn those into the target twin channel, because the author named a place in the source workspace. Links to a specific source message keep a labeled link back to that message in the source workspace. Those links open the source message in the Slack mobile app; in Slack’s desktop browser they may appear as a private message in the other workspace. Unmapped people fall back to a code-ticked display name such as `` `Name (Workspace)` ``.
- Messages from other bots are synced; only SyncBot's own messages are filtered to prevent loops.
- Existing messages are not back-filled; syncing starts from the moment a channel is linked.
- Do not add SyncBot manually to channels. SyncBot adds itself when you Create Sync or Join Sync. If it detects it was added to an unconfigured channel, it posts a message and leaves automatically.
- When you pick a channel to create or join a Sync, SyncBot uses Slack's own channel search, so you can reach any channel in your workspace by typing a few letters. There is no limit on how many channels it can show.
- A channel may participate in more than one Channel Sync. This allows several published sources to feed one local channel (fan-in), and the same channel may be published in more than one group. A workspace cannot subscribe to the same published source twice; SyncBot reports that duplicate in the dialog. Do not reject a channel merely because it already participates in another Channel Sync.
- Public channels are supported out of the box. Private channels are only available if a Slack admin turned them on for **this** workspace in **Settings**; if they have not, SyncBot asks you to pick a public channel. When they are allowed, you create or join a Sync on a private channel the same way you would a public one, and SyncBot adds itself for you using your permission to invite it — see **Authorize SyncBot** below. If it cannot be added, that Channel Sync is undone and you get a direct message explaining why.

## Authorize SyncBot

Slack does not allow an app to add itself to a private channel. Only someone who is already in that channel can add it, acting as themselves. So the first time you use SyncBot, you may see an **Authorize SyncBot** section at the top of the Home tab with a short explanation, a list of the permissions it is asking for, and a button.

Clicking the button opens this SyncBot instance's own install page, which then sends you to Slack. The screen arrives with this workspace already selected, so if you belong to several you do not have to hunt for the right one. It takes a few seconds. It does not ask for any new permissions from your workspace beyond the list on the Home tab; it simply records that SyncBot may act on your behalf. When you click Allow, the Home tab updates on its own — you do not need to press Refresh — and the section disappears. Creating or joining a Sync on a private channel then works without extra steps, and target messages, files, and native reactions can appear as you in this workspace. Direct messages still come from the bot, not from you. You need to authorize in **each** workspace (including federated ones) where you want SyncBot to act as you. Starting from a slack.com link copied from elsewhere can fail after you click Allow; use the Home tab button.

If SyncBot later needs an additional permission, the section comes back. Permissions you already granted stay listed with checkmarks under **Already allowed permissions**, and only what is new appears under **Needed permissions**, so it is an update rather than starting over. The already-allowed list is omitted the first time, when nothing has been granted yet.

Everyone sees this section until they have granted every current permission, whether or not they are an admin. Whoever installed SyncBot originally will usually never see it, because that first install already stored their own permission. A colleague's authorization is not reused: SyncBot only invites itself into a private channel as the person who picked it, and only posts target messages, shares files, or adds a native reaction as the mapped person who authorized in that workspace. If you pick a private channel before authorizing, SyncBot tells you in the dialog and points you here rather than failing after the dialog closes.

### Revoke your authorization

This is personal: it only drops SyncBot's permission to act as *you*. It does not uninstall the app from the workspace, and it does not remove SyncBot from private channels it already joined. After you revoke, SyncBot can no longer invite itself into a private channel, post target messages or files, or add native reactions as you. Direct messages are unaffected because the bot sends them. **Authorize SyncBot** should come back on its own; if the Home tab still looks the same, click **Refresh** in **SyncBot Configuration** (just under Authorize). Use **Authorize SyncBot** again if you change your mind.

Slack owns this screen (there is no button for it on the Home tab). From the desktop app:

1. Click the workspace name in the sidebar, then **Tools & settings** → **Manage apps**.
2. Open **Installed Apps**, find **SyncBot**, and click **App Details**.
3. Open the **Configuration** tab.
4. Under **Authorizations**, find **Authorized members**, click **See all**, and click **Revoke** next to your own name.

Do not click **Remove App** on that same page unless you mean to uninstall SyncBot for the whole workspace. That is a different action: it pauses every group and channel sync, as described under **Uninstall / Reinstall** below.

On many workspaces, Slack's default is that any member except guests can open this list and revoke *other people* as well. That is a workspace setting, not something SyncBot can lock. Workspace owners should turn on approved apps so only owners and chosen app managers can do that — see **Security** below. Slack's own walkthrough is [Remove apps and custom integrations from your workspace](https://slack.com/help/articles/360003125231-Remove-apps-and-custom-integrations-from-your-workspace) (use the tab for removing a configuration or authorization, not for removing the app).

## Security

SyncBot cannot hide Slack's app Configuration page or decide who is allowed to revoke authorizations. That is controlled by the Slack workspace. By default, any member except guests can often install apps, uninstall them, and revoke other members' authorizations. For a community workspace, we recommend tightening that before you rely on **Authorize SyncBot** for private channels.

A **Workspace Owner** can do this from the desktop app:

1. Click the workspace name in the sidebar, then **Tools & settings** → **Manage apps**.
2. Open **App Management Settings** in the left sidebar.
3. Turn on **Approve apps** (some workspaces label this **Require approved apps**). Save.
4. Keep **App Managers** as Workspace Owners only, or add specific admins you trust. Do not leave every member able to manage apps.

Once approved apps are required, only Workspace Owners and the people you appointed as app managers can remove apps or revoke someone else's authorization. Members can still use **Authorize SyncBot** for themselves. They may need to request a new app (or new permissions) instead of installing freely, which is the usual tradeoff.

Slack documents this in [Manage app approval for your workspace](https://slack.com/help/articles/222386767-Manage-app-approval-for-your-workspace) and [Security recommendations for approving apps](https://slack.com/help/articles/360001670528-Security-recommendations-for-approving-apps).

## Workspace Groups

Workspaces must belong to the same **group** before they can sync channels or map users. Admins can create a new group (which generates an invite code) or join an existing group by entering a code. A workspace can be in multiple groups with different combinations of other workspaces.

### Group owners

Every group has at least one **owner** workspace. The workspace that creates a group is its first owner. Owners are the workspaces that can promote other owners and disband the group; everyone else is a member. Inviting another workspace stays open to any member, not just owners.

An owner can share that responsibility by clicking **Promote to Owner** next to another workspace in the group. There is no matching "demote" button for other workspaces — an owner can only step down itself, using **Give Up Ownership**, and only when another owner remains. That keeps one workspace from quietly taking a group over by demoting everyone else.

For the same reason, a group can never be left with no owner. If you are the only owner, SyncBot will not let your workspace leave the group until you have promoted another workspace to owner. It explains this instead of failing silently, so you know what to do next.

Uninstalling SyncBot does not hand your ownership to anyone else. Your membership is only paused, so reinstalling within the retention period gives you the group back exactly as it was. Ownership passes to another workspace only when your data is actually deleted, either because the retention period expired or because an operator purged it. In that case SyncBot promotes the longest-standing remaining member so the group is not stranded.

### Disbanding a group

An owner can **Disband Group** to remove a group entirely, along with its syncs and user mappings. Because this cannot be undone and affects other workspaces, SyncBot only offers it when your workspace is the sole owner *and* the sole publisher of every channel in the group. If another workspace owns the group or has published a channel into it, disbanding is declined with an explanation of who else is involved — ask them to Leave Sync or leave the group first, or just leave the group yourself instead.

Disbanding always asks for confirmation before anything is removed, and tells you how many workspaces, syncs, and channels it will affect. Note that the user mappings scoped to the group go with it, and those took Auto Map Now and manual edits to build, so re-creating the group later means mapping people again.

## Sync Modes

Use **Create Sync** to start a new relationship in a group. There is no separate workspace picker: the new Sync is available to the group, and each other workspace decides independently whether to **Join Sync**. There are no sync owners. If the last publisher leaves, the Sync ends (history is removed) and anyone in the group can Create Sync again.

**Create Sync** offers:

- **Publish only** — send this channel's new messages, files, edits, deletes, threads, and reactions to workspaces that join. This channel will not receive theirs. Copies arriving here do not start another hop.
- **Publish and Subscribe** — send and receive with workspaces that join.

**Join Sync** and **Edit Sync** also offer **Subscribe only** (receive without sending local activity) and the same **Publish only** / **Publish and Subscribe** choices. Join as Publish only is how a workspace sends into an existing Sync without receiving.

A Sync waiting for others to join is listed on Home by its channel name. A private channel is tagged `(private)`. Once another workspace joins, the row becomes a link to your local channel. **Create Sync**, **Join Sync**, and **Edit Sync** are each one screen: they name the Channel and Workspace Group, and they always offer Hybrid, Direct, or Off (that type is used while the Channel subscribes). A local channel may join several different published sources, so several channels can feed one place. SyncBot rejects joining the same workspace to the same published source twice.

## Reactions

Reactions follow the same publishing and subscribing participation as messages and files. A reaction originates only from a channel that publishes and is applied only to channels that subscribe; a synced copy never starts another hop. Each subscribing channel chooses one reaction type, and new Create Sync and Join Sync flows default to **Hybrid**.

- **Hybrid** — try a native reaction first; if that person has not authorized (or their permission there is no longer valid), SyncBot posts a short thread notice instead. Custom emoji the other workspace does not have are skipped, even if SyncBot would otherwise post a thread notice.
- **Direct** — native emoji on the synced message, as the mapped person in that workspace. That person must have clicked **Authorize SyncBot** there. Custom emoji the other workspace does not have are skipped.
- **Off** — do not apply incoming reactions in this workspace, including later unreacts. Messages and files still sync. Turning Off later does not remove reactions that already landed, whether those were native emoji or Hybrid thread notices.

On a Hybrid or Direct target, removing a reaction removes that person's native emoji when they have authorized SyncBot there, and deletes their Hybrid thread notices (including notices that were reactions to those notices). Deleting a Hybrid notice in one workspace only removes it there — other workspaces and the original native reaction stay. Each person's notices are independent; human replies under a notice are not deleted. Reactions are never written back into the channel where they started.

## Pause / Resume / Leave

- **Pause Sync / Resume Sync** — Individual channel syncs can be paused and resumed without losing configuration. SyncBot asks you to confirm. Paused channels do not sync any messages, threads, or reactions.
- **Leave Sync** — Removes this workspace's channel from the Sync and deletes this workspace's tracking history. Other workspaces continue. SyncBot asks you to confirm.
- **Last publisher** — If you are the last publisher, Leave Sync warns that this ends the Sync for everyone and deletes Sync history. Anyone in the group can Create Sync later. Slack messages stay; only SyncBot's tracking history is removed.
- **Channel notices** — When you Create Sync, SyncBot posts in that Channel: who created it, and whether messages will be one-way or two-way. When another workspace joins, it posts who subscribed which Channel, plus a second sentence for one-way vs two-way.

## Uninstall / Reinstall

If a workspace uninstalls SyncBot, group memberships and syncs are paused (not deleted), and every stored bot and user token for that workspace is removed. Reinstalling within the retention period (default 30 days, which the operator can change in **Settings**) automatically restores groups and channel syncs, including group ownership. People who had clicked **Authorize SyncBot** will need to do that again for private-channel invitations and for target messages, files, and native reactions to appear as them. Group members are notified via DMs and channel messages.

## User Mapping

Admins open **User Mapping** from a group on the Home tab; it opens as a modal with the mappings already saved in SyncBot. Unmapped people appear first; use **Edit** and Slack’s native user picker to map someone by hand. **Auto Map Now** compares emails (and unique display names) in the member directory and writes a mapping whenever exactly one person in the other workspace matches — it does not crawl Slack’s full member list. While it runs, the button is replaced with **Mapping users...**; when it finishes, the list and a last-run line update in the same modal (for example, “Last run on September 2, 2026 with 20 new found”). **0 new found** means this run found nothing new in the current directory data, not that every person is mapped. **Refresh List** reloads the list from the database and brings **Auto Map Now** back if the modal stuck on Mapping users... after a timeout. Incomplete lists usually mean the directory is still filling in (for example after a join). The first synced message or reaction from an unmapped author can also create a mapping from that person’s directory email, or one target `users.lookupByEmail` if the directory has no unique hit; **Auto Map Now** still fills in everyone else. In synced messages, a mapped author appears with their **local** display name and profile photo (no workspace suffix in the author line); an unmapped author uses the remote display name and photo, with the source workspace in parentheses. The same applies to messages delivered over **External Connections** (cross-instance federation). In message text, a mapped user is mentioned with a normal `@` tag in the receiving workspace; unmapped users appear as a code-ticked display name such as `` `Name (Workspace)` `` (same style as the file-share notice). Channel names that point at another synced channel in the same sync group are shown as native `#channel` links in each workspace.

When you **Create Sync**, SyncBot posts a short notice in that Channel right away, even if nobody has joined yet. The second sentence says whether this Channel will send only, or send and receive. When another workspace uses **Join Sync**, SyncBot posts in both Channels who joined and whether messages are one-way or two-way.

## Refresh Behavior

The Home tab has a **Refresh** button in **SyncBot Configuration** for everyone, not only admins. To keep API usage low, repeated clicks with no data changes are handled lightly: a 60-second cooldown applies, and when nothing has changed the app reuses cached content and shows "No new data. Wait __ seconds before refreshing again." User Mapping’s **Refresh List** only reloads that modal from saved mappings.

## Media Sync

On the **same instance**, Slack-hosted files of any type (photos, video, PDFs, audio, zips, and so on) are downloaded from the source and uploaded to each target channel. GIFs from the Slack GIF picker or GIPHY stay public image blocks. File shares post as SyncBot with a notice that names the original author in code ticks — for example `` `Ada Lovelace` shared a file `` — never as an @mention. That notice is the same for a caption-only share (file on the same message) and for a share that also has text (file in a thread under the text). When the text is a top-level channel post, the threaded file notice is also sent to the channel.

App posts that use Block Kit (for example a Slackblast preblast) sync from the layout blocks, so line breaks and emoji stay intact. Slack's "Show more" control is only how the client folds a long message; SyncBot does not stop at the preview. Buttons that belong to the source app (Edit this preblast, and similar) are not copied, because they would not work in the other workspace.

**External Connections** still sync those public GIF/image URLs on new posts, threads, and edits. Slack-hosted file bytes (a private PDF or photo) do not cross federation yet.

| Source message | What appears in target workspace |
|---|---|
| Text only | Single message with text, shown under the original poster's name and avatar |
| GIF (Slack picker / GIPHY) | Single message with the GIF embedded inline via image block, under the poster's name |
| GIF + text | Single message with text and GIF together, under the poster's name |
| File only (no text) | Single file upload as SyncBot with `` `Display Name` shared a file `` |
| Text + file | Text message under the poster's name, then the file in a thread reply with the same `` `Display Name` shared a file `` notice (also sent to the channel when the text was a top-level post) |
| Multiple files | Same as the matching row above; all files go in one upload |

## External Connections

*(Opt-in — enable **Federation** in Settings on the primary workspace)*

Workspaces running their own SyncBot deployment can be connected via the "External Connections" section on the Home tab. One admin generates a connection code and shares it out-of-band; the other admin enters it. The code is signed, so the webhook URL cannot be swapped in transit. Each instance identifies itself with a fingerprint of its signing key, not a UUID you set at deploy time. Messages, edits, deletes, reactions, and user mapping work across instances. Public GIF/image URLs travel with new posts, threads, and edits; Slack-hosted file bytes do not. The receiving SyncBot instance rewrites `@` mentions and `#` channel links using the same rules as same-instance sync (native tags when mapped / synced, fallbacks otherwise).

**Data Migration** in the same section lets you export your workspace data (syncs, channels, post meta, user directory, user mappings) for moving to another instance, or import a migration file after connecting. See [Backup and Migration](BACKUP_AND_MIGRATION.md) for details.

## Backup / Restore

**Backup/Restore** appears on the Home tab only when the operator has set `PRIMARY_WORKSPACE` to this workspace’s Slack Team ID (env, SAM, Terraform, or GitHub variable) and **redeployed**. When it is unset, backup is hidden everywhere.

Use it to download a full-instance backup (all durable tables as JSON) or restore from a backup file. Intended for disaster recovery (e.g. before rebuilding AWS). See [Backup and Migration](BACKUP_AND_MIGRATION.md).
