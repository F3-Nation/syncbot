"""Home tab builder."""

import hashlib
import logging
from logging import Logger

from slack_sdk.web import WebClient

import helpers
from builders._common import (
    _get_group_members,
    _get_groups_for_workspace,
    _get_team_id,
    _get_user_id,
    _get_workspace_info,
)
from builders.channel_sync import _build_inline_channel_sync
from db import DbManager
from db.schemas import (
    FederatedWorkspace,
    Sync,
    SyncChannel,
    UserMapping,
    Workspace,
    WorkspaceGroup,
    WorkspaceGroupMember,
)
from slack import actions, orm
from slack.blocks import context as block_context
from slack.blocks import divider, header, section

_logger = logging.getLogger(__name__)


def _prefetch_group_channel_and_mapping_counts(
    group_id: int, sync_ids: list[int]
) -> tuple[dict[int, list], dict[int, int], dict[int, int]]:
    """Return ``(channels_by_sync, channel_count_by_ws, mapped_count_by_ws)``."""
    channels_by_sync: dict[int, list] = {}
    channel_count_by_ws: dict[int, int] = {}
    if sync_ids:
        channels = DbManager.find_records(
            SyncChannel,
            [
                SyncChannel.sync_id.in_(sync_ids),
                SyncChannel.deleted_at.is_(None),
            ],
        )
        for row in channels:
            channels_by_sync.setdefault(row.sync_id, []).append(row)
            if row.workspace_id:
                channel_count_by_ws[row.workspace_id] = channel_count_by_ws.get(row.workspace_id, 0) + 1
    mapped_count_by_ws: dict[int, int] = {}
    mappings = DbManager.find_records(
        UserMapping,
        [
            UserMapping.group_id == group_id,
            UserMapping.map_method != "none",
        ],
    )
    for row in mappings:
        if row.target_workspace_id:
            mapped_count_by_ws[row.target_workspace_id] = mapped_count_by_ws.get(row.target_workspace_id, 0) + 1
    return channels_by_sync, channel_count_by_ws, mapped_count_by_ws


def _home_tab_content_hash(
    workspace_record: Workspace,
    user_id: str | None = None,
    *,
    is_manager: bool = False,
    is_admin: bool = False,
    extra_manager_ids: tuple[str, ...] = (),
) -> str:
    """Compute a stable hash of the data that drives the Home tab.

    *user_id* is part of the payload because one block on Home is per person: the
    Authorize SyncBot section, which disappears once that user has granted every
    current user-scope group. Without it, a Refresh right after authorizing would
    replay cached blocks that still show the button. Granted scopes are hashed too,
    so adding a scope later busts the cache and the section comes back with the
    already-allowed list filled in.

    Non-managers only see Authorize, Refresh, and the lock line, so their hash skips
    groups and syncs. Managers and admins share the full payload; ``is_admin`` and
    ``extra_manager_ids`` bust the cache when Settings visibility changes.
    """
    workspace_id = workspace_record.id
    workspace_name = (workspace_record.workspace_name or "") or ""
    permission_lists = tuple(helpers.user_permission_lists(workspace_record.team_id, user_id)) if user_id else ((), ())
    role_sig = (is_manager, is_admin, extra_manager_ids)
    if not is_manager:
        payload = (workspace_id, workspace_name, user_id or "", permission_lists, role_sig)
        return hashlib.sha256(repr(payload).encode()).hexdigest()

    reset_visible = helpers.is_db_reset_visible_for_workspace(workspace_record.team_id)
    my_groups = _get_groups_for_workspace(workspace_id)
    group_ids = sorted(g.id for g, _ in my_groups)
    pending_invites = DbManager.find_records(
        WorkspaceGroupMember,
        [
            WorkspaceGroupMember.workspace_id == workspace_id,
            WorkspaceGroupMember.status == "pending",
            WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    pending_ids = tuple(sorted(inv.id for inv in pending_invites))
    group_payload: list[tuple] = []
    for group, _ in my_groups:
        members = _get_group_members(group.id)
        syncs = DbManager.find_records(Sync, [Sync.group_id == group.id])
        sync_ids = [s.id for s in syncs]
        channels_by_sync, ch_by_ws, mapped_by_ws = _prefetch_group_channel_and_mapping_counts(group.id, sync_ids)
        sync_channel_tuples: list[tuple] = []
        for sync in syncs:
            channels = channels_by_sync.get(sync.id, [])
            channel_sig = tuple(
                (
                    sync_channel.workspace_id,
                    sync_channel.channel_id,
                    sync_channel.status or "active",
                    helpers.channel_publishes(sync_channel),
                    helpers.channel_subscribes(sync_channel),
                )
                for sync_channel in sorted(channels, key=lambda c: (c.workspace_id, c.channel_id))
            )
            sync_channel_tuples.append((sync.id, channel_sig))
        sync_channel_tuples.sort(key=lambda x: x[0])
        member_sigs: list[tuple] = []
        for member in members:
            ws_id = member.workspace_id or 0
            ch_count = ch_by_ws.get(ws_id, 0) if ws_id else 0
            mapped_count = mapped_by_ws.get(ws_id, 0) if ws_id else 0
            member_sigs.append((ws_id, ch_count, mapped_count))
        member_sigs.sort(key=lambda x: x[0])
        group_payload.append((group.id, len(members), len(syncs), tuple(sync_channel_tuples), tuple(member_sigs)))
    group_payload.sort(key=lambda x: x[0])
    payload = (
        workspace_id,
        workspace_name,
        tuple(group_ids),
        tuple(group_payload),
        pending_ids,
        reset_visible,
        user_id or "",
        permission_lists,
        role_sig,
    )
    return hashlib.sha256(repr(payload).encode()).hexdigest()


def home_tab_hash_key(team_id: str, user_id: str) -> str:
    """Cache key for a Home tab content hash.

    Per user, since the Authorize SyncBot section is per user. Restore-time
    invalidation still works: ``invalidate_home_tab_caches_for_team`` deletes by
    the ``home_tab_hash:{team_id}`` prefix.
    """
    return f"home_tab_hash:{team_id}:{user_id}"


def _build_authorize_section(blocks: list, team_id: str, user_id: str, context: dict | None = None) -> bool:
    """Prepend the Authorize SyncBot section when this user still needs to authorize.

    Slack will not let a bot add itself to a private channel; only a member can,
    with that member's own user token. This button is the OAuth install that mints
    it (or refreshes it when we add scopes later). Shown to everyone, admin or not,
    because authorization is about acting as that person rather than about
    configuring SyncBot.

    When they already granted some permissions, those stay listed with checkmarks
    so a later scope change looks like an addition rather than a redo. A first-time
    visitor has nothing granted yet, so that list is omitted.
    """
    if not helpers.needs_user_authorization(team_id, user_id):
        return False

    url = helpers.authorize_url(team_id, context=context)
    if not url:
        # Single-workspace/local mode has no OAuth flow, so there is nothing to
        # link to and a button would be a dead end.
        return False

    already, needed = helpers.user_permission_lists(team_id, user_id)

    blocks.append(header("Authorize SyncBot"))
    blocks.append(block_context("_Allow SyncBot to act on your behalf in this Slack Workspace._"))
    if already:
        checks = "\n".join(f":white_check_mark: {label}" for label in already)
        blocks.append(block_context(f"*Already allowed permissions:*\n{checks}"))
    if needed:
        dashes = "\n".join(f"- {label}" for label in needed)
        blocks.append(block_context(f"*Needed permissions:*\n{dashes}"))
    blocks.append(
        orm.ActionsBlock(
            elements=[
                orm.ButtonElement(
                    label="Authorize SyncBot",
                    action=actions.CONFIG_AUTHORIZE_SYNCBOT,
                    url=url,
                ),
            ]
        )
    )
    blocks.append(divider())
    return True


def _build_configuration_section(
    blocks: list,
    workspace_record: Workspace,
    *,
    is_admin: bool,
) -> None:
    """Append SyncBot Configuration, directly under Authorize.

    *Refresh* is for everyone so a non-manager can reload Home after revoking.
    Settings, Backup/Restore, and Reset require Slack admin/owner (and primary
    workspace where applicable).
    """
    blocks.append(header("SyncBot Configuration"))
    config_buttons = [
        orm.ButtonElement(
            label=":arrows_counterclockwise: Refresh",
            action=actions.CONFIG_REFRESH_HOME,
        ),
    ]
    if is_admin:
        if helpers.is_settings_visible_for_workspace(workspace_record.team_id):
            config_buttons.append(
                orm.ButtonElement(
                    label=":gear: Settings",
                    action=actions.CONFIG_OPEN_SETTINGS,
                ),
            )
        if helpers.is_backup_visible_for_workspace(workspace_record.team_id):
            config_buttons.append(
                orm.ButtonElement(
                    label=":floppy_disk: Backup/Restore",
                    action=actions.CONFIG_BACKUP_RESTORE,
                ),
            )
        if helpers.is_db_reset_visible_for_workspace(workspace_record.team_id):
            config_buttons.append(
                orm.ButtonElement(
                    label=":bomb: Reset Database",
                    action=actions.CONFIG_DB_RESET,
                    style="danger",
                ),
            )
    blocks.append(orm.ActionsBlock(elements=config_buttons))


def refresh_home_tab_for_workspace(
    workspace: Workspace,
    logger: Logger,
    context: dict | None = None,
    *,
    user_id: str | None = None,
) -> None:
    """Invalidate Home caches for *workspace*, then publish for *user_id* when set.

    Never walks ``users.list`` to fan out. Other viewers rebuild on the next
    ``app_home_opened`` or Refresh after hash invalidation.
    """
    if not workspace or workspace.deleted_at:
        return
    team_id = getattr(workspace, "team_id", None)
    if not team_id:
        return

    from helpers.export_import import invalidate_home_tab_caches_for_team

    # Wipe first so a later publish rewrites this user's keys; do not reverse.
    invalidate_home_tab_caches_for_team(team_id)

    if not user_id or not workspace.bot_token:
        return

    ctx = context if context is not None else {}
    try:
        ws_client = WebClient(token=helpers.decrypt_bot_token(workspace.bot_token))
        synthetic_body = {"team": {"id": team_id}}
        build_home_tab(synthetic_body, ws_client, logger, ctx, user_id=user_id, workspace=workspace)
    except Exception as e:
        _logger.warning(
            "refresh_home_tab_for_workspace: failed for user %s in workspace %s: %s",
            user_id,
            team_id,
            e,
        )


def build_home_tab(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
    *,
    user_id: str | None = None,
    return_blocks: bool = False,
    workspace: Workspace | None = None,
) -> list[dict] | None:
    """Build and publish the App Home tab. If return_blocks is True, return block dicts and do not publish."""
    team_id = _get_team_id(body)
    user_id = user_id or _get_user_id(body)
    if not team_id or not user_id:
        _logger.warning("build_home_tab: missing team_id or user_id")
        return None

    if workspace is not None:
        workspace_record = workspace
    else:
        workspace_record: Workspace = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return None

    is_admin = helpers.is_workspace_admin(client, user_id)
    is_manager = helpers.is_workspace_manager(client, user_id, team_id)
    extra_manager_ids = tuple(sorted(helpers.extra_manager_user_ids(team_id)))

    blocks: list[orm.BaseBlock] = []

    _build_authorize_section(blocks, workspace_record.team_id, user_id, context)
    _build_configuration_section(blocks, workspace_record, is_admin=is_admin)

    if not is_manager:
        blocks.append(block_context(":lock: This area of SyncBot is limited to Workspace managers."))
    else:
        # ── Workspace Groups ──────────────────────────────────────
        blocks.append(header("Workspace Groups"))
        blocks.append(block_context("_Groups of Workspaces that can Create Sync and Join Sync._"))
        blocks.append(
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=":raised_hands: Create Group",
                        action=actions.CONFIG_CREATE_GROUP,
                    ),
                    orm.ButtonElement(
                        label=":punch: Join Group",
                        action=actions.CONFIG_JOIN_GROUP,
                    ),
                ]
            )
        )

        my_groups = _get_groups_for_workspace(workspace_record.id)

        pending_invites = DbManager.find_records(
            WorkspaceGroupMember,
            [
                WorkspaceGroupMember.workspace_id == workspace_record.id,
                WorkspaceGroupMember.status == "pending",
                WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )

        if not my_groups and not pending_invites:
            blocks.append(
                block_context(
                    "You are not in any Workspace Groups yet. Create or join a Group before you can Create Sync or Join Sync with other Workspaces."
                )
            )
        else:
            for group, my_membership in my_groups:
                _build_group_section(blocks, group, my_membership, workspace_record, context)

        for invite in pending_invites:
            _build_pending_invite_section(blocks, invite, context)

        # ── External Connections (federation) ─────────────────────
        if helpers.federation_enabled() and helpers.is_primary_workspace(team_id) and is_admin:
            _build_federation_section(blocks, workspace_record)

    current_hash = _home_tab_content_hash(
        workspace_record,
        user_id,
        is_manager=is_manager,
        is_admin=is_admin,
        extra_manager_ids=extra_manager_ids,
    )
    block_dicts = orm.BlockView(blocks=blocks).as_form_field()
    if return_blocks:
        return block_dicts
    client.views_publish(user_id=user_id, view={"type": "home", "blocks": block_dicts})
    # Update cache so next manual Refresh skips full rebuild when data unchanged
    helpers.refresh_after_full(
        home_tab_hash_key(team_id, user_id),
        f"home_tab_blocks:{team_id}:{user_id}",
        f"refresh_at:home:{team_id}:{user_id}",
        current_hash,
        block_dicts,
    )
    return None


def _build_pending_invite_section(
    blocks: list,
    invite: WorkspaceGroupMember,
    context: dict | None = None,
) -> None:
    """Append blocks for an incoming group invite the workspace hasn't responded to yet."""
    group = DbManager.get_record(WorkspaceGroup, id=invite.group_id)
    if not group:
        return

    inviting_members = DbManager.find_records(
        WorkspaceGroupMember,
        [
            WorkspaceGroupMember.group_id == group.id,
            WorkspaceGroupMember.status == "active",
            WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    inviter_workspace_names = []
    for member in inviting_members:
        if member.workspace_id:
            ws = helpers.get_workspace_by_id(member.workspace_id, context=context)
            inviter_workspace_names.append(
                helpers.resolve_workspace_name(ws) if ws else f"Workspace {member.workspace_id}"
            )
    workspace_label = ", ".join(inviter_workspace_names) if inviter_workspace_names else "Another Workspace"

    inviter_label = workspace_label
    if getattr(invite, "invited_by_slack_user_id", None) and getattr(invite, "invited_by_workspace_id", None):
        inviter_ws = helpers.get_workspace_by_id(invite.invited_by_workspace_id, context=context)
        if inviter_ws and inviter_ws.bot_token:
            try:
                ws_client = WebClient(token=helpers.decrypt_bot_token(inviter_ws.bot_token))
                admin_name, _ = helpers.get_user_info(ws_client, invite.invited_by_slack_user_id)
                if admin_name:
                    inviter_label = f"{admin_name} from {workspace_label}"
            except Exception as exc:
                # Keep the workspace-level fallback label if we cannot resolve the
                # inviter's display name from Slack.
                _logger.debug(
                    "pending_invite_inviter_name_lookup_failed",
                    extra={"invite_id": invite.id, "workspace_id": invite.invited_by_workspace_id, "error": str(exc)},
                )

    blocks.append(divider())
    blocks.append(header(f"{group.name}"))
    blocks.append(section(f":punch: *{inviter_label}* has invited your Workspace to join this Group."))
    blocks.append(
        orm.ActionsBlock(
            elements=[
                orm.ButtonElement(
                    label=":white_check_mark: Accept",
                    action=f"{actions.CONFIG_ACCEPT_GROUP_REQUEST}_{invite.id}",
                    value=str(invite.id),
                    style="primary",
                ),
                orm.ButtonElement(
                    label=":x: Decline",
                    action=f"{actions.CONFIG_DECLINE_GROUP_REQUEST}_{invite.id}",
                    value=str(invite.id),
                    style="danger",
                ),
            ]
        )
    )


def _build_group_section(
    blocks: list,
    group: WorkspaceGroup,
    my_membership: WorkspaceGroupMember,
    workspace_record: Workspace,
    context: dict | None = None,
) -> None:
    """Append blocks for a single workspace group."""
    blocks.append(divider())

    # Belt-and-suspenders behind migration 003's backfill: repairs a legacy
    # group that has no owner at all. Strictly guarded and idempotent, because
    # this is a write on the Home-tab read path. See helpers.ensure_group_has_owner.
    helpers.ensure_group_has_owner(group.id)

    all_members = _get_group_members(group.id)
    other_members = [member for member in all_members if member.workspace_id != workspace_record.id]

    is_owner = helpers.is_workspace_owner(group.id, workspace_record.id)
    owner_count = len(helpers.get_active_owners(group.id))

    blocks.append(header(f"{group.name}"))

    # Action buttons for this group
    group_actions: list[orm.ButtonElement] = [
        orm.ButtonElement(
            label=":incoming_envelope: Invite Workspace",
            action=actions.CONFIG_INVITE_WORKSPACE,
            value=str(group.id),
        ),
        orm.ButtonElement(
            label=":outbox_tray: Create Sync",
            action=actions.CONFIG_CREATE_SYNC,
            value=str(group.id),
        ),
        orm.ButtonElement(
            label=":busts_in_silhouette: User Mapping",
            action=actions.CONFIG_MANAGE_USER_MAPPING,
            value=str(group.id),
        ),
    ]
    group_actions.append(
        orm.ButtonElement(
            label=":wave: Leave Group",
            action=f"{actions.CONFIG_LEAVE_GROUP}_{group.id}",
            style="danger",
            value=str(group.id),
        ),
    )
    # Disband is only offered when it can actually succeed, so the destructive
    # button never appears to a workspace that would just be rejected.
    if is_owner and helpers.can_disband(group.id, workspace_record.id)[0]:
        group_actions.append(
            orm.ButtonElement(
                label=":wastebasket: Disband Group",
                action=f"{actions.CONFIG_DISBAND_GROUP}_{group.id}",
                style="danger",
                value=str(group.id),
            ),
        )
    blocks.append(orm.ActionsBlock(elements=group_actions))

    syncs_for_group = DbManager.find_records(Sync, [Sync.group_id == group.id])
    sync_ids = [s.id for s in syncs_for_group]
    _, ch_by_ws, mapped_by_ws = _prefetch_group_channel_and_mapping_counts(group.id, sync_ids)

    for member in all_members:
        if member.workspace_id:
            member_ws = helpers.get_workspace_by_id(member.workspace_id, context=context)
            name = helpers.resolve_workspace_name(member_ws) if member_ws else f"Workspace {member.workspace_id}"
            if member.role == "owner":
                name += " _(Group Owner)_"
        elif member.federated_workspace_id:
            fed_ws = DbManager.get_record(FederatedWorkspace, id=member.federated_workspace_id)
            name = f":globe_with_meridians: {fed_ws.name}" if fed_ws and fed_ws.name else "External"
        else:
            name = "Unknown"

        joined_str = f"{member.joined_at:%B %d, %Y}" if member.joined_at else "Unknown"

        ws_id = member.workspace_id
        channel_count = ch_by_ws.get(ws_id, 0) if ws_id else 0
        mapped_count = mapped_by_ws.get(ws_id, 0) if ws_id else 0

        stats = f"Member Since: `{joined_str}`\nSynced Channels: `{channel_count}`\nMapped Users: `{mapped_count}` "
        text = f"*{name}*\n{stats}"
        if member.workspace_id and member_ws:
            ws_info = _get_workspace_info(member_ws)
            icon_url = ws_info.get("icon_url")
            if icon_url:
                blocks.append(
                    orm.SectionBlock(
                        label=text,
                        element=orm.ImageAccessoryElement(
                            image_url=icon_url,
                            alt_text=name.split(" ")[0] if name else "Workspace",
                        ),
                    )
                )
            else:
                blocks.append(block_context(text))
        else:
            blocks.append(block_context(text))

        # Owners may promote any active local member. Demotion is self-only, and
        # only while another owner remains to keep the group from losing its
        # last owner.
        role_actions: list[orm.ButtonElement] = []
        if is_owner and member.workspace_id and member.role != "owner":
            role_actions.append(
                orm.ButtonElement(
                    label=":key: Promote to Owner",
                    action=f"{actions.CONFIG_PROMOTE_TO_OWNER}_{member.id}",
                    value=str(member.id),
                )
            )
        if member.workspace_id == workspace_record.id and member.role == "owner" and owner_count > 1:
            role_actions.append(
                orm.ButtonElement(
                    label=":information_source: Give Up Ownership",
                    action=f"{actions.CONFIG_DEMOTE_SELF}_{member.id}",
                    value=str(member.id),
                )
            )
        if role_actions:
            blocks.append(orm.ActionsBlock(elements=role_actions))

    pending_members = DbManager.find_records(
        WorkspaceGroupMember,
        [
            WorkspaceGroupMember.group_id == group.id,
            WorkspaceGroupMember.status == "pending",
            WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    for pending_member in pending_members:
        pending_ws = None
        if pending_member.workspace_id:
            pending_ws = helpers.get_workspace_by_id(pending_member.workspace_id, context=context)
            pname = (
                helpers.resolve_workspace_name(pending_ws) if pending_ws else f"Workspace {pending_member.workspace_id}"
            )
        else:
            pname = "Unknown"
        stats_pending = "Member Since: `Pending Invite`"
        text_pending = f"*{pname}*\n{stats_pending}"
        if pending_member.workspace_id and pending_ws:
            ws_info = _get_workspace_info(pending_ws)
            icon_url = ws_info.get("icon_url")
            if icon_url:
                blocks.append(
                    orm.SectionBlock(
                        label=text_pending,
                        element=orm.ImageAccessoryElement(
                            image_url=icon_url,
                            alt_text=pname.split(" ")[0] if pname else "Workspace",
                        ),
                    )
                )
            else:
                blocks.append(block_context(text_pending))
        else:
            blocks.append(block_context(text_pending))
        blocks.append(
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=":x: Cancel Invite",
                        action=f"{actions.CONFIG_CANCEL_GROUP_REQUEST}_{pending_member.id}",
                        value=str(pending_member.id),
                        style="danger",
                    ),
                ]
            )
        )

    _build_inline_channel_sync(blocks, group, workspace_record, other_members, context)


def _build_federation_section(
    blocks: list,
    workspace_record: Workspace,
) -> None:
    """Append the federation section to the home tab."""
    blocks.append(divider())
    blocks.append(block_context("\u200b"))
    blocks.append(section("*External Connections*"))
    blocks.append(block_context("Connect with Workspaces on other SyncBot deployments."))
    blocks.append(
        orm.ActionsBlock(
            elements=[
                orm.ButtonElement(
                    label=":globe_with_meridians: Generate Connection Code",
                    action=actions.CONFIG_GENERATE_FEDERATION_CODE,
                ),
                orm.ButtonElement(
                    label=":link: Enter Connection Code",
                    action=actions.CONFIG_ENTER_FEDERATION_CODE,
                ),
                orm.ButtonElement(
                    label=":package: Data Migration",
                    action=actions.CONFIG_DATA_MIGRATION,
                ),
            ]
        )
    )

    fed_members = DbManager.find_records(
        WorkspaceGroupMember,
        [
            WorkspaceGroupMember.federated_workspace_id.isnot(None),
            WorkspaceGroupMember.deleted_at.is_(None),
            WorkspaceGroupMember.status == "active",
        ],
    )

    shown_fed: set[int] = set()
    for fed_member in fed_members:
        if not fed_member.federated_workspace_id or fed_member.federated_workspace_id in shown_fed:
            continue
        my_groups = _get_groups_for_workspace(workspace_record.id)
        my_group_ids = {g.id for g, _ in my_groups}
        if fed_member.group_id not in my_group_ids:
            continue

        shown_fed.add(fed_member.federated_workspace_id)
        fed_ws = DbManager.get_record(FederatedWorkspace, id=fed_member.federated_workspace_id)
        if not fed_ws:
            continue

        fed_ws_name = fed_ws.name or f"Connection {fed_ws.instance_id[:8]}"
        status_icon = ":white_check_mark:" if fed_ws.status == "active" else ":warning:"

        blocks.append(block_context("\u200b"))
        label_text = f"{status_icon} *{fed_ws_name}*"
        label_text += f"\n:globe_with_meridians: {fed_ws.webhook_url}"
        blocks.append(section(label_text))

        blocks.append(
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=":octagonal_sign: Remove Connection",
                        action=f"{actions.CONFIG_REMOVE_FEDERATION_CONNECTION}_{fed_member.id}",
                        style="danger",
                        value=str(fed_member.id),
                    ),
                ]
            )
        )
