# SyncBot User Guide

This guide is for **workspace admins and people using SyncBot in Slack**. If you are installing or hosting the app (AWS, GCP, Docker, GitHub Actions), see **[DEPLOY.md](DEPLOY.md)** and the root **[README](../README.md)**.

## Getting Started

1. Click the [install link](https://syncbot-prod-827676898321.us-central1.run.app/slack/install) from a desktop browser (make sure you have selected the correct workspace in the upper right).
2. Open the **SyncBot** app from the sidebar and click the **Home** tab. Everyone can open it. Workspace admins and owners configure Settings; they can also name extra managers who may create groups, Create Sync, and Join Sync without opening Settings.
3. The Home tab shows everything in one view:
   - **Authorize SyncBot** — at the top, when this person still needs to grant user permissions. See **Authorize SyncBot** below.
   - **SyncBot Configuration** — directly under Authorize.
     - **Refresh** is for everyone (including after you revoke your authorization).
     - **Settings** is for Slack admins on every installed workspace: extra managers and whether private Channels may be published here. Federation, retention, and the Workspace Block List stay on the primary workspace (`PRIMARY_WORKSPACE` set and redeployed). The block list is Slack Team IDs (for example `T0123456789`). A listed Workspace is uninstalled and cannot Add to Slack again until you remove the ID; removing it does not reinstall for them. You cannot block the primary Workspace, or a Workspace that still owns a group, until you promote another Owner. The bottom of Settings lists this install's version and primary Workspace. On the primary Workspace it also shows log level, fingerprint, public URL, and database.
     - **Backup/Restore** is primary-only. **Data Migration** is on that same row for Slack admins (export or import this Workspace's SyncBot data). If you do not see those instance options, ask the operator.
   - **Workspace Groups** — create or join groups of workspaces that can sync channels together (admins).
   - **Per-group sections** — for each group you can **Create Sync**, open **User Mapping** (a modal), and see or manage channel syncs inline. Other workspaces in the group see those syncs as **Available Sync Relationships** and can **Join Sync**. Create, Join, and Edit each offer a participation choice, described under **Create Sync and Join Sync**.
   - **Synced Channels** — each row shows the local channel, then **Status** as Active or Paused with participation in parentheses (for example ``Active (Publish and Subscribe)``), **Members** as each partner `` `#channel (Workspace)` ``, with **Edit Sync**, **Pause Sync** / **Resume Sync**, and **Leave Sync**, a synced-since date, and a tracked message count.
   - **External Connections** *(when federation is enabled)* — **Create External Connection** or **Join External Connection**. A created connection appears as a row (Trust Status `Waiting`, Local Workspaces, Remote Workspaces) before the other side joins, with **Show Connection Code**, **Edit Connection**, and **Cancel Connection**. After they join: **Edit Connection**, **Verify Trust**, or **Leave Connection**.

## Things to Know

- Workspace **admins and owners** open Settings, Backup/Restore, Reset Database, and External Connections. **Extra managers** (chosen in Settings) can create groups, Create Sync, and Join Sync, but they cannot open those admin-only screens. Everyone can still open the Home tab, authorize SyncBot, and use **Refresh**.
- Messages, threads, edits, deletes, reactions, images, videos, GIFs, and other hosted files (PDFs, audio, zips) are all synced between workspaces on this instance and across External Connections the same way.
- **@mentions and #channel links** in synced messages are rewritten for each target: mapped users are tagged with the local Slack user, and `#channel` mentions become a code-ticked `#name (Workspace)` (for example `` `#announcements (Workspace A)` ``). SyncBot does not turn those into the target twin channel, because the author named a place in the source workspace. Links to a specific source message keep that URL. A pasted Slack message becomes `message in #channel (Workspace)`; if the author already labeled the link, that text stays. Unmapped people fall back to a code-ticked display name such as `` `Name (Workspace)` ``.
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

For the same reason, a group can never be left with no owner by choice. If you are the only owner and other workspaces are still in the group, SyncBot will not let your workspace leave until you have promoted another workspace to owner. It explains this instead of failing silently, so you know what to do next. If no other workspace has joined yet, Home shows **Disband Group** instead of **Leave Group**. Disband is also offered when you are the sole owner *and* the only workspace publishing a Channel into the group.

Uninstalling SyncBot does not hand your ownership to anyone else. Your membership is only paused, so reinstalling within the retention period gives you the group back exactly as it was. Other workspaces still see the group, with no owner listed. After that window, if another workspace is still in the group, SyncBot promotes the longest-standing remaining member. If nobody is left, the group is disbanded. The primary workspace can join or be invited like any other workspace; it is not added or made owner just because it is primary.

### Disbanding a group

An owner can **Disband Group** to remove a group entirely, along with its syncs and user mappings. Because this cannot be undone and affects other workspaces, SyncBot only offers it when your workspace is the sole owner *and* the sole publisher of every channel in the group. If another workspace owns the group or has published a channel into it, disbanding is declined with an explanation of who else is involved — ask them to Leave Sync or leave the group first, or just leave the group yourself instead.

Disbanding always asks for confirmation before anything is removed, and tells you how many workspaces, syncs, and channels it will affect. Note that the user mappings scoped to the group go with it, and those took Auto Map Now and manual edits to build, so re-creating the group later means mapping people again.

## Create Sync and Join Sync

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

- **Pause Sync / Resume Sync** — Individual channel syncs can be paused and resumed without losing configuration. SyncBot asks you to confirm. A paused Channel does not send or receive messages, threads, or reactions.
- **Leave Sync** — Removes this workspace's channel from the Sync and deletes this workspace's tracking history. Other workspaces continue. SyncBot asks you to confirm.
- **Last publisher** — If you are the last publisher, Leave Sync warns that this ends the Sync for everyone and deletes Sync history. Anyone in the group can Create Sync later. Slack messages stay; only SyncBot's tracking history is removed.
- **Channel notices** — When you Create Sync, SyncBot posts in that Channel: who created it, and whether messages will be one-way or two-way. When another workspace joins, it posts who subscribed which Channel, plus a second sentence for one-way vs two-way.

## Uninstall / Reinstall

If a workspace uninstalls SyncBot, group memberships and syncs are paused (not deleted), and every stored bot and user token for that workspace is removed. Reinstalling within the retention period (default 30 days, which the operator can change in **Settings**) automatically restores groups and channel syncs, including group ownership, unless that Team ID is on the Workspace Block List. The same restore runs if that Workspace had become a remote stub (Leave Connection, or a move to another instance): reinstalling here, or importing that Workspace's migration file, brings it back as a live install and keeps message history. Public Channels are paused with a notice, SyncBot joins them again, then they resume with a notice. Twin Channels on Workspaces that stayed on the original instance also get a resume notice after import. Private Channels stay paused, with a pause notice in that Channel when SyncBot can post there (or on the twin Channel, and a DM, when it cannot). People who had clicked **Authorize SyncBot** will need to do that again, then Resume Sync, before a private Channel is added. Group members are notified via DMs and channel messages.

## User Mapping

Admins open **User Mapping** from a group on the Home tab. It opens as a modal with the mappings already saved in SyncBot. Unmapped people appear first.

- **Edit** uses Slack’s native user picker to map someone by hand.
- **Auto Map Now** compares emails (and unique display names) in the member directory and writes a mapping whenever exactly one person in the other workspace matches. It does not crawl Slack’s full member list. While it runs, the modal asks you to wait until it finishes. When it finishes, the list and a last-run line update in the same modal (for example, “Last run on September 2, 2026 with 20 new found”). **0 new found** means this run found nothing new in the current directory data, not that every person is mapped.
- **Refresh List** reloads the list from the database. Incomplete lists usually mean the directory is still filling in (for example after a join).
- The first synced message or reaction from an unmapped author can also create a mapping from that person’s directory email, or one target `users.lookupByEmail` if the directory has no unique hit. **Auto Map Now** still fills in everyone else.

In synced messages (same-instance and External Connections):

- A mapped author appears with their **local** display name and profile photo (no workspace suffix in the author line). An unmapped author uses the remote display name and photo, with the source workspace in parentheses.
- A mapped user in the text is a normal `@` tag. An unmapped user is a code-ticked `` `Name (Workspace)` ``.
- `#channel` mentions stay source-side as a code-ticked `` `#name (Workspace)` ``. SyncBot does not turn them into the target twin channel.

## Refresh Behavior

The Home tab has a **Refresh** button in **SyncBot Configuration** for everyone, not only admins. It rebuilds this Home tab when something has changed, then refreshes External Connection allowlists (the same pulse keep-warm uses). When nothing has changed, Refresh does nothing. After a deploy, remembered Home tabs update on their own; you should not need to click Refresh twice to wake the app. User Mapping’s **Refresh List** only reloads that modal from saved mappings.

## Media Sync

On the **same instance**, Slack-hosted files of any type (photos, video, PDFs, audio, zips, and so on) are downloaded from the source and uploaded to each target channel, including files shared in a thread and also-send-to-channel replies. Slack allows files up to 1 GB.

- Slack often uploads the file a moment before it shares it into the Channel. SyncBot still copies that share.
- If a file cannot be copied, SyncBot DMs you with the error. The usual Free-plan stop is a workspace whose file storage is full.
- Slack-hosted video and image blocks are not copied as players. The file itself is uploaded so the other workspace can play it.
- GIFs from the Slack GIF picker or GIPHY stay public image blocks.
- When the mapped author has authorized SyncBot in the target workspace, the share posts as that person. The file is the message. A caption, Block Kit, or a public GIF stays on that same message.
- When they have not authorized, the share still posts as one message, with the same from line as other SyncBot posts: the author's name and photo, plus the workspace name when they are not mapped. A file with no text is just the file.

App posts that use Block Kit (for example a form) sync from the layout blocks, so line breaks and emoji stay intact. Slack's "Show more" control is only how the client folds a long message; SyncBot does not stop at the preview. Buttons that belong to the source app (Edit this post, and similar) are not copied, because they would not work in the other workspace.

**External Connections** use the same apply path. Origin posts file parts to the peer first, then the envelope. Those parts stay in the database only while the transfer is in flight (encrypted at rest) and are not included in backup or export.

| Source message | What appears in target workspace |
|---|---|
| Text only | Single message with text, shown under the original poster's name and avatar |
| GIF (Slack picker / GIPHY) | Single message with the GIF embedded inline via image block, under the poster's name |
| GIF + text | Single message with text and GIF together, under the poster's name |
| File only (no text) | One message that is just the file, under the same from line as a text post |
| Text + file | One message with the caption (and Block Kit or a public GIF, when the source had them) and the file together |
| Multiple files | Same as the matching row above; all files go in one upload |

## External Connections

*(Opt-in — enable **Federation** in Settings on the primary workspace)*

Workspaces running their own SyncBot can connect from **External Connections** on the Home tab, using the same Create / Join language as Channel Syncs. Each instance identifies itself with a fingerprint of its signing key, not a UUID you set at deploy time. Trust is that fingerprint. The name is a label only, and a used connection code is not reused.

**Create External Connection** (primary-workspace admin):

1. Name the connection and pick which of this instance's Workspaces the other SyncBot may see. The primary Workspace does not have to be on that list.
2. SyncBot shows the signed connection code in the modal and DMs a copy (24 hours). The code is signed, so the webhook URL and primary Team ID cannot be swapped in transit. Creating the code can be slow (especially **Approve and Create**, which also signs the migration file). The modal stays open with Close, and SyncBot DMs you when the code is ready.
3. Home lists that connection immediately: Trust Status `Waiting`, Local Workspaces as you selected, Remote Workspaces `None yet`. **Show Connection Code**, **Edit Connection**, and **Cancel Connection** sit on that row. After 24 hours without a join, the waiting row drops off.

**Join External Connection:** paste the code, review the name, primary Workspace (display only), Team ID, URL, and fingerprint, pick your own allowed Workspaces, and click **Join**.

Each connected row shows **Trust Status**, **Local Workspaces**, and **Remote Workspaces** as code ticks.

- **Edit Connection** can rename the connection and change the local Workspaces allowed for that peer (or, on a waiting offer, the Workspaces this instance will share after they join). A Workspace that owns a group with members on this connection must **Give Up Ownership** before it can be removed. After they are removed, the other side's Remote Workspaces update on their next Home open or Refresh. That paused stub stays for the Settings retention window (default 30 days) unless it is allowed again.
- After they join, this instance pushes the allowlist on pair, on Edit, and on keep-warm (every 5 minutes) so the other side's Remote Workspaces and primary Workspace name stay current. Keep-warm also pushes Groups and Sync Channels for Workspaces on that allowlist so a Workspace that imported its own data is not left waiting for those members to join.
- If Trust Status is `Untrusted` (for example after the other instance reinstalled with a new key), **Verify Trust** shows the primary Workspace, Team ID, URL, fingerprint, and remote Workspaces so you can confirm them out of band before trusting again.
- **Leave Connection** asks for confirmation, then pauses remote Workspaces on this instance (same retention window as uninstall, default 30 days). Reconnecting that External Connection, or reinstalling SyncBot on the same Workspace, restores groups, Syncs, and message history. After the retention period, that data is deleted. The other instance still lists this connection until they leave too.
- **Cancel Connection** on a waiting offer deletes the unused code before anyone joins.
- There is no Pause/Resume on the connection itself. Turning Federation off in Settings stops new traffic without leaving. Untrusted already blocks inbound delivery.

Messages, edits, deletes, reactions, hosted files, and user mapping work across instances.

- Public GIF/image URLs travel with new posts, threads, and edits.
- Private Slack file bytes are copied over a signed file offer/parts channel before the message envelope.
- The receiving instance rewrites `@` mentions and `#` channel links using the same rules as same-instance sync (native `@` tags when mapped; `#channel` stays a source code-tick).
- If you move an instance to a new hostname but keep the same signing key, create a fresh connection code. The peer updates the URL without requiring a new trust decision.

**Data Migration** on SyncBot Configuration lets any workspace admin export or import this Workspace's SyncBot data. See [Backup and Migration](BACKUP_AND_MIGRATION.md) for the Export, Export and Request Connection, and Import steps.

## Backup / Restore

**Backup/Restore** appears on the Home tab only when the operator has set `PRIMARY_WORKSPACE` to this workspace’s Slack Team ID (env, SAM, Terraform, or GitHub variable) and **redeployed**. When it is unset, backup is hidden everywhere.

Use it to download a full-instance backup (all durable tables as JSON) or restore from a backup file. Intended for disaster recovery (for example after a rebuild). See [Backup and Migration](BACKUP_AND_MIGRATION.md).
