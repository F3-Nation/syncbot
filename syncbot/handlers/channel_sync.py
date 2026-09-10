"""Channel sync handlers — create, join, edit, pause, resume, leave."""

import contextlib
import logging
from datetime import UTC, datetime
from logging import Logger

from slack_sdk.web import WebClient

import builders
import constants
import helpers
from builders._common import _format_channel_ref, _get_group_members
from db import DbManager, schemas
from handlers._common import (
    _close_modal_done,
    _ensure_membership_or_rollback,
    _extract_team_id,
    _get_authorized_workspace,
    _get_selected_conversation_or_option,
    _get_selected_option_value,
    _parse_private_metadata,
    _sanitize_text,
)
from slack import actions, orm
from slack.blocks import context as block_context
from slack.blocks import section

_logger = logging.getLogger(__name__)


_PARTICIPATION_ACTIONS = (actions.CONFIG_SYNC_PARTICIPATION,)
_CREATE_CHANNEL_ACTIONS = (actions.CONFIG_CREATE_SYNC_SELECT,)
_JOIN_CHANNEL_ACTIONS = (actions.CONFIG_JOIN_SYNC_SELECT,)
_REACTION_STYLE_ACTIONS = (actions.CONFIG_SYNC_REACTION_STYLE,)


def _participation_block(
    *,
    mode: str,
    publishes: bool = True,
    subscribes: bool = True,
) -> orm.InputBlock:
    """Choose this channel's source/target participation in a sync."""
    create_options = [
        orm.SelectorOption(name="Publish only", value="publish_only"),
        orm.SelectorOption(name="Publish and Subscribe", value="publish_and_subscribe"),
    ]
    all_options = [
        orm.SelectorOption(name="Publish only", value="publish_only"),
        orm.SelectorOption(name="Subscribe only", value="subscribe_only"),
        orm.SelectorOption(name="Publish and Subscribe", value="publish_and_subscribe"),
    ]
    options = create_options if mode == "create" else all_options
    if publishes and not subscribes:
        initial = "publish_only"
    elif subscribes and not publishes:
        initial = "subscribe_only"
    else:
        initial = "publish_and_subscribe"
    if mode == "create" and initial == "subscribe_only":
        initial = "publish_and_subscribe"
    return orm.InputBlock(
        label="Participation",
        action=actions.CONFIG_SYNC_PARTICIPATION,
        element=orm.RadioButtonsElement(
            initial_value=initial,
            options=options,
        ),
        optional=False,
    )


def _reaction_style_block(
    action_id: str,
    *,
    initial: str = constants.DEFAULT_REACTION_STYLE_NEW_RECEIVE,
) -> orm.InputBlock:
    return orm.InputBlock(
        label="Reaction type in this Workspace",
        action=action_id,
        element=orm.RadioButtonsElement(
            initial_value=initial,
            options=[
                orm.SelectorOption(
                    name="Hybrid — direct when possible, otherwise a thread notice",
                    value=constants.REACTION_STYLE_THREADED_AND_DIRECT,
                ),
                orm.SelectorOption(
                    name="Direct — native emoji on the synced message",
                    value=constants.REACTION_STYLE_DIRECT_ONLY,
                ),
                orm.SelectorOption(
                    name="Off — do not apply incoming reactions",
                    value=constants.REACTION_STYLE_OFF,
                ),
            ],
        ),
        optional=False,
    )


def _valid_reaction_style(value: str | None) -> str | None:
    raw = (value or "").strip()
    if raw in (
        constants.REACTION_STYLE_DIRECT_ONLY,
        constants.REACTION_STYLE_THREADED_AND_DIRECT,
        constants.REACTION_STYLE_OFF,
    ):
        return raw
    return None


def _participation_from_value(value: str | None) -> tuple[bool, bool]:
    return helpers.participation_flags(value)


def _participation_from_body(body: dict) -> tuple[bool, bool]:
    for action in _PARTICIPATION_ACTIONS:
        value = _get_selected_option_value(body, action)
        if value:
            return helpers.participation_flags(value)
    return True, True


def _selected_channel(body: dict, action_ids: tuple[str, ...]) -> tuple[str | None, str]:
    for action_id in action_ids:
        channel_id = _get_selected_conversation_or_option(body, action_id)
        if channel_id:
            return channel_id, action_id
    return None, action_ids[0]


def _parse_reaction_fields(body: dict, *, style_action: str | None = None) -> str:
    """Reaction type from the form. Always shown; unused until the Channel subscribes."""
    from helpers.reaction import default_reaction_style_for_new_channel

    actions_to_try = (style_action,) if style_action else _REACTION_STYLE_ACTIONS
    for action in actions_to_try:
        if not action:
            continue
        style = _valid_reaction_style(_get_selected_option_value(body, action))
        if style:
            return style
    return default_reaction_style_for_new_channel(subscribes=True)


def _group_name(group_id: int | None) -> str | None:
    if not group_id:
        return None
    group = DbManager.get_record(schemas.WorkspaceGroup, group_id)
    name = getattr(group, "name", None) if group else None
    return str(name).strip() if name else None


def _publisher_sync_channel(sync_id: int) -> schemas.SyncChannel | None:
    rows = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == int(sync_id), schemas.SyncChannel.deleted_at.is_(None)],
    )
    return next((row for row in rows if helpers.channel_publishes(row)), rows[0] if rows else None)


def _admin_workspace_label(client: WebClient, user_id: str, workspace) -> str:
    """``Name (Workspace)`` for in-channel notices."""
    name, _ = helpers.format_admin_label(client, user_id, workspace)
    ws_name = getattr(workspace, "workspace_name", None) if workspace else None
    if not ws_name and workspace is not None and hasattr(workspace, "bot_token"):
        with contextlib.suppress(Exception):
            ws_name = helpers.resolve_workspace_name(workspace)
    if ws_name:
        return f"{name} ({ws_name})"
    return name


def _create_sync_notice(admin_name: str, publishes: bool, subscribes: bool) -> str:
    first = f"*{admin_name}* created a Sync for this Channel."
    if publishes and subscribes:
        second = "This Channel will send and receive messages between Workspaces that join."
    else:
        second = "Messages from this Channel will be sent to Workspaces that join."
    return f":arrows_counterclockwise: {first} {second}"


def _flow_sentence(
    *,
    here_publishes: bool,
    here_subscribes: bool,
    there_publishes: bool,
    there_subscribes: bool,
) -> str:
    to_them = here_publishes and there_subscribes
    to_here = there_publishes and here_subscribes
    if to_them and to_here:
        return "Messages will flow both ways between these Channels."
    if to_them:
        return "Messages from here will be sent to the Channel in the other Workspace."
    if to_here:
        return "Messages from that Channel will appear here."
    return "Messages will not be exchanged between these Channels."


def _join_notice(
    *,
    admin_label: str,
    other_ref: str,
    here_publishes: bool,
    here_subscribes: bool,
    there_publishes: bool,
    there_subscribes: bool,
    joined: bool,
) -> str:
    if joined:
        first = f"*{admin_label}* subscribed *{other_ref}* to this Channel."
    else:
        first = f"*{admin_label}* subscribed this Channel to *{other_ref}*."
    second = _flow_sentence(
        here_publishes=here_publishes,
        here_subscribes=here_subscribes,
        there_publishes=there_publishes,
        there_subscribes=there_subscribes,
    )
    return f":arrows_counterclockwise: {first} {second}"


def _is_last_publisher(channels: list, workspace_id: int) -> bool:
    publishers = [row for row in channels if helpers.channel_publishes(row)]
    mine = [row for row in publishers if row.workspace_id == workspace_id]
    others = [row for row in publishers if row.workspace_id != workspace_id]
    return bool(mine) and not others


def _sync_id_from_action(body: dict, prefixes: tuple[str, ...]) -> int | None:
    raw = helpers.safe_get(body, "actions", 0, "value")
    try:
        if raw is not None and str(raw).strip() != "":
            return int(raw)
    except (TypeError, ValueError):
        pass
    action_id = helpers.safe_get(body, "actions", 0, "action_id") or ""
    for prefix in prefixes:
        if action_id.startswith(prefix + "_"):
            try:
                return int(action_id[len(prefix) + 1 :])
            except (TypeError, ValueError):
                continue
    return None


def _relationship_context(
    *,
    group_id: int | None = None,
    local_channel_id: str | None = None,
    local_workspace=None,
    published_channel: schemas.SyncChannel | None = None,
    published_workspace=None,
) -> orm.ContextBlock | None:
    """Local Channel, published source, and group — whichever this modal knows."""
    lines: list[str] = []
    if local_channel_id:
        lines.append(f"Channel: {_format_channel_ref(local_channel_id, local_workspace, is_local=True)}")
    if published_channel is not None:
        lines.append(
            "Published Channel: "
            + _format_channel_ref(published_channel.channel_id, published_workspace, is_local=False)
        )
    name = _group_name(group_id)
    if name:
        lines.append(f"Group: `{name}`")
    if not lines:
        return None
    return block_context("\n".join(lines))


def _participation_help(*, mode: str) -> str:
    if mode == "create":
        return (
            "Publish only sends this Channel's messages to Workspaces that join. "
            "Publish and Subscribe also receives messages from Workspaces that join."
        )
    return (
        "Subscribe only receives messages from this Sync. Publish only sends this Channel's "
        "messages without receiving. Publish and Subscribe exchanges messages both ways."
    )


def _reaction_style_help(*, first_time: bool) -> str:
    base = (
        "Reaction type applies while this Channel subscribes. "
        "Off does not apply incoming reactions here, including later unreacts."
    )
    if not first_time:
        return base
    return (
        f"{base} Authorize SyncBot in each Workspace where you want reactions to appear as you. "
        "Custom emoji the other Workspace does not have will not appear as a native reaction there."
    )


def _reaction_style_for_edit(
    sync_channel: schemas.SyncChannel,
    body: dict | None = None,
    *,
    subscribes: bool | None = None,
) -> str | None:
    """Keep a saved type even when the channel does not subscribe.

    Edit always shows the type radios. Turning subscribe off should not
    reset Hybrid, Direct, or Off to the new-channel default.
    """
    from helpers.reaction import default_reaction_style_for_new_channel, reaction_style

    submitted = None
    if body is not None:
        for action in _REACTION_STYLE_ACTIONS:
            submitted = _valid_reaction_style(_get_selected_option_value(body, action))
            if submitted:
                break
    if submitted:
        return submitted
    stored = _valid_reaction_style(getattr(sync_channel, "reaction_style", None))
    if stored:
        return stored
    resolved = _valid_reaction_style(reaction_style(sync_channel))
    if resolved:
        return resolved
    receives = helpers.channel_subscribes(sync_channel) if subscribes is None else subscribes
    if receives:
        return default_reaction_style_for_new_channel(subscribes=True)
    return None


def _channel_picker_block(label: str, action_id: str, *, team_id: str | None) -> orm.InputBlock:
    """Build a native channel picker, honoring the private-channel policy.

    Slack renders ``conversations_select`` as a searchable list over all of the
    user's conversations with no app-side enumeration, so workspaces with more
    than ~100 channels can reach all of them. It is scoped to the viewer, which
    means the private channels it offers are exactly the ones that person belongs
    to — they cannot pick a private channel they are not in.

    That is a client-side guarantee, so it is not relied on alone. The submitted
    payload could still name any channel, and the answer to that is not a second
    membership lookup but the token used to act on it: a private channel is
    reached only by inviting the bot as the acting user, so a channel that person
    is not in fails at Slack. See :func:`helpers.ensure_bot_in_conversation`.
    Already-synced channels, which the picker also cannot pre-exclude, are
    rejected in :func:`_validate_channel_selection`.
    """
    return orm.InputBlock(
        label=label,
        action=action_id,
        element=orm.ConversationsSelectElement(
            placeholder="Search for a Channel",
            include_private=helpers.allow_private_channels(team_id),
        ),
        optional=False,
    )


def _channel_picker_help_text(*, team_id: str | None, subscribe: bool = False) -> str:
    """Explain what may be selected, including the private-channel warning when relevant."""
    if subscribe:
        base = "Search for a Channel in your Workspace to join this Sync."
    else:
        base = "Search for a Channel in your Workspace to create a Sync."
    if helpers.allow_private_channels(team_id):
        if subscribe:
            return (
                f"{base} :warning: Private Channels are currently allowed. Messages from this Sync "
                "will be copied into it, so anyone who can see your Channel "
                "will be able to read them. If you pick a private Channel, SyncBot is added to it "
                "for you, using your permission to invite it."
            )
        return (
            f"{base} :warning: Private Channels are currently allowed. If you create a Sync on a "
            "private Channel, its messages will be copied into the other Workspaces in this Group, "
            "where anyone who can see the synced Channel will be able to read them. If you pick a "
            "private Channel, SyncBot is added to it for you, using your permission to invite it."
        )
    return f"{base} Only public Channels can be synced."


def _looks_private(client: WebClient, channel_id: str) -> bool:
    """Whether a channel is private as far as the bot token can tell.

    A private channel SyncBot has never been in is invisible to the bot token, so
    an unreadable channel is treated as private rather than as a lookup failure.
    """
    try:
        conv_info = client.conversations_info(channel=channel_id)
    except Exception as exc:
        _logger.debug(f"_looks_private: conversations_info failed for {channel_id}: {exc}")
        return True
    return bool(helpers.safe_get(conv_info, "channel", "is_private"))


def _validate_channel_selection(
    client: WebClient,
    channel_id: str | None,
    action_id: str,
    *,
    team_id: str | None = None,
    acting_user_id: str | None = None,
    workspace_id: int | None = None,
    source_sync_id: int | None = None,
) -> dict | None:
    """Validate a selected channel on submit, returning a Slack errors response or None.

    Three rules, all enforced here rather than only in the picker filter, which is
    advisory and bypassable:

    * A Workspace cannot subscribe to the same published source twice.
    * Private channels are rejected unless the ``allow_private_channels`` setting
      is on.
    * A private channel is also rejected when SyncBot has no user token it could
      invite itself with, because only a member of that channel can add an app.
      The check is a local installation-store lookup, cheap enough for the ack
      phase, and it fails here as a field error instead of failing after the
      modal has closed.
    """
    if not channel_id or channel_id == "__none__":
        return {
            "response_action": "errors",
            "errors": {action_id: "Select a Channel."},
        }

    if workspace_id and source_sync_id:
        source_rows = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == int(source_sync_id),
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
        for source in source_rows:
            if not helpers.channel_publishes(source):
                continue
            if helpers.already_subscribed_to_source(
                workspace_id=workspace_id,
                source_workspace_id=source.workspace_id,
                source_channel_id=source.channel_id,
            ):
                return {
                    "response_action": "errors",
                    "errors": {
                        action_id: (
                            "This Workspace already subscribes to that published Channel through another Channel Sync."
                        )
                    },
                }

    if not helpers.allow_private_channels(team_id or ""):
        try:
            conv_info = client.conversations_info(channel=channel_id)
            is_private = bool(helpers.safe_get(conv_info, "channel", "is_private"))
        except Exception as e:
            # Fail closed: an unreadable channel is one the bot cannot join either.
            _logger.warning(f"_validate_channel_selection: conversations_info failed for {channel_id}: {e}")
            return {
                "response_action": "errors",
                "errors": {action_id: "SyncBot could not read that Channel. Pick a public Channel it can join."},
            }
        if is_private:
            return {
                "response_action": "errors",
                "errors": {action_id: "Private Channels cannot be synced. Pick a public Channel."},
            }
        return None

    # Private channels are allowed. Adding the bot to one needs a user token, so
    # when there is none, only a public pick can succeed.
    if helpers.has_user_token(team_id, acting_user_id):
        return None
    if _looks_private(client, channel_id):
        return {
            "response_action": "errors",
            "errors": {action_id: helpers.AUTHORIZE_HINT},
        }

    return None


def _build_create_sync_blocks(*, team_id: str | None, group_id: int | None) -> list[orm.BaseBlock]:
    """One-screen Create Sync: group context, participation, picker, reaction type."""
    blocks: list[orm.BaseBlock] = []
    ctx = _relationship_context(group_id=group_id)
    if ctx:
        blocks.append(ctx)
    blocks.extend(
        [
            _participation_block(mode="create"),
            block_context(_participation_help(mode="create")),
            _channel_picker_block("Channel", actions.CONFIG_CREATE_SYNC_SELECT, team_id=team_id),
            block_context(_channel_picker_help_text(team_id=team_id)),
            _reaction_style_block(actions.CONFIG_SYNC_REACTION_STYLE),
            block_context(_reaction_style_help(first_time=True)),
        ]
    )
    return blocks


def handle_create_sync(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the one-step Create Sync modal."""
    auth_result = _get_authorized_workspace(body, client, context, "create_sync")
    if not auth_result:
        return
    _, workspace_record = auth_result

    trigger_id = helpers.safe_get(body, "trigger_id")
    raw_group_id = helpers.safe_get(body, "actions", 0, "value")
    try:
        group_id = int(raw_group_id)
    except (TypeError, ValueError):
        _logger.warning(f"create_sync: invalid group_id: {raw_group_id!r}")
        return

    orm.BlockView(blocks=_build_create_sync_blocks(team_id=workspace_record.team_id, group_id=group_id)).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_CREATE_SYNC_SUBMIT,
        title_text="Create Sync",
        submit_button_text="Create Sync",
        parent_metadata={"group_id": group_id, "workspace_id": workspace_record.id},
        new_or_add="new",
        body=body,
    )


def handle_create_sync_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase for Create Sync: validate and close modal (errors) or empty ack (success)."""
    auth_result = _get_authorized_workspace(body, client, context, "create_sync_submit")
    if not auth_result:
        return None
    _, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    group_id = metadata.get("group_id")

    if not group_id:
        _logger.warning("create_sync_submit: missing group_id in metadata")
        return None

    channel_id, picker_action = _selected_channel(body, _CREATE_CHANNEL_ACTIONS)

    return _validate_channel_selection(
        client,
        channel_id,
        picker_action,
        team_id=_extract_team_id(body) or workspace_record.team_id,
        acting_user_id=helpers.safe_get(body, "user", "id"),
    )


def handle_create_sync_submit_work(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Lazy work phase: create Sync + SyncChannel after modal closed."""
    auth_result = _get_authorized_workspace(body, client, context, "create_sync_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    group_id = metadata.get("group_id")

    if not group_id:
        return

    publishes, subscribes = _participation_from_body(body)
    reaction_style = _parse_reaction_fields(body)
    channel_id, picker_action = _selected_channel(body, _CREATE_CHANNEL_ACTIONS)

    # The ack phase already surfaced any error; this keeps the work phase from
    # writing on a payload it should reject.
    if _validate_channel_selection(
        client,
        channel_id,
        picker_action,
        team_id=_extract_team_id(body) or workspace_record.team_id,
        acting_user_id=helpers.safe_get(body, "user", "id") or user_id,
    ):
        return

    acting_user_id = helpers.safe_get(body, "user", "id") or user_id
    team_id = _extract_team_id(body) or workspace_record.team_id
    channel_name, _is_private = helpers.lookup_channel_meta(
        channel_id,
        workspace_record,
        user_token=helpers.get_user_token(team_id, acting_user_id),
        client=client,
    )

    try:
        sync_record = schemas.Sync(
            title=_sanitize_text(channel_name),
            description=None,
            group_id=group_id,
            sync_mode="group",
            target_workspace_id=None,
            publisher_workspace_id=workspace_record.id,
        )
        DbManager.create_record(sync_record)

        sync_channel_record = schemas.SyncChannel(
            sync_id=sync_record.id,
            channel_id=channel_id,
            workspace_id=workspace_record.id,
            created_at=datetime.now(UTC),
            reaction_style=reaction_style,
            publishes=publishes,
            subscribes=subscribes,
        )
        DbManager.create_record(sync_channel_record)
    except Exception as e:
        _logger.error(f"Failed to create Sync for channel {channel_id}: {e}")
        return

    helpers.invalidate_channel_memberships(channel_id)

    # Membership comes after the rows exist: Slack fires ``member_joined_channel``
    # as soon as the bot is added, and that handler leaves any channel with no
    # SyncChannel. Writing first is what lets a private channel work at all.
    if not _ensure_membership_or_rollback(
        client,
        channel_id,
        team_id=team_id,
        acting_user_id=acting_user_id,
        rollback=lambda: helpers.purge_sync(sync_record.id),
        log_event="create_sync_membership_failed",
        log_extra={"workspace_id": workspace_record.id, "channel_id": channel_id, "sync_id": sync_record.id},
        context=context,
    ):
        return

    refreshed_name, _is_private = helpers.lookup_channel_meta(channel_id, workspace_record)
    if refreshed_name and refreshed_name != channel_id and refreshed_name != sync_record.title:
        DbManager.update_records(
            schemas.Sync,
            [schemas.Sync.id == sync_record.id],
            {schemas.Sync.title: _sanitize_text(refreshed_name)},
        )
        sync_record.title = refreshed_name

    admin_name, _admin_label = helpers.format_admin_label(client, acting_user_id, workspace_record)
    try:
        client.chat_postMessage(channel=channel_id, text=_create_sync_notice(admin_name, publishes, subscribes))
    except Exception as exc:
        _logger.warning(
            "create_sync_announce_failed",
            extra={"channel_id": channel_id, "sync_id": sync_record.id, "error": str(exc)},
        )

    _logger.info(
        "sync_created",
        extra={
            "workspace_id": workspace_record.id,
            "channel_id": channel_id,
            "group_id": group_id,
            "sync_id": sync_record.id,
            "publishes": publishes,
            "subscribes": subscribes,
        },
    )

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    _refresh_group_member_homes(group_id, workspace_record.id, logger, context=context)


def handle_leave_sync(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Show a confirmation modal before leaving a channel sync."""
    sync_id = _sync_id_from_action(body, (actions.CONFIG_LEAVE_SYNC,))
    if not sync_id:
        _logger.warning("leave_sync_invalid_id", extra={"action_id": helpers.safe_get(body, "actions", 0, "action_id")})
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    confirm_blocks: list[orm.BaseBlock] = []
    last_publisher = False
    auth_result = _get_authorized_workspace(body, client, context, "leave_sync")
    workspace_record = None
    if auth_result:
        _, workspace_record = auth_result
        sync_record = DbManager.get_record(schemas.Sync, sync_id)
        all_channels = DbManager.find_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.sync_id == sync_id, schemas.SyncChannel.deleted_at.is_(None)],
        )
        my_channel = next((row for row in all_channels if row.workspace_id == workspace_record.id), None)
        last_publisher = _is_last_publisher(all_channels, workspace_record.id)
        ctx = _relationship_context(
            group_id=getattr(sync_record, "group_id", None) if sync_record else None,
            local_channel_id=my_channel.channel_id if my_channel else None,
            local_workspace=workspace_record,
        )
        if ctx:
            confirm_blocks.append(ctx)

    if last_publisher:
        confirm_blocks.append(
            section(
                ":warning: *You are the last publisher in this Sync.*\n\n"
                "Leaving will:\n"
                "\u2022 End this Sync for every Workspace\n"
                "\u2022 Delete Sync history for every participating Channel\n\n"
                "Anyone in the Group can Create Sync later. "
                "_No messages will be deleted from Slack — only SyncBot's tracking history is removed._"
            )
        )
    else:
        confirm_blocks.append(
            section(
                ":warning: *Are you sure you want to leave this Sync?*\n\n"
                "This will:\n"
                "\u2022 Remove your Workspace's Sync history for this Channel\n"
                "\u2022 Remove this Channel from the Sync\n"
                "\u2022 Other Workspaces in the Sync will continue uninterrupted\n\n"
                "_No messages will be deleted from any Channel — only SyncBot's tracking history for your Workspace is removed._"
            )
        )
    confirm_form = orm.BlockView(
        blocks=confirm_blocks
        + [
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=":octagonal_sign: Leave Sync",
                        action=actions.CONFIG_LEAVE_SYNC_CONFIRM,
                        value=str(sync_id),
                        style="danger",
                    ),
                ]
            ),
        ]
    )

    confirm_form.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_LEAVE_SYNC_CONFIRM,
        title_text="Leave Sync",
        submit_button_text=None,
        close_button_text="Cancel",
        parent_metadata={"sync_id": sync_id, "last_publisher": last_publisher},
        body=body,
    )


def handle_leave_sync_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Remove this workspace from the sync; purge everyone if the last publisher leaves."""
    auth_result = _get_authorized_workspace(body, client, context, "leave_sync_confirm")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    meta = _parse_private_metadata(body)
    sync_id = meta.get("sync_id")
    if not sync_id:
        _logger.warning("leave_sync_confirm: missing sync_id in metadata")
        return

    sync_record = DbManager.get_record(schemas.Sync, id=sync_id)
    admin_name, admin_label = helpers.format_admin_label(client, user_id, workspace_record)

    all_channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == sync_id, schemas.SyncChannel.deleted_at.is_(None)],
    )
    my_channel = next((c for c in all_channels if c.workspace_id == workspace_record.id), None)
    other_channels = [c for c in all_channels if c.workspace_id != workspace_record.id]
    last_publisher = _is_last_publisher(all_channels, workspace_record.id)
    end_sync = last_publisher or not other_channels

    for sync_channel in all_channels:
        try:
            channel_ws = helpers.get_workspace_by_id(sync_channel.workspace_id)
            if not channel_ws or not channel_ws.bot_token:
                continue
            name = admin_name if sync_channel.workspace_id == workspace_record.id else admin_label
            if end_sync:
                msg = f":octagonal_sign: *{name}* left this Sync. The Sync has ended."
            else:
                msg = f":octagonal_sign: *{name}* left this Sync."
            ws_client = WebClient(token=helpers.decrypt_bot_token(channel_ws.bot_token))
            helpers.notify_synced_channels(ws_client, [sync_channel.channel_id], msg)
        except Exception as e:
            _logger.warning(f"Failed to notify channel {sync_channel.channel_id}: {e}")

    group_id = sync_record.group_id if sync_record else None
    if last_publisher:
        for sync_channel in all_channels:
            try:
                member_ws = helpers.get_workspace_by_id(sync_channel.workspace_id)
                if member_ws and member_ws.bot_token:
                    member_client = WebClient(token=helpers.decrypt_bot_token(member_ws.bot_token))
                    member_client.conversations_leave(channel=sync_channel.channel_id)
            except Exception as e:
                _logger.warning(f"Failed to leave channel {sync_channel.channel_id}: {e}")
        try:
            helpers.purge_sync(sync_id)
        except Exception as exc:
            _logger.error(
                "leave_sync_failed",
                extra={"sync_id": sync_id, "group_id": group_id, "error": str(exc)},
            )
            with contextlib.suppress(Exception):
                helpers.notify_admins_dm(
                    client,
                    helpers.format_error_dm(
                        ":warning: Leaving that Sync failed, so it is still active. "
                        "Please try again, and let your SyncBot operator know if it keeps failing.",
                        {"error": str(exc), "event": "leave_sync_failed", "sync": sync_id},
                    ),
                    team_id=workspace_record.team_id if workspace_record else None,
                )
            return
    else:
        if my_channel:
            helpers.purge_sync_channels([my_channel])
            try:
                client.conversations_leave(channel=my_channel.channel_id)
            except Exception as e:
                _logger.warning(f"Failed to leave channel {my_channel.channel_id}: {e}")
        if not other_channels:
            helpers.purge_sync(sync_id)

    _logger.info(
        "sync_left",
        extra={
            "sync_id": sync_id,
            "workspace_id": workspace_record.id,
            "channel_id": my_channel.channel_id if my_channel else None,
            "ended": end_sync,
        },
    )

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    if group_id:
        _refresh_group_member_homes(group_id, workspace_record.id, logger, context=context)
    _close_modal_done(client, body, ":octagonal_sign: You left the Sync. You can close this now.")


def _open_pause_resume_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
    *,
    prefixes: tuple[str, ...],
    confirm_action: str,
    title: str,
    button_label: str,
    warning: str,
    log_event: str,
) -> None:
    sync_id = _sync_id_from_action(body, prefixes)
    if not sync_id:
        _logger.warning(
            f"{log_event}_invalid_id", extra={"action_id": helpers.safe_get(body, "actions", 0, "action_id")}
        )
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    confirm_blocks: list[orm.BaseBlock] = []
    auth_result = _get_authorized_workspace(body, client, context, log_event)
    if auth_result:
        _, workspace_record = auth_result
        sync_record = DbManager.get_record(schemas.Sync, sync_id)
        my_channel = next(
            (
                row
                for row in DbManager.find_records(
                    schemas.SyncChannel,
                    [
                        schemas.SyncChannel.sync_id == sync_id,
                        schemas.SyncChannel.workspace_id == workspace_record.id,
                        schemas.SyncChannel.deleted_at.is_(None),
                    ],
                )
            ),
            None,
        )
        ctx = _relationship_context(
            group_id=getattr(sync_record, "group_id", None) if sync_record else None,
            local_channel_id=my_channel.channel_id if my_channel else None,
            local_workspace=workspace_record,
        )
        if ctx:
            confirm_blocks.append(ctx)
    confirm_blocks.append(section(warning))
    confirm_form = orm.BlockView(
        blocks=confirm_blocks
        + [
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=button_label,
                        action=confirm_action,
                        value=str(sync_id),
                    ),
                ]
            ),
        ]
    )
    confirm_form.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=confirm_action,
        title_text=title,
        submit_button_text=None,
        close_button_text="Cancel",
        parent_metadata={"sync_id": sync_id},
        body=body,
    )


def _toggle_sync_status(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
    *,
    target_status: str,
    emoji: str,
    verb: str,
    log_event: str,
    done_message: str,
) -> None:
    """Pause or resume this workspace's channel only."""
    meta = _parse_private_metadata(body)
    sync_id = meta.get("sync_id")
    if not sync_id:
        sync_id = _sync_id_from_action(body, (actions.CONFIG_PAUSE_SYNC, actions.CONFIG_RESUME_SYNC))
    if not sync_id:
        _logger.warning(f"{log_event}_invalid_id")
        return

    auth_result = _get_authorized_workspace(body, client, context, log_event)
    if not auth_result:
        return
    user_id, workspace_record = auth_result
    admin_name, _admin_label = helpers.format_admin_label(client, user_id, workspace_record)

    all_channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == sync_id, schemas.SyncChannel.deleted_at.is_(None)],
    )
    my_sync_channel = next(
        (c for c in all_channels if c.workspace_id == workspace_record.id),
        None,
    )
    if not my_sync_channel:
        _logger.warning(
            f"{log_event}_no_channel_for_workspace", extra={"sync_id": sync_id, "workspace_id": workspace_record.id}
        )
        return

    DbManager.update_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.id == my_sync_channel.id],
        {schemas.SyncChannel.status: target_status},
    )
    helpers.invalidate_channel_memberships(my_sync_channel.channel_id)

    try:
        if workspace_record.bot_token:
            ws_client = WebClient(token=helpers.decrypt_bot_token(workspace_record.bot_token))
            if target_status == "active":
                try:
                    helpers.ensure_bot_in_conversation(
                        ws_client,
                        my_sync_channel.channel_id,
                        team_id=workspace_record.team_id,
                        acting_user_id=user_id,
                        context=context,
                    )
                except Exception as exc:
                    _logger.warning(
                        "resume_sync_membership_failed",
                        extra={"channel_id": my_sync_channel.channel_id, "error": str(exc)},
                    )
            helpers.notify_synced_channels(
                ws_client,
                [my_sync_channel.channel_id],
                f":{emoji}: *{admin_name}* {verb} this Sync.",
            )
    except Exception as e:
        _logger.warning(f"Failed to notify channel {my_sync_channel.channel_id} about {verb}: {e}")

    _logger.info(log_event, extra={"sync_id": sync_id, "sync_channel_id": my_sync_channel.id})

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    sync_record = DbManager.get_record(schemas.Sync, id=sync_id)
    if sync_record and sync_record.group_id:
        _refresh_group_member_homes(
            sync_record.group_id, workspace_record.id if workspace_record else 0, logger, context=context
        )
    _close_modal_done(client, body, done_message)


def handle_pause_sync(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Show a confirmation modal before pausing."""
    _open_pause_resume_confirm(
        body,
        client,
        logger,
        context,
        prefixes=(actions.CONFIG_PAUSE_SYNC,),
        confirm_action=actions.CONFIG_PAUSE_SYNC_CONFIRM,
        title="Pause Sync",
        button_label=":double_vertical_bar: Pause Sync",
        warning=":double_vertical_bar: *Pause this Sync?*\n\nMessages, threads, and reactions will not sync until you Resume Sync.",
        log_event="pause_sync",
    )


def handle_pause_sync_confirm(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Pause an active channel sync."""
    _toggle_sync_status(
        body,
        client,
        logger,
        context,
        target_status="paused",
        emoji="double_vertical_bar",
        verb="paused",
        log_event="sync_paused",
        done_message=":double_vertical_bar: Sync paused. You can close this now.",
    )


def handle_resume_sync(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Show a confirmation modal before resuming."""
    _open_pause_resume_confirm(
        body,
        client,
        logger,
        context,
        prefixes=(actions.CONFIG_RESUME_SYNC,),
        confirm_action=actions.CONFIG_RESUME_SYNC_CONFIRM,
        title="Resume Sync",
        button_label=":arrow_forward: Resume Sync",
        warning=":arrow_forward: *Resume this Sync?*\n\nMessages, threads, and reactions will start syncing again.",
        log_event="resume_sync",
    )


def handle_resume_sync_confirm(body: dict, client: WebClient, logger: Logger, context: dict) -> None:
    """Resume a paused channel sync."""
    _toggle_sync_status(
        body,
        client,
        logger,
        context,
        target_status="active",
        emoji="arrow_forward",
        verb="resumed",
        log_event="sync_resumed",
        done_message=":arrow_forward: Sync resumed. You can close this now.",
    )


def handle_join_sync(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the one-step Join Sync modal."""
    auth_result = _get_authorized_workspace(body, client, context, "join_sync")
    if not auth_result:
        return
    _, workspace_record = auth_result

    trigger_id = helpers.safe_get(body, "trigger_id")
    sync_id = helpers.safe_get(body, "actions", 0, "value")
    if not sync_id:
        _logger.warning("join_sync: missing sync_id")
        return

    sync_record = DbManager.get_record(schemas.Sync, int(sync_id))
    pub_ch = _publisher_sync_channel(int(sync_id))
    pub_ws = helpers.get_workspace_by_id(pub_ch.workspace_id) if pub_ch else None

    blocks: list[orm.BaseBlock] = []
    ctx = _relationship_context(
        group_id=getattr(sync_record, "group_id", None) if sync_record else None,
        published_channel=pub_ch,
        published_workspace=pub_ws,
    )
    if ctx:
        blocks.append(ctx)
    blocks.extend(
        [
            _participation_block(mode="join"),
            block_context(_participation_help(mode="join")),
            _channel_picker_block(
                "Channel",
                actions.CONFIG_JOIN_SYNC_SELECT,
                team_id=workspace_record.team_id,
            ),
            block_context(_channel_picker_help_text(team_id=workspace_record.team_id, subscribe=True)),
            _reaction_style_block(actions.CONFIG_SYNC_REACTION_STYLE),
            block_context(_reaction_style_help(first_time=True)),
        ]
    )

    orm.BlockView(blocks=blocks).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_JOIN_SYNC_SUBMIT,
        title_text="Join Sync",
        submit_button_text="Join Sync",
        parent_metadata={"sync_id": int(sync_id)},
        new_or_add="new",
        body=body,
    )


def handle_join_sync_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase for Join Sync: surface a visible error, or ack empty on success.

    The native picker cannot pre-exclude ineligible channels, so an invalid
    choice has to be reported here rather than returning silently and leaving the
    user with a modal that appeared to work.
    """
    auth_result = _get_authorized_workspace(body, client, context, "join_sync_submit")
    if not auth_result:
        return None
    user_id, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    if not metadata.get("sync_id"):
        _logger.warning("join_sync_submit: missing sync_id")
        return None

    channel_id, picker_action = _selected_channel(body, _JOIN_CHANNEL_ACTIONS)

    return _validate_channel_selection(
        client,
        channel_id,
        picker_action,
        team_id=_extract_team_id(body) or workspace_record.team_id,
        acting_user_id=helpers.safe_get(body, "user", "id") or user_id,
        workspace_id=workspace_record.id,
        source_sync_id=int(metadata["sync_id"]),
    )


def handle_join_sync_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Join an available channel sync: create SyncChannel for this workspace."""
    auth_result = _get_authorized_workspace(body, client, context, "join_sync_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    sync_id = metadata.get("sync_id")

    if not sync_id:
        _logger.warning("join_sync_submit: missing sync_id")
        return

    channel_id, picker_action = _selected_channel(body, _JOIN_CHANNEL_ACTIONS)

    # The ack phase already surfaced any error; this keeps the work phase from
    # writing on a payload it should reject.
    if _validate_channel_selection(
        client,
        channel_id,
        picker_action,
        team_id=_extract_team_id(body) or workspace_record.team_id,
        acting_user_id=helpers.safe_get(body, "user", "id") or user_id,
        workspace_id=workspace_record.id,
        source_sync_id=int(sync_id),
    ):
        return

    sync_record = DbManager.get_record(schemas.Sync, id=sync_id)
    if not sync_record:
        return

    group_id = sync_record.group_id

    existing_sub = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.sync_id == sync_id,
            schemas.SyncChannel.workspace_id == workspace_record.id,
            schemas.SyncChannel.channel_id == channel_id,
            schemas.SyncChannel.deleted_at.is_(None),
            schemas.SyncChannel.status == "active",
        ],
    )
    if existing_sub:
        _logger.info(
            "join_sync_duplicate_skip",
            extra={
                "sync_id": sync_id,
                "channel_id": channel_id,
                "workspace_id": workspace_record.id,
            },
        )
        builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
        if group_id:
            _refresh_group_member_homes(group_id, workspace_record.id, logger, context=context)
        return

    acting_user_id = helpers.safe_get(body, "user", "id") or user_id
    admin_label = _admin_workspace_label(client, acting_user_id, workspace_record)
    admin_name, _ = helpers.format_admin_label(client, acting_user_id, workspace_record)

    team_id = _extract_team_id(body) or workspace_record.team_id

    reaction_style = _parse_reaction_fields(body)
    publishes, subscribes = _participation_from_body(body)

    try:
        sync_channel_record = schemas.SyncChannel(
            sync_id=sync_id,
            channel_id=channel_id,
            workspace_id=workspace_record.id,
            created_at=datetime.now(UTC),
            reaction_style=reaction_style,
            publishes=publishes,
            subscribes=subscribes,
        )
        DbManager.create_record(sync_channel_record)
    except Exception as e:
        _logger.error(f"Failed to join channel sync {sync_id}: {e}")
        return

    helpers.invalidate_channel_memberships(channel_id)

    # Same ordering as publish: the row has to exist before Slack announces the
    # bot joined, or the unconfigured-channel handler shows it the door.
    if not _ensure_membership_or_rollback(
        client,
        channel_id,
        team_id=team_id,
        acting_user_id=acting_user_id,
        rollback=lambda: helpers.purge_sync_channels([sync_channel_record]),
        log_event="join_sync_membership_failed",
        log_extra={"workspace_id": workspace_record.id, "channel_id": channel_id, "sync_id": sync_id},
        context=context,
    ):
        return

    peer_channels: list = []
    try:
        peer_channels = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == sync_id,
                schemas.SyncChannel.deleted_at.is_(None),
                schemas.SyncChannel.workspace_id != workspace_record.id,
            ],
        )
        joiner_publishes = helpers.channel_publishes(sync_channel_record)
        joiner_subscribes = helpers.channel_subscribes(sync_channel_record)
        local_ref = _format_channel_ref(channel_id, workspace_record, is_local=False)

        try:
            if peer_channels:
                peer = peer_channels[0]
                peer_ws = helpers.get_workspace_by_id(peer.workspace_id)
                channel_ref = _format_channel_ref(peer.channel_id, peer_ws, is_local=False)
            else:
                channel_ref = sync_record.title or "the other Channel"
            client.chat_postMessage(
                channel=channel_id,
                text=_join_notice(
                    admin_label=admin_name,
                    other_ref=channel_ref,
                    here_publishes=joiner_publishes,
                    here_subscribes=joiner_subscribes,
                    there_publishes=helpers.channel_publishes(peer_channels[0]) if peer_channels else True,
                    there_subscribes=helpers.channel_subscribes(peer_channels[0]) if peer_channels else True,
                    joined=False,
                ),
            )
        except Exception as exc:
            _logger.debug(f"join_sync: failed to notify joining channel {channel_id}: {exc}")

        for peer in peer_channels:
            try:
                peer_ws = helpers.get_workspace_by_id(peer.workspace_id)
                if peer_ws:
                    pub_client = WebClient(token=helpers.decrypt_bot_token(peer_ws.bot_token))
                    pub_client.chat_postMessage(
                        channel=peer.channel_id,
                        text=_join_notice(
                            admin_label=admin_label,
                            other_ref=local_ref,
                            here_publishes=helpers.channel_publishes(peer),
                            here_subscribes=helpers.channel_subscribes(peer),
                            there_publishes=joiner_publishes,
                            there_subscribes=joiner_subscribes,
                            joined=True,
                        ),
                    )
            except Exception as exc:
                _logger.debug(f"join_sync: failed to notify peer channel {peer.channel_id}: {exc}")

        _logger.info(
            "sync_joined",
            extra={
                "workspace_id": workspace_record.id,
                "channel_id": channel_id,
                "sync_id": sync_id,
                "group_id": group_id,
            },
        )
    except Exception as e:
        _logger.error(f"Failed to join channel sync {sync_id}: {e}")

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    if group_id:
        _refresh_group_member_homes(group_id, workspace_record.id, logger, context=context)


def _parse_edit_sync_ref(body: dict) -> tuple[str | None, int | None]:
    """Parse Edit button value ``c:{sync_channel_id}`` or ``s:{sync_id}``.

    Channel and Sync PKs both autoincrement from 1, so a bare integer is unsafe.
    """
    action_data = helpers.safe_get(body, "actions", 0) or {}
    raw = (action_data.get("value") or "").strip()
    if not raw and action_data.get("action_id"):
        # Fallback from action_id edit_sync_c_12 / edit_sync_s_12
        aid = action_data.get("action_id") or ""
        prefix = f"{actions.CONFIG_EDIT_SYNC}_"
        if aid.startswith(prefix):
            rest = aid[len(prefix) :]
            if rest.startswith("c_"):
                raw = f"c:{rest[2:]}"
            elif rest.startswith("s_"):
                raw = f"s:{rest[2:]}"
    if raw.startswith("c:"):
        try:
            return "channel", int(raw[2:])
        except (TypeError, ValueError):
            return None, None
    if raw.startswith("s:"):
        try:
            return "sync", int(raw[2:])
        except (TypeError, ValueError):
            return None, None
    return None, None


def _sync_channel_by_pk(sync_channel_id: int) -> schemas.SyncChannel | None:
    """Look up SyncChannel by integer PK (``get_record`` uses Slack ``channel_id``)."""
    rows = DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.id == sync_channel_id])
    return rows[0] if rows else None


def handle_edit_sync(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the one-step Edit Sync modal for this channel's participation."""
    auth_result = _get_authorized_workspace(body, client, context, "edit_sync")
    if not auth_result:
        return
    _, workspace_record = auth_result

    kind, ref_id = _parse_edit_sync_ref(body)
    if not kind or not ref_id:
        _logger.warning("edit_sync: invalid action value")
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    sync_channel: schemas.SyncChannel | None = None
    sync_record: schemas.Sync | None = None

    if kind == "channel":
        sync_channel = _sync_channel_by_pk(ref_id)
        if not sync_channel or sync_channel.deleted_at:
            return
        if sync_channel.workspace_id != workspace_record.id:
            _logger.warning("edit_sync: channel not in acting workspace")
            return
        sync_record = DbManager.get_record(schemas.Sync, id=sync_channel.sync_id)
    else:
        # Available cards no longer expose sync-wide policy editing.
        return

    if not sync_record:
        return

    blocks: list[orm.BaseBlock] = []
    metadata: dict = {"sync_id": sync_record.id}
    if sync_channel:
        metadata["sync_channel_id"] = sync_channel.id
        publishes = helpers.channel_publishes(sync_channel)
        subscribes = helpers.channel_subscribes(sync_channel)
        ctx = _relationship_context(
            group_id=sync_record.group_id,
            local_channel_id=sync_channel.channel_id,
            local_workspace=workspace_record,
        )
        if ctx:
            blocks.append(ctx)
        blocks.append(
            _participation_block(
                mode="edit",
                publishes=publishes,
                subscribes=subscribes,
            )
        )
        blocks.append(block_context(_participation_help(mode="edit")))
        style_initial = _reaction_style_for_edit(sync_channel) or constants.DEFAULT_REACTION_STYLE_NEW_RECEIVE
        blocks.append(
            _reaction_style_block(
                actions.CONFIG_SYNC_REACTION_STYLE,
                initial=style_initial,
            )
        )
        blocks.append(block_context(_reaction_style_help(first_time=False)))

    if not blocks:
        return

    orm.BlockView(blocks=blocks).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_EDIT_SYNC_SUBMIT,
        title_text="Edit Sync",
        submit_button_text="Save",
        parent_metadata=metadata,
        new_or_add="new",
        body=body,
    )


def handle_edit_sync_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase for the participation-only Edit modal."""
    auth_result = _get_authorized_workspace(body, client, context, "edit_sync_submit_ack")
    if not auth_result:
        return None
    return None


def handle_edit_sync_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Persist this channel's participation and reaction type."""
    auth_result = _get_authorized_workspace(body, client, context, "edit_sync_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    metadata = _parse_private_metadata(body)
    sync_id = metadata.get("sync_id")
    sync_channel_id = metadata.get("sync_channel_id")
    if not sync_id:
        return

    sync_record = DbManager.get_record(schemas.Sync, id=int(sync_id))
    if not sync_record:
        return

    if sync_channel_id:
        sync_channel = _sync_channel_by_pk(int(sync_channel_id))
        if sync_channel and sync_channel.workspace_id == workspace_record.id:
            publishes, subscribes = _participation_from_body(body)
            from helpers.reaction import update_sync_channel_reactions

            style = _reaction_style_for_edit(sync_channel, body, subscribes=subscribes)
            if subscribes and style is None:
                from helpers.reaction import default_reaction_style_for_new_channel

                style = default_reaction_style_for_new_channel(subscribes=True)
            update_sync_channel_reactions(
                int(sync_channel_id),
                style=style,
                publishes=publishes,
                subscribes=subscribes,
            )

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
    if sync_record.group_id:
        _refresh_group_member_homes(sync_record.group_id, workspace_record.id, logger, context=context)


def _refresh_group_member_homes(
    group_id: int,
    exclude_workspace_id: int,
    logger: Logger,
    context: dict | None = None,
) -> None:
    """Invalidate Home caches for other group members (no users.list fan-out).

    Partner workspaces rebuild on the next ``app_home_opened`` or Refresh.
    """
    members = _get_group_members(group_id)
    refreshed: set[int] = set()
    for member in members:
        if not member.workspace_id or member.workspace_id == exclude_workspace_id or member.workspace_id in refreshed:
            continue
        member_ws = helpers.get_workspace_by_id(member.workspace_id, context=context)
        if member_ws:
            builders.refresh_home_tab_for_workspace(member_ws, logger, context=None, user_id=None)
            refreshed.add(member.workspace_id)
