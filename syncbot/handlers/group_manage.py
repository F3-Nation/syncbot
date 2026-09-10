"""Group management handlers — leave group with confirmation."""

import logging
from logging import Logger

from slack_sdk.web import WebClient

import builders
import helpers
from db import DbManager, schemas
from slack import actions, orm

_logger = logging.getLogger(__name__)


def handle_leave_group(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Show a confirmation modal before leaving a workspace group."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = (
        helpers.safe_get(body, "team", "id")
        or helpers.safe_get(body, "view", "team_id")
        or helpers.safe_get(body, "team_id")
    )
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        _logger.warning("authorization_denied", extra={"user_id": user_id, "action": "leave_group"})
        return

    action_data = helpers.safe_get(body, "actions", 0) or {}
    action_id: str = action_data.get("action_id", "")
    group_id_str = action_id.replace(f"{actions.CONFIG_LEAVE_GROUP}_", "")

    try:
        group_id = int(group_id_str)
    except (TypeError, ValueError):
        _logger.warning("leave_group_invalid_id", extra={"action_id": action_id})
        return

    groups = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])
    if not groups:
        return
    group = groups[0]

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    workspace_record = helpers.get_workspace_record(
        helpers.safe_get(body, "team", "id"),
        body,
        context,
        client,
    )

    # An owner may not abandon a group that would be left with no owner. Show
    # the reason in the modal rather than failing on submit.
    if workspace_record:
        allowed, reason = helpers.can_workspace_leave(group_id, workspace_record.id)
        if not allowed:
            blocked_form = orm.BlockView(
                blocks=[
                    orm.SectionBlock(
                        label=(
                            f':lock: *You are the only Owner of "{group.name}", so you cannot leave it yet.*\n\n'
                            "Every group needs at least one Owner. Promote another Workspace to Owner "
                            "from the group's member list, and then you will be able to leave.\n\n"
                            "_If you would rather shut the whole group down, and you are the only "
                            "Workspace publishing a Channel into it, use Disband Group instead._"
                        ),
                    ),
                ]
            )
            _logger.info(
                "leave_group_blocked",
                extra={"group_id": group_id, "workspace_id": workspace_record.id, "reason": reason},
            )
            blocked_form.post_modal(
                client=client,
                trigger_id=trigger_id,
                callback_id=actions.CONFIG_LEAVE_GROUP_CONFIRM,
                title_text="Leave Group",
                submit_button_text=None,
                close_button_text="Close",
                parent_metadata={"group_id": group_id, "blocked": True},
                body=body,
            )
            return

    confirm_form = orm.BlockView(
        blocks=[
            orm.SectionBlock(
                label=(
                    f':warning: *Are you sure you want to leave the group "{group.name}"?*\n\n'
                    "This will:\n"
                    "\u2022 Stop all channel syncs you have in this group\n"
                    "\u2022 Remove your synced message history from this group\n"
                    "\u2022 Remove your user mappings for this group\n\n"
                    "_Other members will continue syncing uninterrupted._"
                ),
            ),
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=":wave: Leave Group",
                        action=actions.CONFIG_LEAVE_GROUP_CONFIRM,
                        value=str(group_id),
                        style="danger",
                    ),
                ]
            ),
        ]
    )

    confirm_form.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_LEAVE_GROUP_CONFIRM,
        title_text="Leave Group",
        submit_button_text=None,
        close_button_text="Cancel",
        parent_metadata={"group_id": group_id},
        body=body,
    )


def handle_leave_group_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Execute group departure after confirmation.

    - Soft-deletes the membership record
    - Removes this workspace's SyncChannels (and their PostMeta) for group syncs
    - Leaves all affected Slack channels
    - Cleans up syncs this workspace published (if all subscribers are gone)
    - Removes user mappings scoped to this group
    - Notifies remaining group members
    """
    from handlers._common import _close_modal_done, _parse_private_metadata

    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.safe_get(body, "view", "team_id") or helpers.safe_get(body, "team", "id")
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        _logger.warning("authorization_denied", extra={"user_id": user_id, "action": "leave_group_confirm"})
        return

    meta = _parse_private_metadata(body)
    group_id = meta.get("group_id")
    if not group_id:
        _logger.warning("leave_group_confirm: missing group_id in metadata")
        return

    if meta.get("blocked"):
        # The sole-owner explanation modal is close-only and never routes here;
        # this guards a forged payload.
        return

    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return

    # Re-checked on submit: main_response has no authorization gate, so the
    # modal-time check alone is bypassable with a forged view submission.
    allowed, reason = helpers.can_workspace_leave(group_id, workspace_record.id)
    if not allowed:
        _logger.warning(
            "leave_group_denied",
            extra={"group_id": group_id, "workspace_id": workspace_record.id, "reason": reason},
        )
        return

    groups = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])
    if not groups:
        return
    group = groups[0]

    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.workspace_id == workspace_record.id,
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    if not members:
        _logger.warning("leave_group_confirm: not a member", extra={"group_id": group_id})
        return

    acting_user_id = helpers.safe_get(body, "user", "id") or user_id
    _, admin_label = helpers.format_admin_label(client, acting_user_id, workspace_record)

    syncs_in_group = DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group_id])

    for sync in syncs_in_group:
        my_channels = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == sync.id,
                schemas.SyncChannel.workspace_id == workspace_record.id,
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
        helpers.purge_sync_channels(my_channels)
        for ch in my_channels:
            try:
                client.conversations_leave(channel=ch.channel_id)
            except Exception as e:
                _logger.warning(f"Failed to leave channel {ch.channel_id}: {e}")

        if sync.publisher_workspace_id == workspace_record.id:
            remaining = DbManager.find_records(
                schemas.SyncChannel,
                [schemas.SyncChannel.sync_id == sync.id, schemas.SyncChannel.deleted_at.is_(None)],
            )
            if not remaining:
                # purge_sync, not a bare Sync delete: soft-deleted channels from an
                # uninstalled member are excluded by `remaining` but still reference
                # the sync, so a parent-first delete fails on MySQL.
                helpers.purge_sync(sync.id)

    DbManager.delete_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.group_id == group_id,
            (
                (schemas.UserMapping.source_workspace_id == workspace_record.id)
                | (schemas.UserMapping.target_workspace_id == workspace_record.id)
            ),
        ],
    )

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    for member in members:
        DbManager.update_records(
            schemas.WorkspaceGroupMember,
            [schemas.WorkspaceGroupMember.id == member.id],
            {
                schemas.WorkspaceGroupMember.status: "inactive",
                schemas.WorkspaceGroupMember.deleted_at: now,
            },
        )

    _logger.info(
        "group_left",
        extra={"workspace_id": workspace_record.id, "group_id": group_id, "group_name": group.name},
    )

    remaining_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )

    if not remaining_members:
        DbManager.delete_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])
        _logger.info("group_deleted_empty", extra={"group_id": group_id})
    else:
        for member in remaining_members:
            if not member.workspace_id:
                continue
            member_ws = helpers.get_workspace_by_id(member.workspace_id)
            if not member_ws or not member_ws.bot_token or member_ws.deleted_at:
                continue
            try:
                member_client = WebClient(token=helpers.decrypt_bot_token(member_ws.bot_token))
                helpers.notify_admins_dm(
                    member_client,
                    f":wave: *{admin_label}* left the group *{group.name}*.",
                    team_id=member_ws.team_id,
                )
                builders.refresh_home_tab_for_workspace(member_ws, logger, context=None)
            except Exception as e:
                _logger.warning(f"Failed to notify group member {member.workspace_id}: {e}")

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    _close_modal_done(client, body, f":wave: You have left *{group.name}*. You can close this now.")


def _member_id_from_action(body: dict, prefix: str) -> int | None:
    """Read the trailing member id off a prefix-matched action id."""
    action_data = helpers.safe_get(body, "actions", 0) or {}
    raw = action_data.get("value") or (action_data.get("action_id", "") or "").replace(f"{prefix}_", "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        _logger.warning("invalid_member_id", extra={"action": prefix, "raw_length": len(str(raw))})
        return None


def _notify_group_admins(group_id: int, message: str, logger: Logger) -> None:
    """DM every active local member's admins and refresh their Home tabs."""
    for member in helpers.get_active_members(group_id):
        if not member.workspace_id:
            continue
        member_ws = helpers.get_workspace_by_id(member.workspace_id)
        if not member_ws or not member_ws.bot_token or member_ws.deleted_at:
            continue
        try:
            member_client = WebClient(token=helpers.decrypt_bot_token(member_ws.bot_token))
            helpers.notify_admins_dm(member_client, message, team_id=member_ws.team_id)
            builders.refresh_home_tab_for_workspace(member_ws, logger, context=None)
        except Exception as e:
            _logger.warning(f"Failed to notify group member {member.workspace_id}: {e}")


def handle_promote_to_owner(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Promote another member of a group to Owner.

    Owner-only. Eligible targets are active members with a ``workspace_id``,
    which excludes pending invitees and federated members — promoting a
    federated member would hand group control to a remote instance.
    """
    from handlers._common import _get_authorized_workspace

    auth_result = _get_authorized_workspace(body, client, context, "promote_to_owner")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    member_id = _member_id_from_action(body, actions.CONFIG_PROMOTE_TO_OWNER)
    if member_id is None:
        return

    target = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not target:
        return

    if not helpers.is_workspace_owner(target.group_id, workspace_record.id):
        _logger.warning(
            "authorization_denied",
            extra={
                "action": "promote_to_owner",
                "group_id": target.group_id,
                "acting_workspace_id": workspace_record.id,
            },
        )
        return

    eligible_ids = {member.id for member in helpers.get_promotable_members(target.group_id)}
    if member_id not in eligible_ids:
        _logger.warning(
            "promote_to_owner_ineligible",
            extra={"member_id": member_id, "group_id": target.group_id},
        )
        return

    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member_id],
        {schemas.WorkspaceGroupMember.role: helpers.OWNER},
    )

    _logger.info(
        "group_owner_promoted",
        extra={
            "group_id": target.group_id,
            "member_id": member_id,
            "workspace_id": target.workspace_id,
            "promoted_by_workspace_id": workspace_record.id,
        },
    )

    group = DbManager.get_record(schemas.WorkspaceGroup, id=target.group_id)
    target_ws = helpers.get_workspace_by_id(target.workspace_id) if target.workspace_id else None
    target_name = helpers.resolve_workspace_name(target_ws) if target_ws else "A Workspace"
    _notify_group_admins(
        target.group_id,
        f":key: *{target_name}* is now an Owner of the group *{group.name if group else 'the group'}*.",
        logger,
    )

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)


def handle_demote_self(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Give up this workspace's own ownership of a group.

    Self-demotion only, under the same "another owner remains" guard as leaving.
    Letting owners demote each other invites ownership fights and has no use case
    yet.
    """
    from handlers._common import _get_authorized_workspace

    auth_result = _get_authorized_workspace(body, client, context, "demote_self")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    member_id = _member_id_from_action(body, actions.CONFIG_DEMOTE_SELF)
    if member_id is None:
        return

    target = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not target:
        return

    if target.workspace_id != workspace_record.id:
        _logger.warning(
            "authorization_denied",
            extra={
                "action": "demote_self",
                "member_id": member_id,
                "acting_workspace_id": workspace_record.id,
            },
        )
        return

    owners = helpers.get_active_owners(target.group_id)
    if not any(owner.id == member_id for owner in owners):
        return
    if len(owners) < 2:
        _logger.info(
            "demote_self_blocked_sole_owner",
            extra={"group_id": target.group_id, "workspace_id": workspace_record.id},
        )
        return

    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member_id],
        {schemas.WorkspaceGroupMember.role: helpers.MEMBER},
    )

    _logger.info(
        "group_owner_demoted",
        extra={"group_id": target.group_id, "member_id": member_id, "workspace_id": workspace_record.id},
    )

    group = DbManager.get_record(schemas.WorkspaceGroup, id=target.group_id)
    ws_name = helpers.resolve_workspace_name(workspace_record)
    _notify_group_admins(
        target.group_id,
        f":information_source: *{ws_name}* is no longer an Owner of the group "
        f"*{group.name if group else 'the group'}*.",
        logger,
    )

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)


def _disband_denial_message(reason: str, group_name: str, group_id: int, workspace_id: int) -> str:
    """Explain a disband rejection in terms of what the operator has to do next."""
    if reason == "co_owner_exists":
        return (
            f':lock: *"{group_name}" has more than one Owner, so it cannot be disbanded.*\n\n'
            "Disbanding destroys the group for everyone, so it takes the agreement of every Owner. "
            "Ask the other Owners to give up ownership first — each Owner can demote itself while "
            "another Owner remains — and then you will be the only Owner and able to disband.\n\n"
            "_You can always Leave Sync on your own Channels and leave the group instead._"
        )
    if reason == "other_publishers":
        publisher_ids = helpers.get_other_publisher_workspace_ids(group_id, workspace_id)
        names = []
        for publisher_id in publisher_ids:
            publisher_ws = helpers.get_workspace_by_id(publisher_id)
            names.append(helpers.resolve_workspace_name(publisher_ws) if publisher_ws else f"Workspace {publisher_id}")
        named = ", ".join(f"*{name}*" for name in names) or "another Workspace"
        return (
            f':lock: *"{group_name}" still has Channels published by other Workspaces.*\n\n'
            f"{named} would lose work you do not own, so disbanding is blocked. "
            "Ask them to Leave Sync on their Channels or leave the group first.\n\n"
            "_You can always Leave Sync on your own Channels and leave the group instead._"
        )
    return (
        f':lock: *You are not an Owner of "{group_name}", so you cannot disband it.*\n\n'
        "Only the group's sole Owner can disband a group."
    )


def handle_disband_group(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Show a confirmation modal before disbanding a group.

    Gated on two conditions that must both hold: the acting workspace is the
    group's only Owner, and it is the group's only publisher. See
    ``helpers.can_disband``.
    """
    from handlers._common import _get_authorized_workspace

    auth_result = _get_authorized_workspace(body, client, context, "disband_group")
    if not auth_result:
        return
    _, workspace_record = auth_result

    action_data = helpers.safe_get(body, "actions", 0) or {}
    raw = action_data.get("value") or (action_data.get("action_id", "") or "").replace(
        f"{actions.CONFIG_DISBAND_GROUP}_", ""
    )
    try:
        group_id = int(raw)
    except (TypeError, ValueError):
        _logger.warning("disband_group_invalid_id")
        return

    groups = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])
    if not groups:
        return
    group = groups[0]

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    allowed, reason = helpers.can_disband(group_id, workspace_record.id)
    if not allowed:
        _logger.info(
            "disband_group_blocked",
            extra={"group_id": group_id, "workspace_id": workspace_record.id, "reason": reason},
        )
        orm.BlockView(
            blocks=[
                orm.SectionBlock(
                    label=_disband_denial_message(reason, group.name, group_id, workspace_record.id),
                )
            ]
        ).post_modal(
            client=client,
            trigger_id=trigger_id,
            callback_id=actions.CONFIG_DISBAND_GROUP_CONFIRM,
            title_text="Disband Group",
            submit_button_text=None,
            close_button_text="Close",
            parent_metadata={"group_id": group_id, "blocked": True},
            body=body,
        )
        return

    members = helpers.get_active_members(group_id)
    syncs = DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group_id])
    channel_count = 0
    if syncs:
        channel_count = len(
            DbManager.find_records(
                schemas.SyncChannel,
                [
                    schemas.SyncChannel.sync_id.in_([sync.id for sync in syncs]),
                    schemas.SyncChannel.deleted_at.is_(None),
                ],
            )
        )

    orm.BlockView(
        blocks=[
            orm.SectionBlock(
                label=(
                    f':warning: *Are you sure you want to disband the group "{group.name}"?*\n\n'
                    "This cannot be undone. It will:\n"
                    f"\u2022 Remove all {len(members)} Workspace(s) from the group\n"
                    f"\u2022 Delete {len(syncs)} Channel Sync(s) covering {channel_count} Channel(s)\n"
                    "\u2022 Delete the synced message history for those Channels\n"
                    "\u2022 Delete every user mapping scoped to this group\n\n"
                    "_User mappings took Auto Map Now and manual edits to build. If you re-create "
                    "this group later, those mappings have to be established again._"
                ),
            ),
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=":wastebasket: Disband Group",
                        action=actions.CONFIG_DISBAND_GROUP_CONFIRM,
                        value=str(group_id),
                        style="danger",
                    ),
                ]
            ),
        ]
    ).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_DISBAND_GROUP_CONFIRM,
        title_text="Disband Group",
        submit_button_text=None,
        close_button_text="Cancel",
        parent_metadata={"group_id": group_id},
        body=body,
    )


def handle_disband_group_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Disband a group after confirmation: purge its syncs, memberships, and mappings."""
    from handlers._common import _close_modal_done, _parse_private_metadata

    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.safe_get(body, "view", "team_id") or helpers.safe_get(body, "team", "id")
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        _logger.warning("authorization_denied", extra={"user_id": user_id, "action": "disband_group_confirm"})
        return

    meta = _parse_private_metadata(body)
    group_id = meta.get("group_id")
    if not group_id or meta.get("blocked"):
        return

    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return

    # Re-checked on submit: main_response has no authorization gate, so the
    # modal-time check alone is bypassable with a forged view submission.
    allowed, reason = helpers.can_disband(group_id, workspace_record.id)
    if not allowed:
        _logger.warning(
            "disband_group_denied",
            extra={"group_id": group_id, "workspace_id": workspace_record.id, "reason": reason},
        )
        return

    groups = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])
    if not groups:
        return
    group = groups[0]

    acting_user_id = helpers.safe_get(body, "user", "id") or user_id
    _, admin_label = helpers.format_admin_label(client, acting_user_id, workspace_record)

    # Notify before the teardown, while the membership rows still exist.
    _notify_group_admins(
        group_id,
        f":wastebasket: *{admin_label}* disbanded the group *{group.name}*. Its Channel Syncs have been removed.",
        logger,
    )

    for sync in DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group_id]):
        channels = DbManager.find_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.sync_id == sync.id, schemas.SyncChannel.deleted_at.is_(None)],
        )
        try:
            helpers.purge_sync(sync.id)
        except Exception as e:
            _logger.error("disband_purge_sync_failed", extra={"sync_id": sync.id, "error": str(e)})
            return

        for channel in channels:
            channel_ws = helpers.get_workspace_by_id(channel.workspace_id)
            if not channel_ws or not channel_ws.bot_token:
                continue
            try:
                ws_client = WebClient(token=helpers.decrypt_bot_token(channel_ws.bot_token))
                ws_client.conversations_leave(channel=channel.channel_id)
            except Exception as e:
                _logger.warning(f"Failed to leave channel {channel.channel_id}: {e}")

    DbManager.delete_records(schemas.UserMapping, [schemas.UserMapping.group_id == group_id])
    DbManager.delete_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.group_id == group_id],
    )
    DbManager.delete_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])

    _logger.info(
        "group_disbanded",
        extra={"group_id": group_id, "group_name": group.name, "workspace_id": workspace_record.id},
    )

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    _close_modal_done(client, body, f":wastebasket: *{group.name}* has been disbanded. You can close this now.")
