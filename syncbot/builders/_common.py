"""Shared helpers for builder modules."""

import logging

from slack_sdk.web import WebClient

import helpers
from db import DbManager
from db.schemas import Workspace, WorkspaceGroup, WorkspaceGroupMember
from helpers import get_user_id_from_body, is_workspace_manager, safe_get

_logger = logging.getLogger(__name__)


def _get_user_id(body: dict) -> str | None:
    """Extract the acting user ID from any Slack request body."""
    return safe_get(body, "event", "user") or safe_get(body, "user", "id") or safe_get(body, "user_id")


def _get_team_id(body: dict) -> str | None:
    """Extract the team ID from any Slack request body."""
    return (
        safe_get(body, "view", "team_id")
        or safe_get(body, "team_id")
        or safe_get(body, "team", "id")
        or safe_get(body, "event", "view", "team_id")
    )


def _deny_unauthorized(body: dict, client: WebClient, logger) -> bool:
    """Check authorization and send an ephemeral denial if the user is not an admin.

    Returns *True* if the user was denied (caller should return early).
    """
    user_id = get_user_id_from_body(body)
    if not user_id:
        logger.warning("authorization_denied: could not determine user_id from request body")
        return True

    team_id = _get_team_id(body)
    if is_workspace_manager(client, user_id, team_id):
        return False

    channel_id = safe_get(body, "channel_id") or safe_get(body, "channel", "id")
    _logger.warning(
        "authorization_denied",
        extra={"user_id": user_id, "action": "config"},
    )

    if channel_id:
        try:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=":lock: Only workspace managers can configure SyncBot.",
            )
        except Exception:
            _logger.debug("Could not send ephemeral denial — user may have invoked from a modal")

    return True


def _get_groups_for_workspace(workspace_id: int) -> list[tuple[WorkspaceGroup, WorkspaceGroupMember]]:
    """Return all active groups the workspace belongs to, with membership info."""
    members = DbManager.find_records(
        WorkspaceGroupMember,
        [
            WorkspaceGroupMember.workspace_id == workspace_id,
            WorkspaceGroupMember.status == "active",
            WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    results: list[tuple[WorkspaceGroup, WorkspaceGroupMember]] = []
    for member in members:
        groups = DbManager.find_records(
            WorkspaceGroup,
            [WorkspaceGroup.id == member.group_id, WorkspaceGroup.status == "active"],
        )
        if groups:
            results.append((groups[0], member))
    return results


def _get_group_members(group_id: int) -> list[WorkspaceGroupMember]:
    """Return all active members of a group."""
    return helpers.get_group_members(group_id)


def _get_workspace_info(workspace: Workspace) -> dict:
    """Fetch workspace icon URL and domain from the Slack API (cached 24h)."""
    result: dict[str, str | None] = {"icon_url": None, "domain": None, "raw_domain": None}
    if not workspace or not workspace.bot_token:
        return result

    cache_key = f"ws_info:{workspace.id}"
    cached = helpers._cache_get(cache_key)
    if cached:
        return cached

    try:
        ws_client = WebClient(token=helpers.decrypt_bot_token(workspace.bot_token))
        info = ws_client.team_info()
        result["icon_url"] = helpers.safe_get(info, "team", "icon", "image_88") or helpers.safe_get(
            info, "team", "icon", "image_68"
        )
        domain = helpers.safe_get(info, "team", "domain")
        if domain:
            result["domain"] = f"<https://{domain}.slack.com|{domain}.slack.com>"
            result["raw_domain"] = domain
        helpers._cache_set(cache_key, result, ttl=86400)
    except Exception as exc:
        _logger.debug(f"_get_workspace_meta: team_info call failed: {exc}")
    return result


def _format_channel_ref(
    channel_id: str,
    workspace: Workspace,
    is_local: bool = True,
    *,
    include_workspace_in_link: bool = True,
) -> str:
    """Format a channel reference for display in Block Kit mrkdwn."""
    if is_local:
        return f"<#{channel_id}>"

    ws_name = workspace.workspace_name if workspace and workspace.workspace_name else "Partner"

    if not workspace or not workspace.bot_token:
        return f"#{channel_id} ({ws_name})" if include_workspace_in_link else f"#{channel_id}"

    cache_key = f"chan_ref:{channel_id}:{include_workspace_in_link}"
    cached = helpers._cache_get(cache_key)
    if cached:
        return cached

    ch_name = channel_id
    try:
        ws_client = WebClient(token=helpers.decrypt_bot_token(workspace.bot_token))
        info = ws_client.conversations_info(channel=channel_id)
        ch_name = helpers.safe_get(info, "channel", "name") or channel_id
    except Exception as e:
        _logger.warning(
            "format_channel_ref_failed",
            extra={"channel_id": channel_id, "workspace": ws_name, "error": str(e)},
        )

    ws_info = _get_workspace_info(workspace)
    domain = ws_info.get("raw_domain")
    link_text = f"#{ch_name} ({ws_name})" if include_workspace_in_link else f"#{ch_name}"
    if domain:
        deep_link = f"https://{domain}.slack.com/archives/{channel_id}"
        result = f"<{deep_link}|{link_text}>"
    else:
        result = f"`[{link_text}]`"
    if ch_name != channel_id:
        helpers._cache_set(cache_key, result, ttl=3600)
    return result
