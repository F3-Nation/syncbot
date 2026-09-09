"""Channel sync form builders."""

import logging

import helpers
from builders._common import (
    _format_channel_ref,
)
from db import DbManager
from db.schemas import PostMeta, Sync, SyncChannel, Workspace, WorkspaceGroup, WorkspaceGroupMember
from slack import actions, orm
from slack.blocks import (
    context as block_context,
)
from slack.blocks import (
    section,
)

_logger = logging.getLogger(__name__)


def _format_other_channel_ref(channel, workspace, *, paused: bool) -> str:
    """One other Channel on the Home Members line: code-ticked ``#name (Workspace)``."""
    raw_name, _is_private = helpers.lookup_channel_meta(channel.channel_id, workspace)
    label = f"#{str(raw_name or channel.channel_id).removeprefix('#')}"
    ws_name = helpers.resolve_workspace_name(workspace) if workspace else f"Workspace {channel.workspace_id}"
    ref = helpers.code_ticked_display_name(label, ws_name)
    if paused:
        return f"{ref} `(Paused)`"
    return ref


def _available_channel_label(channel_id: str | None, workspace, title: str | None) -> str:
    """Name of a published channel on Home, with `` (private)`` when it is private.

    ``sync.title`` used to be the Slack Channel ID when the bot looked up the
    name before it had joined a private Channel. Ask the publisher workspace
    now that the bot is in it. The Home context wraps this in backticks, same
    as From and Channel — no hash, no emoji.
    """
    fallback = (title or channel_id or "Unknown").removeprefix("#")
    if channel_id and workspace:
        name, is_private = helpers.lookup_channel_meta(channel_id, workspace)
        if name == channel_id:
            name = fallback
    else:
        name, is_private = fallback, False
    name = str(name).removeprefix("#")
    if is_private:
        return f"{name} (private)"
    return name


def _build_inline_channel_sync(
    blocks: list,
    group: WorkspaceGroup,
    workspace_record: Workspace,
    other_members: list[WorkspaceGroupMember],
    context: dict | None = None,
) -> None:
    """Append channel-sync blocks inline under a group on the Home tab.

    Shows:
    - Active synced channels with Pause/Resume and Leave Sync
    - Paused synced channels with Resume/Leave Sync
    - Channels waiting for others to join with Leave Sync
    - Available syncs from other members with Join Sync
    """
    syncs_for_group = DbManager.find_records(
        Sync,
        [Sync.group_id == group.id],
    )

    published_syncs: list[tuple[Sync, SyncChannel, list[SyncChannel], bool]] = []
    waiting_syncs: list[tuple[Sync, SyncChannel]] = []
    available_syncs: list[tuple[Sync, list[SyncChannel]]] = []

    for sync in syncs_for_group:
        channels = DbManager.find_records(
            SyncChannel,
            [SyncChannel.sync_id == sync.id, SyncChannel.deleted_at.is_(None)],
        )
        my_channel = next((c for c in channels if c.workspace_id == workspace_record.id), None)
        other_channels = [c for c in channels if c.workspace_id != workspace_record.id]

        if my_channel and other_channels:
            is_paused = my_channel.status == "paused"
            published_syncs.append((sync, my_channel, other_channels, is_paused))
        elif my_channel and not other_channels:
            waiting_syncs.append((sync, my_channel))
        elif not my_channel and other_channels:
            if sync.sync_mode == "direct" and sync.target_workspace_id != workspace_record.id:
                continue
            publishers = [c for c in other_channels if helpers.channel_publishes(c)]
            if not publishers:
                continue
            available_syncs.append((sync, other_channels))

    published_syncs.sort(key=lambda t: (t[0].title or "").lower())
    waiting_syncs.sort(key=lambda t: (t[0].title or "").lower())
    available_syncs.sort(key=lambda t: (t[0].title or "").lower())

    if not published_syncs and not waiting_syncs and not available_syncs:
        return

    blocks.append(section("*Synced Channels*"))

    for sync, my_ch, other_chs, is_paused in published_syncs:
        my_ref = _format_channel_ref(my_ch.channel_id, workspace_record, is_local=True)

        other_refs: list[str] = []
        for other_channel in other_chs:
            other_ws = helpers.get_workspace_by_id(other_channel.workspace_id, context=context)
            other_refs.append(
                _format_other_channel_ref(
                    other_channel,
                    other_ws,
                    paused=other_channel.status == "paused",
                )
            )

        if is_paused:
            icon = ":double_vertical_bar:"
            toggle_btn = orm.ButtonElement(
                label=":arrow_forward: Resume Sync",
                action=f"{actions.CONFIG_RESUME_SYNC}_{sync.id}",
                value=str(sync.id),
            )
        else:
            icon = ":arrows_counterclockwise:"
            toggle_btn = orm.ButtonElement(
                label=":double_vertical_bar: Pause Sync",
                action=f"{actions.CONFIG_PAUSE_SYNC}_{sync.id}",
                value=str(sync.id),
            )

        blocks.append(section(f"{icon} {my_ref}"))

        context_parts: list[str] = []
        if is_paused:
            status_tag = "Paused"
        else:
            status_tag = "Active"
        participation = helpers.participation_label(my_ch)
        context_parts.append(f"Status: `{status_tag} ({participation})`")

        if other_refs:
            context_parts.append(f"Members: {', '.join(other_refs)}")

        if getattr(my_ch, "created_at", None):
            context_parts.append(f"Synced Since: `{my_ch.created_at:%B %d, %Y}`")

        msg_count = DbManager.count_records(
            PostMeta,
            [PostMeta.sync_channel_id == my_ch.id],
        )
        context_parts.append(f"Messages Tracked: `{msg_count}`")

        if context_parts:
            blocks.append(block_context("\n".join(context_parts)))
        teardown_btn = orm.ButtonElement(
            label=":octagonal_sign: Leave Sync",
            action=f"{actions.CONFIG_LEAVE_SYNC}_{sync.id}",
            value=str(sync.id),
            style="danger",
        )
        edit_btn = orm.ButtonElement(
            label=":pencil2: Edit Sync",
            action=f"{actions.CONFIG_EDIT_SYNC}_c_{my_ch.id}",
            value=f"c:{my_ch.id}",
        )
        blocks.append(orm.ActionsBlock(elements=[edit_btn, toggle_btn, teardown_btn]))

    for sync, my_ch in waiting_syncs:
        publishers = [c for c in [my_ch] if helpers.channel_publishes(c)]
        if publishers:
            blocks.append(section(f":outbox_tray: <#{my_ch.channel_id}> — _waiting for others to join_"))
            edit_btn = orm.ButtonElement(
                label=":pencil2: Edit Sync",
                action=f"{actions.CONFIG_EDIT_SYNC}_c_{my_ch.id}",
                value=f"c:{my_ch.id}",
            )
            teardown_btn = orm.ButtonElement(
                label=":octagonal_sign: Leave Sync",
                action=f"{actions.CONFIG_LEAVE_SYNC}_{sync.id}",
                value=str(sync.id),
                style="danger",
            )
            blocks.append(orm.ActionsBlock(elements=[edit_btn, teardown_btn]))
        else:
            blocks.append(
                section(f":outbox_tray: <#{my_ch.channel_id}> — _no publishers remaining; this Sync has ended_")
            )
            teardown_btn = orm.ButtonElement(
                label=":octagonal_sign: Leave Sync",
                action=f"{actions.CONFIG_LEAVE_SYNC}_{sync.id}",
                value=str(sync.id),
                style="danger",
            )
            blocks.append(orm.ActionsBlock(elements=[teardown_btn]))

    if available_syncs:
        blocks.append(section(":inbox_tray: Available Sync Relationships"))
    for sync, other_chs in available_syncs:
        publishers = [c for c in other_chs if helpers.channel_publishes(c)]
        pub_ch = publishers[0] if publishers else None
        publisher_ws = helpers.get_workspace_by_id(pub_ch.workspace_id, context=context) if pub_ch else None
        publisher_name = helpers.resolve_workspace_name(publisher_ws) if publisher_ws else "another Workspace"
        channel_label = _available_channel_label(pub_ch.channel_id if pub_ch else None, publisher_ws, sync.title)

        card_context = f"From: `{publisher_name}`\nChannel: `{channel_label}`"
        blocks.append(block_context(card_context))
        row_buttons: list[orm.ButtonElement] = []
        duplicate_source = bool(
            pub_ch
            and helpers.already_subscribed_to_source(
                workspace_id=workspace_record.id,
                source_workspace_id=pub_ch.workspace_id,
                source_channel_id=pub_ch.channel_id,
            )
        )
        if duplicate_source:
            blocks.append(block_context("_Already subscribed to this published Channel through another sync._"))
        else:
            row_buttons.append(
                orm.ButtonElement(
                    label=":inbox_tray: Join Sync",
                    action=f"{actions.CONFIG_JOIN_SYNC}_{sync.id}",
                    value=str(sync.id),
                )
            )
        if row_buttons:
            blocks.append(orm.ActionsBlock(elements=row_buttons))
