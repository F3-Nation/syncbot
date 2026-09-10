"""Admin DM notifications and channel notifications."""

import logging
from datetime import UTC, datetime

from slack_sdk import WebClient
from sqlalchemy.exc import ProgrammingError

from db import DbManager, schemas
from helpers._cache import _cache_get, _cache_set
from helpers.core import safe_get
from helpers.encryption import decrypt_bot_token

_logger = logging.getLogger(__name__)


def get_admin_ids(
    client: WebClient,
    *,
    team_id: str | None = None,
    context: dict | None = None,
) -> list[str]:
    """Return a list of admin/owner user IDs for the workspace behind *client*.

    If *context* and *team_id* are provided, uses request-scoped cache to avoid
    repeated users.list for the same workspace within one request. A 60s process
    cache keyed by *team_id* covers subsequent requests on a warm container.
    """
    if context is not None and team_id:
        cache = context.setdefault("_admin_ids", {})
        if team_id in cache:
            return cache[team_id]

    cache_key = f"admin_ids:{team_id}" if team_id else None
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            if context is not None and team_id:
                context.setdefault("_admin_ids", {})[team_id] = cached
            return cached

    from helpers.user_map import _users_list_page

    cursor = ""
    admin_ids: list[str] = []

    while True:
        try:
            res = _users_list_page(client, cursor=cursor)
        except Exception as e:
            _logger.warning(f"get_admin_ids: failed to list users: {e}")
            break

        members = safe_get(res, "members") or []
        for member in members:
            if member.get("is_bot") or member.get("id") == "USLACKBOT":
                continue
            if member.get("deleted"):
                continue
            if member.get("is_admin") or member.get("is_owner"):
                admin_ids.append(member["id"])

        next_cursor = safe_get(res, "response_metadata", "next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor

    if context is not None and team_id:
        context.setdefault("_admin_ids", {})[team_id] = admin_ids
    if cache_key:
        _cache_set(cache_key, admin_ids)
    return admin_ids


def get_manager_ids(
    client: WebClient,
    *,
    team_id: str | None = None,
    context: dict | None = None,
) -> list[str]:
    """Return Slack admin/owner IDs plus extra managers for *team_id*."""
    from helpers.workspace_settings import extra_manager_user_ids

    admin_ids = get_admin_ids(client, team_id=team_id, context=context)
    if not team_id:
        return admin_ids
    merged = sorted(set(admin_ids) | set(extra_manager_user_ids(team_id)))
    return merged


def notify_admins_dm(
    client: WebClient,
    message: str,
    exclude_user_ids: set[str] | None = None,
    blocks: list[dict] | None = None,
    *,
    include_managers: bool = True,
    team_id: str | None = None,
    context: dict | None = None,
) -> int:
    """Send a DM to workspace admins/owners and extra managers.

    Extra managers are included because these DMs are operational (sync failures,
    group changes). Settings / Backup / Reset do not use this helper.
    """
    notified = 0
    kwargs: dict = {"text": message}
    if blocks:
        kwargs["blocks"] = blocks
    user_ids = (
        get_manager_ids(client, team_id=team_id, context=context)
        if include_managers
        else get_admin_ids(client, team_id=team_id, context=context)
    )
    for user_id in user_ids:
        if exclude_user_ids and user_id in exclude_user_ids:
            continue
        try:
            dm = client.conversations_open(users=[user_id])
            channel_id = safe_get(dm, "channel", "id")
            if channel_id:
                client.chat_postMessage(channel=channel_id, **kwargs)
                notified += 1
        except Exception as e:
            _logger.warning(f"notify_admins_dm: failed to DM user {user_id}: {e}")

    return notified


def notify_admins_dm_blocks(
    client: WebClient,
    text: str,
    blocks: list[dict],
    *,
    team_id: str | None = None,
    context: dict | None = None,
) -> list[dict]:
    """Send a Block Kit DM to workspace admins/owners and extra managers."""
    sent: list[dict] = []
    for user_id in get_manager_ids(client, team_id=team_id, context=context):
        try:
            dm = client.conversations_open(users=[user_id])
            channel_id = safe_get(dm, "channel", "id")
            if channel_id:
                res = client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
                msg_ts = safe_get(res, "ts")
                if msg_ts:
                    sent.append({"channel": channel_id, "ts": msg_ts})
        except Exception as e:
            _logger.warning(f"notify_admins_dm_blocks: failed to DM user {user_id}: {e}")

    return sent


def save_dm_messages_to_group_member(member_id: int, dm_entries: list[dict]) -> None:
    """Persist DM channel/ts metadata on a group member record for later updates."""
    import json as _json

    if not dm_entries:
        return
    existing = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not existing:
        return
    try:
        prev = _json.loads(existing.dm_messages) if existing.dm_messages else []
    except (ValueError, TypeError):
        prev = []
    prev.extend(dm_entries)
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member_id],
        {schemas.WorkspaceGroupMember.dm_messages: _json.dumps(prev)},
    )


def notify_synced_channels(client: WebClient, channel_ids: list[str], message: str) -> int:
    """Post a message to a list of channels.  Best-effort."""
    notified = 0
    for channel_id in channel_ids:
        try:
            client.chat_postMessage(channel=channel_id, text=message)
            notified += 1
        except Exception as e:
            _logger.warning(f"notify_synced_channels: failed to post to {channel_id}: {e}")
    return notified


def purge_stale_soft_deletes() -> int:
    """Permanently delete workspaces that have been soft-deleted beyond the retention period.

    Returns 0 without raising if the schema is missing (e.g. fresh DB before Alembic bootstrap).
    """
    from helpers.workspace import get_workspace_by_id

    cache_key = "purge_check"
    if _cache_get(cache_key):
        return 0
    _cache_set(cache_key, True, ttl=86400)

    from helpers.settings import soft_delete_retention_days

    retention_days = soft_delete_retention_days()
    cutoff = datetime.now(UTC) - __import__("datetime").timedelta(days=retention_days)

    try:
        stale_workspaces = DbManager.find_records(
            schemas.Workspace,
            [
                schemas.Workspace.deleted_at.isnot(None),
                schemas.Workspace.deleted_at < cutoff,
            ],
        )
    except ProgrammingError as e:
        _logger.debug("purge_stale_soft_deletes: schema not ready (%s), skipping", e.orig if hasattr(e, "orig") else e)
        return 0

    if not stale_workspaces:
        return 0

    purged = 0
    for ws in stale_workspaces:
        ws_name = ws.workspace_name or ws.team_id or f"Workspace {ws.id}"

        group_memberships = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [schemas.WorkspaceGroupMember.workspace_id == ws.id],
        )

        notified_ws: set[int] = set()
        for membership in group_memberships:
            other_members = DbManager.find_records(
                schemas.WorkspaceGroupMember,
                [
                    schemas.WorkspaceGroupMember.group_id == membership.group_id,
                    schemas.WorkspaceGroupMember.workspace_id != ws.id,
                    schemas.WorkspaceGroupMember.status == "active",
                    schemas.WorkspaceGroupMember.deleted_at.is_(None),
                ],
            )
            for member in other_members:
                if not member.workspace_id or member.workspace_id in notified_ws:
                    continue
                member_ws = get_workspace_by_id(member.workspace_id)
                if not member_ws or not member_ws.bot_token or member_ws.deleted_at is not None:
                    continue
                notified_ws.add(member.workspace_id)
                try:
                    member_client = WebClient(token=decrypt_bot_token(member_ws.bot_token))
                    notify_admins_dm(
                        member_client,
                        f":wastebasket: *{ws_name}* has been permanently removed "
                        f"after {retention_days} days of inactivity.",
                        team_id=member_ws.team_id,
                    )
                except Exception as e:
                    _logger.warning(f"purge: failed to notify member {member.workspace_id}: {e}")

        # purge_workspace, not a bare Workspace delete: seven foreign keys across
        # six tables still reference this row, so a parent-first delete fails on
        # MySQL with error 1451 and the retention purge silently stops working.
        from helpers.sync_cleanup import purge_workspace

        try:
            purge_workspace(ws.id)
        except Exception as e:
            _logger.error(
                "purge_workspace_failed",
                extra={"workspace_id": ws.id, "error": str(e)},
            )
            continue

        purged += 1

    if purged:
        _logger.info("purge_stale_soft_deletes_complete", extra={"purged": purged})

    return purged
