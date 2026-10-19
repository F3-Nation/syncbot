"""Shared helpers for builder modules."""

from slack_sdk.web import WebClient

import helpers
from db import DbManager
from db.schemas import Workspace, WorkspaceGroup, WorkspaceGroupMember
from helpers import get_team_id_from_body, get_user_id_from_body, is_workspace_manager, safe_get
from logger import log_debug, log_warning


def _deny_unauthorized(body: dict, client: WebClient, logger) -> bool:
    """Check authorization and send an ephemeral denial if the user is not a manager.

    Returns *True* if the user was denied (caller should return early).
    """
    user_id = get_user_id_from_body(body)
    if not user_id:
        log_warning("authorization_denied", reason="could not determine user_id from request body")
        return True

    team_id = get_team_id_from_body(body)
    if is_workspace_manager(client, user_id, team_id):
        return False

    channel_id = safe_get(body, "channel_id") or safe_get(body, "channel", "id")
    log_warning("authorization_denied", user_id=user_id, action="config")

    if channel_id:
        try:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=":lock: Only workspace managers can configure SyncBot.",
            )
        except Exception:
            log_debug("could_not_send_ephemeral_denial_user_may_have_invoked_from_a")

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
    if not members:
        return []
    group_ids = [m.group_id for m in members]
    groups = DbManager.find_records(
        WorkspaceGroup,
        [WorkspaceGroup.id.in_(group_ids), WorkspaceGroup.status == "active"],
    )
    groups_by_id = {g.id: g for g in groups}
    results: list[tuple[WorkspaceGroup, WorkspaceGroupMember]] = []
    for member in members:
        group = groups_by_id.get(member.group_id)
        if group:
            results.append((group, member))
    return results


def _get_group_members(group_id: int) -> list[WorkspaceGroupMember]:
    """Return all active members of a group."""
    return helpers.get_group_members(group_id)


def _get_workspace_info(workspace: Workspace) -> dict:
    """Fetch workspace icon URL and domain from the Slack API (cached 24h)."""
    result: dict[str, str | None] = {"icon_url": None, "domain": None, "raw_domain": None}
    if not workspace or not helpers.get_bot_token(workspace):
        return result

    cache_key = f"ws_info:{workspace.id}"
    cached = helpers._cache_get(cache_key)
    if cached:
        return cached

    try:
        ws_client = WebClient(token=helpers.get_bot_token(workspace))
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
        log_debug("get_workspace_meta_team_info_call_failed", error=str(exc))
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

    ws_name = workspace.workspace_name if workspace and workspace.workspace_name else "Workspace"

    if not workspace or not helpers.get_bot_token(workspace):
        ch_name, _is_private = helpers.lookup_channel_meta(channel_id, workspace)
        label = f"#{str(ch_name or channel_id).removeprefix('#')}"
        if include_workspace_in_link:
            if ch_name != channel_id:
                return helpers.code_ticked_display_name(label, ws_name)
            return f"{label} ({ws_name})"
        return label

    cache_key = f"chan_ref:{channel_id}:{include_workspace_in_link}"
    cached = helpers._cache_get(cache_key)
    if cached:
        return cached

    ch_name = channel_id
    try:
        ws_client = WebClient(token=helpers.get_bot_token(workspace))
        info = ws_client.conversations_info(channel=channel_id)
        ch_name = helpers.safe_get(info, "channel", "name") or channel_id
    except Exception as e:
        log_warning("format_channel_ref_failed", channel_id=channel_id, workspace=ws_name, error=str(e))

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
