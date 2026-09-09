"""Workspace group handlers — create, join, accept, cancel."""

import contextlib
import logging
import secrets
import string
from datetime import UTC, datetime
from logging import Logger

from slack_sdk.web import WebClient

import builders
import helpers
from db import DbManager, schemas
from handlers._common import (
    _get_authorized_workspace,
    _get_selected_option_value,
    _get_text_input_value,
    _parse_private_metadata,
)
from slack import actions, forms, orm
from slack.blocks import context as block_context
from slack.blocks import divider, section

_logger = logging.getLogger(__name__)

_INVITE_CODE_CHARS = string.ascii_uppercase + string.digits


def _generate_invite_code(length: int = 7) -> str:
    """Generate a random alphanumeric invite code like ``A7X-K9M``."""
    raw = "".join(secrets.choice(_INVITE_CODE_CHARS) for _ in range(length))
    return f"{raw[:3]}-{raw[3:]}" if length >= 6 else raw


def _activate_group_membership(
    client: WebClient,
    workspace_record: "schemas.Workspace",
    group: "schemas.WorkspaceGroup",
) -> None:
    """Seed mapping stubs from existing directories (no crawl, no auto-match).

    Auto Map Now runs from the User Mapping modal. Partners
    keep whatever ``user_directory`` rows they already have.
    """
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group.id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
            schemas.WorkspaceGroupMember.workspace_id != workspace_record.id,
        ],
    )

    for member in members:
        if not member.workspace_id:
            continue
        member_ws = helpers.get_workspace_by_id(member.workspace_id)
        if not member_ws or member_ws.deleted_at:
            continue

        try:
            helpers.seed_user_mappings(workspace_record.id, member_ws.id, group_id=group.id)
            helpers.seed_user_mappings(member_ws.id, workspace_record.id, group_id=group.id)
        except Exception as e:
            _logger.warning(f"Failed to seed user mappings: {e}")


def handle_create_group(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open a modal for naming a new workspace group."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = (
        helpers.safe_get(body, "team", "id")
        or helpers.safe_get(body, "view", "team_id")
        or helpers.safe_get(body, "team_id")
    )
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        _logger.warning("authorization_denied", extra={"user_id": user_id, "action": "create_group"})
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    view = orm.BlockView(
        blocks=[
            orm.InputBlock(
                label="Workspace Group Name",
                action=actions.CONFIG_CREATE_GROUP_NAME,
                element=orm.PlainTextInputElement(placeholder="e.g. Slack Syndicate, The Multiverse..."),
                optional=False,
            ),
            orm.ContextBlock(
                element=orm.ContextElement(
                    initial_value="_Give this Workspace Group a friendly and descriptive name._",
                ),
            ),
        ]
    )

    view.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_CREATE_GROUP_SUBMIT,
        title_text="Create Group",
        submit_button_text="Create Group",
        body=body,
    )


def handle_create_group_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Create the workspace group and add this workspace as the creator."""
    auth_result = _get_authorized_workspace(body, client, context, "create_group_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    group_name = (_get_text_input_value(body, actions.CONFIG_CREATE_GROUP_NAME) or "").strip()

    if not group_name:
        _logger.warning("create_group_submit: empty group name")
        return

    if len(group_name) > 100:
        group_name = group_name[:100]

    code = _generate_invite_code()
    now = datetime.now(UTC)

    group = schemas.WorkspaceGroup(
        name=group_name,
        invite_code=code,
        status="active",
        created_at=now,
    )
    DbManager.create_record(group)

    member = schemas.WorkspaceGroupMember(
        group_id=group.id,
        workspace_id=workspace_record.id,
        status="active",
        role="owner",
        joined_at=now,
    )
    DbManager.create_record(member)

    _logger.info(
        "group_created",
        extra={
            "workspace_id": workspace_record.id,
            "group_id": group.id,
            "group_name": group_name,
            "invite_code": code,
        },
    )

    acting_user_id = helpers.safe_get(body, "user", "id") or user_id
    if acting_user_id:
        try:
            dm = client.conversations_open(users=[acting_user_id])
            dm_channel = helpers.safe_get(dm, "channel", "id")
            if dm_channel:
                client.chat_postMessage(
                    channel=dm_channel,
                    text=f":raised_hands: *New Workspace Group Created!*\n\n*Group Name:* `{group_name}`\n\n*Invite Code:* `{code}`\n\n"
                    "You can share the Invite Code with an Admin from another Workspace and they can join the Group.",
                )
        except Exception as e:
            _logger.warning(f"Failed to DM invite code: {e}")

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)


def handle_join_group(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open a modal for entering a group invite code."""
    import copy

    user_id = helpers.get_user_id_from_body(body)
    team_id = (
        helpers.safe_get(body, "team", "id")
        or helpers.safe_get(body, "view", "team_id")
        or helpers.safe_get(body, "team_id")
    )
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        _logger.warning("authorization_denied", extra={"user_id": user_id, "action": "join_group"})
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    enter_form = copy.deepcopy(forms.ENTER_GROUP_CODE_FORM)
    enter_form.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_JOIN_GROUP_SUBMIT,
        title_text="Join Group",
        new_or_add="new",
        body=body,
    )


def handle_join_group_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Validate an invite code and join the workspace group."""
    auth_result = _get_authorized_workspace(body, client, context, "join_group_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    form_data = forms.ENTER_GROUP_CODE_FORM.get_selected_values(body)
    raw_code = (helpers.safe_get(form_data, actions.CONFIG_JOIN_GROUP_CODE) or "").strip().upper()

    if "-" not in raw_code and len(raw_code) >= 6:
        raw_code = f"{raw_code[:3]}-{raw_code[3:]}"

    acting_user_id = helpers.safe_get(body, "user", "id") or user_id

    rate_key = f"group_join_attempts:{workspace_record.id}"
    attempts = helpers._cache_get(rate_key) or 0
    if attempts >= 5:
        _logger.warning("group_join_rate_limited", extra={"workspace_id": workspace_record.id})
        builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
        return

    groups = DbManager.find_records(
        schemas.WorkspaceGroup,
        [
            schemas.WorkspaceGroup.invite_code == raw_code,
            schemas.WorkspaceGroup.status == "active",
        ],
    )

    if not groups:
        helpers._cache_set(rate_key, attempts + 1, ttl=900)
        _logger.warning(
            "group_code_invalid",
            extra={
                "workspace_id": workspace_record.id,
                "attempt": attempts + 1,
                "code_length": len(raw_code),
            },
        )
        builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
        return

    group = groups[0]

    # The old self-join guard read created_by_workspace_id here. The membership
    # check below already covers it, since a group's creator always has a
    # membership row — and dropping the guard lets a workspace that left rejoin
    # by code instead of being blocked forever.
    existing = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group.id,
            schemas.WorkspaceGroupMember.workspace_id == workspace_record.id,
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    if existing:
        _logger.info("group_already_member", extra={"workspace_id": workspace_record.id, "group_id": group.id})
        builders.build_home_tab(body, client, logger, context, user_id=acting_user_id)
        return

    now = datetime.now(UTC)
    member = schemas.WorkspaceGroupMember(
        group_id=group.id,
        workspace_id=workspace_record.id,
        status="active",
        role="member",
        joined_at=now,
    )
    DbManager.create_record(member)

    _logger.info(
        "group_joined",
        extra={
            "workspace_id": workspace_record.id,
            "group_id": group.id,
            "group_name": group.name,
        },
    )

    _activate_group_membership(client, workspace_record, group)

    _, admin_label = helpers.format_admin_label(client, acting_user_id, workspace_record)

    other_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group.id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
            schemas.WorkspaceGroupMember.workspace_id != workspace_record.id,
        ],
    )
    for other_member in other_members:
        if not other_member.workspace_id:
            continue
        member_ws = helpers.get_workspace_by_id(other_member.workspace_id)
        if not member_ws or not member_ws.bot_token or member_ws.deleted_at:
            continue
        try:
            member_client = WebClient(token=helpers.decrypt_bot_token(member_ws.bot_token))
            helpers.notify_admins_dm(
                member_client,
                f":punch: *{admin_label}* joined the Workspace Group called *{group.name}*.",
                team_id=member_ws.team_id,
            )
            builders.refresh_home_tab_for_workspace(member_ws, logger, context=None)
        except Exception as e:
            _logger.warning(f"Failed to notify group member {other_member.workspace_id}: {e}")

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)


# ---------------------------------------------------------------------------
# Invite workspace to group
# ---------------------------------------------------------------------------


def handle_invite_workspace(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open a modal for inviting a workspace to a group."""

    auth_result = _get_authorized_workspace(body, client, context, "invite_workspace")
    if not auth_result:
        return
    _, workspace_record = auth_result

    trigger_id = helpers.safe_get(body, "trigger_id")
    raw_group_id = helpers.safe_get(body, "actions", 0, "value")
    try:
        group_id = int(raw_group_id)
    except (TypeError, ValueError):
        _logger.warning(f"invite_workspace: invalid group_id: {raw_group_id!r}")
        return

    group = DbManager.get_record(schemas.WorkspaceGroup, id=group_id)
    if not group:
        return

    current_workspace_id = workspace_record.id if workspace_record else None

    # Only active members count as "already in the group"; pending invites can be re-invited
    current_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    member_ws_ids = {member.workspace_id for member in current_members if member.workspace_id}

    all_workspaces = DbManager.find_records(
        schemas.Workspace,
        [schemas.Workspace.deleted_at.is_(None)],
    )
    eligible = [ws for ws in all_workspaces if ws.id not in member_ws_ids and ws.bot_token]

    # Show Oops only when there are no other installed workspaces at all (not when everyone is already in the group)
    other_installed = [ws for ws in all_workspaces if ws.bot_token and ws.id != current_workspace_id]
    if not other_installed and not helpers.federation_enabled():
        msg_blocks = [
            section(
                "At least one other Slack Workspace needs to install this SyncBot app, or "
                "External Connections need to be allowed, before you can invite another Workspace to this Group."
            ),
        ]
        orm.BlockView(blocks=msg_blocks).post_modal(
            client=client,
            trigger_id=trigger_id,
            callback_id=actions.CONFIG_INVITE_WORKSPACE_SUBMIT,
            title_text="Oops!",
            submit_button_text=None,
            new_or_add="new",
            body=body,
        )
        return

    modal_blocks: list = []

    if eligible:
        workspace_options = [
            orm.SelectorOption(
                name=helpers.resolve_workspace_name(workspace),
                value=str(workspace.id),
            )
            for workspace in eligible
        ]
        modal_blocks.append(
            orm.InputBlock(
                label="Send a SyncBot DM",
                action=actions.CONFIG_INVITE_WORKSPACE_SELECT,
                element=orm.StaticSelectElement(
                    placeholder="Select a Workspace",
                    options=workspace_options,
                ),
                optional=True,
            )
        )
        modal_blocks.append(
            block_context(
                "A SyncBot DM will be sent to Admins in the other Workspace.",
            )
        )

    modal_blocks.append(block_context("\u200b"))
    modal_blocks.append(divider())
    modal_blocks.append(section(":memo: *Invite Code*"))
    modal_blocks.append(
        block_context(
            f"Alternatively, share this Invite Code with an Admin from another Workspace:\n\n`{group.invite_code}`"
        )
    )

    if helpers.federation_enabled():
        modal_blocks.append(divider())
        modal_blocks.append(section(":globe_with_meridians: *External Workspace*"))
        modal_blocks.append(
            block_context(
                "For Workspaces running their own external SyncBot instance, "
                f"share this Invite Code for them to join:\n\n`{group.invite_code}`"
            )
        )

    submit_text = "Send Invite" if eligible else None
    view = orm.BlockView(blocks=modal_blocks)
    view.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_INVITE_WORKSPACE_SUBMIT,
        title_text="Invite Workspace",
        submit_button_text=submit_text,
        parent_metadata={"group_id": group_id},
        new_or_add="new",
        body=body,
    )


def handle_invite_workspace_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Send a DM invite to admins of the selected workspace."""
    auth_result = _get_authorized_workspace(body, client, context, "invite_workspace_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result
    meta = _parse_private_metadata(body)
    group_id = meta.get("group_id")
    if not group_id:
        return

    group = DbManager.get_record(schemas.WorkspaceGroup, id=group_id)
    if not group:
        return

    selected_ws_id = _get_selected_option_value(body, actions.CONFIG_INVITE_WORKSPACE_SELECT)

    if not selected_ws_id:
        return

    try:
        target_ws_id = int(selected_ws_id)
    except (TypeError, ValueError):
        return

    target_ws = helpers.get_workspace_by_id(target_ws_id)
    if not target_ws or not target_ws.bot_token or target_ws.deleted_at:
        _logger.warning(f"invite_workspace_submit: target workspace {target_ws_id} not available")
        return

    existing = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.workspace_id == target_ws_id,
        ],
    )
    if existing:
        _logger.info(f"invite_workspace_submit: workspace {target_ws_id} already in group {group_id}")
        builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
        return

    acting_user_id = helpers.safe_get(body, "user", "id") or user_id
    member = schemas.WorkspaceGroupMember(
        group_id=group_id,
        workspace_id=target_ws_id,
        status="pending",
        role="member",
        joined_at=None,
        invited_by_slack_user_id=acting_user_id,
        invited_by_workspace_id=workspace_record.id,
    )
    DbManager.create_record(member)

    _, admin_label = helpers.format_admin_label(client, acting_user_id, workspace_record)

    target_client = WebClient(token=helpers.decrypt_bot_token(target_ws.bot_token))

    invite_blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":punch: *{admin_label}* has invited your Workspace to join a SyncBot Group!\n\n*Group Name:* `{group.name}`",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Accept"},
                    "style": "primary",
                    "action_id": f"{actions.CONFIG_ACCEPT_GROUP_REQUEST}_{member.id}",
                    "value": str(member.id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Decline"},
                    "style": "danger",
                    "action_id": f"{actions.CONFIG_DECLINE_GROUP_REQUEST}_{member.id}",
                    "value": str(member.id),
                },
            ],
        },
    ]

    dm_entries = helpers.notify_admins_dm_blocks(
        target_client,
        f"{admin_label} has invited your Workspace to join a SyncBot Group!\n\n*Group Name:* `{group.name}`",
        invite_blocks,
        team_id=target_ws.team_id,
    )
    helpers.save_dm_messages_to_group_member(member.id, dm_entries)

    _logger.info(
        "group_invite_sent",
        extra={
            "group_id": group_id,
            "target_workspace_id": target_ws_id,
            "member_id": member.id,
        },
    )

    builders.refresh_home_tab_for_workspace(target_ws, logger, context=None)
    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)


# ---------------------------------------------------------------------------
# Accept / Decline group invite
# ---------------------------------------------------------------------------


def handle_accept_group_invite(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Accept a pending group invite from a DM button.

    Only the invited workspace may accept. ``member_id`` comes from the button
    value, so the acting workspace is resolved from the request and compared
    against ``member.workspace_id`` — without that check any signed interaction
    carrying an arbitrary integer could activate any pending invite.
    """
    raw_member_id = helpers.safe_get(body, "actions", 0, "value")
    try:
        member_id = int(raw_member_id)
    except (TypeError, ValueError):
        _logger.warning(f"accept_group_invite: invalid member_id: {raw_member_id!r}")
        return

    auth_result = _get_authorized_workspace(body, client, context, "accept_group_invite")
    if not auth_result:
        return
    user_id, acting_workspace = auth_result

    member = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not member or member.status != "pending":
        _logger.info(f"accept_group_invite: member {member_id} not pending")
        return

    if not member.workspace_id or member.workspace_id != acting_workspace.id:
        _logger.warning(
            "authorization_denied",
            extra={
                "action": "accept_group_invite",
                "member_id": member_id,
                "acting_workspace_id": acting_workspace.id,
            },
        )
        return

    group = DbManager.get_record(schemas.WorkspaceGroup, id=member.group_id)
    if not group:
        return

    workspace_record = helpers.get_workspace_by_id(member.workspace_id)
    if not workspace_record:
        return

    now = datetime.now(UTC)
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member_id],
        {
            schemas.WorkspaceGroupMember.status: "active",
            schemas.WorkspaceGroupMember.joined_at: now,
        },
    )

    _activate_group_membership(client, workspace_record, group)

    _update_invite_dms(
        member,
        workspace_record,
        f"Your Workspace has joined the SyncBot Group called *{group.name}*.",
    )

    other_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group.id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
            schemas.WorkspaceGroupMember.workspace_id != workspace_record.id,
        ],
    )
    ws_name = helpers.resolve_workspace_name(workspace_record)
    for other_member in other_members:
        if not other_member.workspace_id:
            continue
        member_ws = helpers.get_workspace_by_id(other_member.workspace_id)
        if not member_ws or not member_ws.bot_token or member_ws.deleted_at:
            continue
        try:
            member_client = WebClient(token=helpers.decrypt_bot_token(member_ws.bot_token))
            helpers.notify_admins_dm(
                member_client,
                f":punch: *{ws_name}* has joined the Workspace Group called *{group.name}*.",
                team_id=member_ws.team_id,
            )
            builders.refresh_home_tab_for_workspace(member_ws, logger, context=None)
        except Exception as e:
            _logger.warning(f"Failed to notify group member {other_member.workspace_id}: {e}")

    _logger.info(
        "group_invite_accepted",
        extra={
            "member_id": member_id,
            "group_id": group.id,
            "workspace_id": workspace_record.id,
        },
    )

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)


def handle_decline_group_invite(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle Decline (invited workspace) or Cancel Invite (inviting workspace) for a pending group invite.

    Decline is an invitee action, so the acting workspace must be the invited one.
    Cancel is an inviter action, so the acting workspace must be the one that sent
    the invite (``invited_by_workspace_id``). Both destroy a membership row, so
    without these checks any signed interaction carrying an arbitrary integer
    could cancel or decline any pending invite on the instance.
    """
    raw_member_id = helpers.safe_get(body, "actions", 0, "value")
    try:
        member_id = int(raw_member_id)
    except (TypeError, ValueError):
        _logger.warning(f"decline_group_invite: invalid member_id: {raw_member_id!r}")
        return

    action_id = helpers.safe_get(body, "actions", 0, "action_id") or ""
    is_cancel = action_id.startswith(actions.CONFIG_CANCEL_GROUP_REQUEST)
    outcome = "canceled" if is_cancel else "declined"
    action_name = "cancel_group_request" if is_cancel else "decline_group_invite"

    auth_result = _get_authorized_workspace(body, client, context, action_name)
    if not auth_result:
        return
    _, acting_workspace = auth_result

    member = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not member or member.status != "pending":
        _logger.info(f"decline_group_invite: member {member_id} not pending")
        return

    if is_cancel:
        # Cancel is an inviter action: the workspace that sent the invite may
        # cancel it, and so may an owner of the group (co-owners have equal
        # standing over group membership).
        authorized = member.invited_by_workspace_id == acting_workspace.id or helpers.is_workspace_owner(
            member.group_id, acting_workspace.id
        )
    else:
        # Accept and decline are invitee actions: only the invited workspace.
        authorized = member.workspace_id == acting_workspace.id

    if not authorized:
        _logger.warning(
            "authorization_denied",
            extra={
                "action": action_name,
                "member_id": member_id,
                "acting_workspace_id": acting_workspace.id,
            },
        )
        return

    group = DbManager.get_record(schemas.WorkspaceGroup, id=member.group_id)
    group_name = group.name if group else "the group"

    target_ws = helpers.get_workspace_by_id(member.workspace_id) if member.workspace_id else None

    _update_invite_dms(
        member,
        target_ws,
        f":x: The invitation to join *{group_name}* was {outcome}.",
    )

    group_id = member.group_id

    DbManager.delete_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member_id],
    )

    _logger.info(
        "group_invite_declined",
        extra={"member_id": member_id, "group_id": group_id},
    )

    all_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    for member in all_members:
        if not member.workspace_id:
            continue
        member_ws = helpers.get_workspace_by_id(member.workspace_id)
        if not member_ws or not member_ws.bot_token or member_ws.deleted_at:
            continue
        with contextlib.suppress(Exception):
            builders.refresh_home_tab_for_workspace(member_ws, logger, context=None)

    if target_ws and target_ws.bot_token and not target_ws.deleted_at:
        with contextlib.suppress(Exception):
            builders.refresh_home_tab_for_workspace(target_ws, logger, context=None)


def _update_invite_dms(
    member: schemas.WorkspaceGroupMember,
    workspace: schemas.Workspace | None,
    new_text: str,
) -> None:
    """Replace the original invite DM content with an updated message so the invite
    is removed and replaced by the success message (e.g. workspace joined the group).
    """
    import json as _json

    if not member.dm_messages:
        _logger.debug("_update_invite_dms: no dm_messages on member %s", member.id)
        return
    if not workspace or not workspace.bot_token:
        return

    try:
        entries = _json.loads(member.dm_messages)
    except (ValueError, TypeError):
        _logger.warning("_update_invite_dms: invalid dm_messages JSON for member %s", member.id)
        return

    if not entries:
        return

    ws_client = WebClient(token=helpers.decrypt_bot_token(workspace.bot_token))
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": new_text}}]
    for entry in entries:
        channel_id = entry.get("channel")
        message_ts = entry.get("ts")
        if not channel_id or message_ts is None:
            continue
        message_ts_str = str(message_ts).strip()
        if not message_ts_str:
            continue
        try:
            ws_client.chat_update(
                channel=channel_id,
                ts=message_ts_str,
                text=new_text,
                blocks=blocks,
            )
        except Exception as e:
            _logger.warning(
                "_update_invite_dms: failed to update DM channel=%s ts=%s: %s",
                channel_id,
                message_ts_str,
                e,
            )
