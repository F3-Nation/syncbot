"""Core utility functions used throughout SyncBot."""

import logging
import os
from typing import Any

from slack_sdk.errors import SlackApiError

import constants
from slack import actions

_logger = logging.getLogger(__name__)


_ERROR_DM_VALUE_MAX = 240


def format_error_dm(summary: str, details: dict[str, Any] | None = None) -> str:
    """Human summary plus a fenced details block the user can copy."""
    if not details:
        return summary
    lines: list[str] = []
    for key, value in details.items():
        if value is None or value == "":
            continue
        text = str(value).replace("```", "`")
        if len(text) > _ERROR_DM_VALUE_MAX:
            text = text[: _ERROR_DM_VALUE_MAX - 1] + "…"
        lines.append(f"{key}: {text}")
    if not lines:
        return summary
    return f"{summary}\n```\n" + "\n".join(lines) + "\n```"


def synced_from_line_username(display_name: str | None, workspace_name: str | None = None) -> str:
    """Display name used on the Slack from line for a synced message.

    Mapped authors pass ``workspace_name=None``. Unmapped authors include
    ``(source workspace)`` after the name, matching ``chat.postMessage``.
    """
    name = (display_name or "").strip() or "Someone"
    if workspace_name:
        return f"{name} ({workspace_name})"
    return name


def code_ticked_display_name(display_name: str | None, workspace_name: str | None = None) -> str:
    """Name in code ticks, optionally with (Workspace). From-line, unmapped people, source #channel."""
    return f"`{synced_from_line_username(display_name, workspace_name)}`"


def format_file_share_notice(display_name: str | None, workspace_name: str | None = None) -> str:
    """Bot notice for who shared a file. Never tags; from-line name in code ticks."""
    return f"{code_ticked_display_name(display_name, workspace_name)} shared a file"


def safe_get(data: Any, *keys: Any) -> Any:
    """Safely traverse nested dicts/lists. Returns None on missing keys."""
    if not data:
        return None
    try:
        result = data
        for k in keys:
            if isinstance(k, int) and isinstance(result, list) or result.get(k):
                result = result[k]
            else:
                return None
        return result
    except (KeyError, AttributeError, IndexError):
        return None


def get_user_id_from_body(body: dict) -> str | None:
    """Extract the acting user's ID from any Slack request payload."""
    return safe_get(body, "user_id") or safe_get(body, "user", "id")


_REQUIRE_ADMIN_WARNED = False


def _warn_require_admin_leftover() -> None:
    global _REQUIRE_ADMIN_WARNED
    if _REQUIRE_ADMIN_WARNED:
        return
    raw = os.environ.get(constants.REQUIRE_ADMIN)
    if raw is None or raw.strip() == "":
        return
    _REQUIRE_ADMIN_WARNED = True
    _logger.warning(
        "%s is ignored; Slack admins and owners configure Settings, and managers come from Settings extra managers",
        constants.REQUIRE_ADMIN,
    )


def is_workspace_admin(client, user_id: str) -> bool:
    """Return *True* if the user is a Slack workspace admin or owner."""
    from .slack_api import _users_info

    _warn_require_admin_leftover()
    try:
        res = _users_info(client, user_id)
    except SlackApiError:
        _logger.warning(f"Could not verify admin status for user {user_id} — denying access")
        return False

    user = safe_get(res, "user") or {}
    return bool(user.get("is_admin") or user.get("is_owner"))


def is_workspace_manager(client, user_id: str, team_id: str | None) -> bool:
    """Return *True* if the user may manage groups, syncs, and channel configuration."""
    from .workspace_settings import extra_manager_user_ids

    _warn_require_admin_leftover()
    if is_workspace_admin(client, user_id):
        return True
    if not team_id or not user_id:
        return False
    return user_id in extra_manager_user_ids(team_id)


def is_primary_workspace(team_id: str | None) -> bool:
    """Return *True* when *team_id* matches ``PRIMARY_WORKSPACE``."""
    primary = (os.environ.get(constants.PRIMARY_WORKSPACE) or "").strip()
    return bool(primary and (team_id or "") == primary)


def is_backup_visible_for_workspace(team_id: str | None) -> bool:
    """Return True if full backup/restore UI and handlers are allowed for this workspace.

    Requires PRIMARY_WORKSPACE to be set and to match *team_id*.
    When PRIMARY_WORKSPACE is unset, backup/restore is hidden everywhere.
    """
    primary = (os.environ.get(constants.PRIMARY_WORKSPACE) or "").strip()
    if not primary:
        _logger.debug("backup/restore hidden: PRIMARY_WORKSPACE not set")
        return False
    visible = (team_id or "") == primary
    if not visible:
        _logger.debug(
            "backup/restore hidden: team_id %r does not match PRIMARY_WORKSPACE",
            team_id,
        )
    return visible


def is_db_reset_visible_for_workspace(team_id: str | None) -> bool:
    """Return True if the DB reset button/action is allowed for this workspace.

    Requires PRIMARY_WORKSPACE to match *team_id* and ENABLE_DB_RESET to be a truthy
    boolean string (``true``, ``1``, ``yes``). Reads env at call time.
    """
    primary = (os.environ.get(constants.PRIMARY_WORKSPACE) or "").strip()
    if not primary or (team_id or "") != primary:
        _logger.debug("DB reset button hidden: PRIMARY_WORKSPACE unset or team_id mismatch")
        return False
    enabled = (os.environ.get(constants.ENABLE_DB_RESET) or "").strip().lower()
    if enabled not in ("true", "1", "yes"):
        _logger.debug("DB reset button hidden: ENABLE_DB_RESET not true")
        return False
    return True


def is_settings_visible_for_workspace(team_id: str | None) -> bool:
    """Return True if the Settings button may appear for this workspace.

    Any installed workspace may open Settings; Slack admin/owner is enforced
    when the modal opens. Instance-wide fields inside the modal still require
    ``PRIMARY_WORKSPACE`` to match.
    """
    return bool((team_id or "").strip())


def format_admin_label(client, user_id: str, workspace) -> tuple[str, str]:
    """Return ``(display_name, full_label)`` for an admin."""
    from .slack_api import get_user_info
    from .workspace import resolve_workspace_name

    display_name, _ = get_user_info(client, user_id)
    display_name = display_name or "An Admin from another Workspace"
    ws_name = resolve_workspace_name(workspace) if workspace else None
    if ws_name:
        return display_name, f"{display_name} from {ws_name}"
    return display_name, display_name


_PREFIXED_ACTIONS = (
    actions.CONFIG_REMOVE_FEDERATION_CONNECTION,
    actions.CONFIG_LEAVE_GROUP,
    actions.CONFIG_ACCEPT_GROUP_REQUEST,
    actions.CONFIG_DECLINE_GROUP_REQUEST,
    actions.CONFIG_CANCEL_GROUP_REQUEST,
    actions.CONFIG_PROMOTE_TO_OWNER,
    actions.CONFIG_DEMOTE_SELF,
    actions.CONFIG_DISBAND_GROUP,
    actions.CONFIG_JOIN_SYNC,
    actions.CONFIG_EDIT_SYNC,
    actions.CONFIG_LEAVE_SYNC,
    actions.CONFIG_USER_MAPPING_EDIT,
    actions.CONFIG_RESUME_SYNC,
    actions.CONFIG_PAUSE_SYNC,
)
# ``create_sync`` is not prefixed so ``create_sync_select`` stays a picker.
# ``join_sync`` is prefixed (Home buttons are ``join_sync_{id}``), so the Join
# Sync picker must not start with ``join_sync_`` (see CONFIG_JOIN_SYNC_SELECT).


def get_request_type(body: dict) -> tuple[str, str]:
    """Classify an incoming Slack request into a ``(category, identifier)`` pair."""
    request_type = safe_get(body, "type")
    if request_type == "event_callback":
        return ("event_callback", safe_get(body, "event", "type"))
    elif request_type == "block_actions":
        block_action = safe_get(body, "actions", 0, "action_id")
        for prefix in _PREFIXED_ACTIONS:
            if block_action == prefix or block_action.startswith(prefix + "_"):
                block_action = prefix
                break
        return ("block_actions", block_action)
    elif request_type == "view_submission":
        return ("view_submission", safe_get(body, "view", "callback_id"))
    elif not request_type and "command" in body:
        return ("command", safe_get(body, "command"))
    else:
        return ("unknown", "unknown")
