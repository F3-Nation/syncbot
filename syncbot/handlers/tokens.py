"""Token revocation handler."""

import logging
from datetime import UTC, datetime
from logging import Logger

from slack_sdk.errors import SlackApiError
from slack_sdk.web import WebClient

import builders
import helpers
from db import DbManager, schemas

_logger = logging.getLogger(__name__)


def handle_tokens_revoked(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle ``tokens_revoked`` using Bolt's installation store.

    Slack sends ``event.tokens.oauth`` (user IDs whose *user* tokens died) and
    ``event.tokens.bot``. A personal **Authorize SyncBot** revoke is
    ``InstallationStore.delete_installation`` for that user_id, then republish
    Home. Slack sometimes also fills ``tokens.bot`` on that event; only run the
    uninstall path (``delete_all`` + workspace pause) when the stored bot token
    fails ``auth.test``. Bolt's ``enable_token_revocation_listeners()`` would
    delete the bot whenever ``tokens.bot`` is present, which blanks Home.
    """
    team_id = helpers.safe_get(body, "team_id")
    if not team_id:
        _logger.warning("handle_tokens_revoked: missing team_id")
        return

    event = helpers.safe_get(body, "event") or {}
    tokens = event.get("tokens") if isinstance(event, dict) else None
    if not isinstance(tokens, dict):
        tokens = {}
    oauth_users = [str(uid) for uid in (tokens.get("oauth") or []) if uid]
    bot_users = [str(uid) for uid in (tokens.get("bot") or []) if uid]

    if bot_users:
        workspace_record = DbManager.get_record(schemas.Workspace, team_id)
        if workspace_record and _workspace_bot_is_alive(workspace_record):
            _logger.info(
                "tokens_revoked_ignored_bot_array",
                extra={"team_id": team_id, "oauth_users": oauth_users, "bot_users": bot_users},
            )
        else:
            _uninstall_workspace(team_id)
            return

    for user_id in oauth_users:
        helpers.clear_user_authorization(team_id, user_id)
        _republish_home_after_user_revoke(team_id, user_id, logger)


def handle_app_uninstalled(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle ``app_uninstalled``: Bolt ``delete_all`` plus SyncBot workspace pause.

    This is the event Bolt's built-in listener uses for a workspace uninstall.
    We call the same store method, then pause groups and channel syncs.
    """
    team_id = helpers.safe_get(body, "team_id")
    if not team_id:
        _logger.warning("handle_app_uninstalled: missing team_id")
        return
    _uninstall_workspace(team_id)


def _uninstall_workspace(team_id: str) -> None:
    """Wipe Bolt install rows for this team, then pause SyncBot workspace data."""
    helpers.clear_workspace_installations(team_id)
    _soft_delete_uninstalled_workspace(team_id)


def _workspace_bot_is_alive(workspace_record) -> bool:
    """True when this workspace's stored bot token still authenticates.

    Slack sometimes includes ``tokens.bot`` on a personal user-token revoke.
    Uninstalling in that case pauses every sync. If ``auth.test`` still works,
    treat the event as user-token-only.
    """
    if not workspace_record or not workspace_record.bot_token:
        return False
    try:
        WebClient(token=helpers.decrypt_bot_token(workspace_record.bot_token)).auth_test()
        return True
    except Exception:
        return False


def _republish_home_after_user_revoke(team_id: str, user_id: str, logger: Logger) -> None:
    """Rebuild Home so Authorize SyncBot reappears without waiting for Refresh."""
    workspace_record = DbManager.get_record(schemas.Workspace, team_id)
    if not workspace_record or not workspace_record.bot_token or workspace_record.deleted_at:
        return
    try:
        client = WebClient(token=helpers.decrypt_bot_token(workspace_record.bot_token))
        builders.build_home_tab(
            {"team": {"id": team_id}, "user": {"id": user_id}},
            client,
            logger,
            {},
            user_id=user_id,
        )
    except SlackApiError as exc:
        error = None
        try:
            error = exc.response["error"]
        except Exception:
            error = None
        if error == "account_inactive":
            _logger.info(
                "tokens_revoked_home_skipped_inactive_user",
                extra={"team_id": team_id, "user_id": user_id},
            )
            return
        _logger.exception(
            "tokens_revoked_home_refresh_failed",
            extra={"team_id": team_id, "user_id": user_id},
        )
    except Exception:
        _logger.exception(
            "tokens_revoked_home_refresh_failed",
            extra={"team_id": team_id, "user_id": user_id},
        )


def _soft_delete_uninstalled_workspace(team_id: str) -> None:
    """Soft-delete the workspace after Slack uninstalled the app."""
    workspace_record = DbManager.get_record(schemas.Workspace, team_id)
    if not workspace_record:
        _logger.warning("handle_tokens_revoked: unknown workspace", extra={"team_id": team_id})
        return
    if workspace_record.deleted_at is not None:
        return

    now = datetime.now(UTC)
    ws_name = helpers.resolve_workspace_name(workspace_record)
    retention_days = helpers.soft_delete_retention_days()

    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.id == workspace_record.id],
        {schemas.Workspace.deleted_at: now},
    )

    active_memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id == workspace_record.id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )

    for membership in active_memberships:
        DbManager.update_records(
            schemas.WorkspaceGroupMember,
            [schemas.WorkspaceGroupMember.id == membership.id],
            {schemas.WorkspaceGroupMember.deleted_at: now},
        )

    my_channels = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.workspace_id == workspace_record.id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    for sync_channel in my_channels:
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == sync_channel.id],
            {schemas.SyncChannel.deleted_at: now, schemas.SyncChannel.status: "paused"},
        )

    notified_ws: set[int] = set()
    for membership in active_memberships:
        group_members = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.group_id == membership.group_id,
                schemas.WorkspaceGroupMember.workspace_id != workspace_record.id,
                schemas.WorkspaceGroupMember.status == "active",
                schemas.WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )
        for member in group_members:
            if not member.workspace_id or member.workspace_id in notified_ws:
                continue
            member_ws = helpers.get_workspace_by_id(member.workspace_id)
            if not member_ws or not member_ws.bot_token or member_ws.deleted_at:
                continue
            notified_ws.add(member.workspace_id)

            try:
                member_client = WebClient(token=helpers.decrypt_bot_token(member_ws.bot_token))

                helpers.notify_admins_dm(
                    member_client,
                    f":double_vertical_bar: *{ws_name}* has uninstalled SyncBot. "
                    f"Syncing has been paused. If they reinstall within {retention_days} days, "
                    "Syncing will resume automatically.",
                    team_id=member_ws.team_id,
                )

                member_channel_ids = []
                for sync_channel in my_channels:
                    sibling_channels = DbManager.find_records(
                        schemas.SyncChannel,
                        [
                            schemas.SyncChannel.sync_id == sync_channel.sync_id,
                            schemas.SyncChannel.workspace_id == member.workspace_id,
                            schemas.SyncChannel.deleted_at.is_(None),
                        ],
                    )
                    for sibling in sibling_channels:
                        member_channel_ids.append(sibling.channel_id)

                if member_channel_ids:
                    helpers.notify_synced_channels(
                        member_client,
                        member_channel_ids,
                        f":double_vertical_bar: Syncing with *{ws_name}* has been paused because they uninstalled the app.",
                    )
            except Exception as e:
                _logger.warning(f"handle_tokens_revoked: failed to notify member {member.workspace_id}: {e}")

    _logger.info(
        "workspace_soft_deleted",
        extra={
            "workspace_id": workspace_record.id,
            "team_id": team_id,
            "memberships_paused": len(active_memberships),
            "channels_paused": len(my_channels),
        },
    )
