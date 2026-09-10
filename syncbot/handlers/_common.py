"""Shared handler utilities and types."""

import contextlib
import logging
from typing import Any

import helpers
from db import schemas

_logger = logging.getLogger(__name__)

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict


class EventContext(TypedDict):
    """Strongly-typed dict returned by ``_parse_event_fields``."""

    team_id: str | None
    channel_id: str | None
    user_id: str | None
    msg_text: str
    mentioned_users: list[dict[str, Any]]
    thread_ts: str | None
    ts: str | None
    event_subtype: str | None
    reply_broadcast: bool
    content_blocks: list[dict]


def _parse_private_metadata(body: dict) -> dict:
    """Extract and parse JSON ``private_metadata`` from a view submission."""
    import json as _json

    raw = helpers.safe_get(body, "view", "private_metadata") or "{}"
    try:
        return _json.loads(raw)
    except Exception as exc:
        _logger.debug(f"_parse_private_metadata: bad JSON: {exc}")
        return {}


def _close_modal_done(client, body: dict, message: str) -> None:
    """Replace an open modal with a terminal, close-only acknowledgement.

    Destructive confirmations put a red ``danger`` button in the modal body
    rather than using the modal's submit button, which Slack renders in the
    theme colour and cannot be styled red. A block action cannot close a modal
    (only a view submission can), so once the work is done we swap the view for
    a "you can close this" screen, mirroring the database-reset flow.
    """
    view_id = helpers.safe_get(body, "view", "id")
    if not view_id:
        return
    try:
        client.views_update(
            view_id=view_id,
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "Done"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message}}],
            },
        )
    except Exception as exc:
        _logger.warning("modal_close_failed", extra={"error": str(exc)})


def _dm_user(client, user_id: str | None, message: str) -> None:
    """Send *message* to *user_id* as a DM, ignoring failures.

    Used to report work-phase failures. By then the modal is closed, so a field
    error is no longer possible and silence would look like success.
    """
    if not user_id:
        return
    try:
        client.chat_postMessage(channel=user_id, text=message)
    except Exception as exc:
        _logger.warning("admin_dm_failed", extra={"user_id": user_id, "error": str(exc)})


def _ensure_membership_or_rollback(
    client,
    channel_id: str,
    *,
    team_id: str | None,
    acting_user_id: str | None,
    rollback,
    log_event: str,
    log_extra: dict | None = None,
    context: dict | None = None,
) -> bool:
    """Add SyncBot to *channel_id*, undoing the caller's rows if that fails.

    Callers write their ``Sync`` / ``SyncChannel`` rows first so the
    unconfigured-channel leave handlers see a configured channel, which is what
    makes a private-channel invite survive. The flip side is that a failed
    join or invite would leave a Channel listed on Home that SyncBot cannot
    read, so the rows are removed again and the admin is told why.

    Returns *True* when SyncBot is in the channel.
    """
    try:
        helpers.ensure_bot_in_conversation(
            client,
            channel_id,
            team_id=team_id,
            acting_user_id=acting_user_id,
            context=context,
        )
        return True
    except helpers.ConversationAccessError as exc:
        message = str(exc)
        _logger.warning(log_event, extra={**(log_extra or {}), "error": message})
        details: dict = {"event": log_event, "channel": channel_id}
        cause = exc.__cause__
        slack_code = ""
        resp = getattr(cause, "response", None) if cause is not None else None
        if resp is not None:
            with contextlib.suppress(Exception):
                slack_code = str(resp.get("error") or "")
        if slack_code:
            details["error"] = slack_code
    except Exception as exc:
        _logger.error(log_event, extra={**(log_extra or {}), "error": str(exc)})
        message = "SyncBot could not be added to that Channel. Please try again."
        details = {"error": str(exc), "event": log_event, "channel": channel_id}

    try:
        rollback()
    except Exception as rollback_exc:
        _logger.error(f"{log_event}_rollback_failed", extra={"channel_id": channel_id, "error": str(rollback_exc)})

    _dm_user(
        client,
        acting_user_id,
        helpers.format_error_dm(f":warning: {message}", details),
    )
    return False


def _extract_team_id(body: dict) -> str | None:
    """Return a workspace/team ID from common Slack payload locations."""
    return (
        helpers.safe_get(body, "view", "team_id")
        or helpers.safe_get(body, "team", "id")
        or helpers.safe_get(body, "team_id")
        or helpers.safe_get(body, "user", "team_id")
    )


def _get_authorized_workspace(
    body: dict, client, context: dict, action_name: str
) -> tuple[str, schemas.Workspace] | None:
    """Validate authorization and return ``(user_id, workspace_record)``.

    Returns *None* and logs a warning if the user is not authorized or
    the workspace cannot be resolved.
    """
    user_id = helpers.get_user_id_from_body(body)
    team_id = _extract_team_id(body)
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        _logger.warning("authorization_denied", extra={"user_id": user_id, "action": action_name})
        return None

    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return None

    return user_id, workspace_record


def _iter_view_state_actions(body: dict):
    """Yield ``(action_id, action_data)`` pairs from ``view.state.values``."""
    state_values = helpers.safe_get(body, "view", "state", "values") or {}
    for block_data in state_values.values():
        yield from block_data.items()


def _get_selected_option_value(body: dict, action_id: str) -> str | None:
    """Return ``selected_option.value`` for a view state action."""
    for aid, action_data in _iter_view_state_actions(body):
        if aid == action_id:
            return helpers.safe_get(action_data, "selected_option", "value")
    return None


def _get_text_input_value(body: dict, action_id: str) -> str | None:
    """Return plain-text ``value`` for a view state action."""
    for aid, action_data in _iter_view_state_actions(body):
        if aid == action_id:
            return action_data.get("value")
    return None


def _get_selected_conversation_or_option(body: dict, action_id: str) -> str | None:
    """Return selected conversation ID, falling back to selected option value."""
    for aid, action_data in _iter_view_state_actions(body):
        if aid == action_id:
            return action_data.get("selected_conversation") or helpers.safe_get(action_data, "selected_option", "value")
    return None


def _sanitize_text(value: str, max_length: int = 100) -> str:
    """Strip and truncate user-supplied text to prevent oversized DB writes."""
    if not value:
        return value
    return value.strip()[:max_length]
