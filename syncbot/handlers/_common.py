"""Shared handler utilities and types."""

import contextlib
from typing import Any

import helpers
from db import schemas
from logger import log_debug, log_error, log_warning

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
        log_debug("parse_private_metadata_bad_json", error=str(exc))
        return {}


_DM_WAIT_TEXT = "You can close this now. SyncBot will DM you when complete."
_STAY_WAIT_TEXT = "Please wait for this to complete."


def _wait_modal_view(
    *,
    title: str,
    dm: bool,
    text: str | None = None,
    callback_id: str | None = None,
    private_metadata: str | None = None,
) -> dict[str, Any]:
    """Close-only modal shown while slow work runs."""
    body = text if text is not None else (_DM_WAIT_TEXT if dm else _STAY_WAIT_TEXT)
    view: dict[str, Any] = {
        "type": "modal",
        "title": {"type": "plain_text", "text": title},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": body}}],
    }
    if callback_id is not None:
        view["callback_id"] = callback_id
    if private_metadata is not None:
        view["private_metadata"] = private_metadata
    return view


def _wait_modal_ack(
    *,
    title: str,
    callback_id: str,
    dm: bool,
    text: str | None = None,
    private_metadata: str | None = None,
) -> dict:
    """Ack payload that replaces the open modal with the wait view.

    View submissions return this dict. A block action's return value is ignored,
    so buttons use :func:`_update_wait_modal` instead.
    """
    return {
        "response_action": "update",
        "view": _wait_modal_view(
            title=title,
            dm=dm,
            text=text,
            callback_id=callback_id,
            private_metadata=private_metadata,
        ),
    }


def _update_wait_modal(
    client,
    body: dict,
    *,
    title: str,
    dm: bool,
    text: str | None = None,
    callback_id: str | None = None,
    private_metadata: str | None = None,
) -> None:
    """Replace the open modal with the wait view."""
    view_id = helpers.safe_get(body, "view", "id")
    if not view_id:
        return
    try:
        client.views_update(
            view_id=view_id,
            view=_wait_modal_view(
                title=title,
                dm=dm,
                text=text,
                callback_id=callback_id,
                private_metadata=private_metadata,
            ),
        )
    except Exception as exc:
        log_warning("modal_wait_update_failed", error=str(exc))


def _close_modal_done(client, body: dict, message: str) -> None:
    """Replace an open modal with a terminal, close-only acknowledgement.

    Destructive confirmations put a red ``danger`` button in the modal body
    rather than using the modal's submit button, which Slack renders in the
    theme colour and cannot be styled red. A block action cannot close a modal
    (only a view submission can), so once the work is done we swap the view for
    a close-only screen.
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
        log_warning("modal_close_failed", error=str(exc))


def _dm_user(client, user_id: str | None, message: str) -> None:
    """Send *message* to *user_id* as a DM, ignoring failures.

    Used to report work-phase failures. By then the modal is closed, so a field
    error is no longer possible and silence would look like success.
    """
    helpers.notify_user_dm(client, user_id, message)


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
        log_warning(log_event, **log_extra or {}, error=message)
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
        log_error(log_event, **log_extra or {}, error=str(exc))
        message = "SyncBot could not be added to that Channel. Please try again."
        details = {"error": str(exc), "event": log_event, "channel": channel_id}

    try:
        rollback()
    except Exception as rollback_exc:
        log_error(f"{log_event}_rollback_failed", channel_id=channel_id, error=str(rollback_exc))

    _dm_user(
        client,
        acting_user_id,
        helpers.format_error_dm(f":warning: {message}", details),
    )
    return False


def _get_authorized_workspace(
    body: dict, client, context: dict, action_name: str
) -> tuple[str, schemas.Workspace] | None:
    """Validate authorization and return ``(user_id, workspace_record)``.

    Returns *None* and logs a warning if the user is not authorized or
    the workspace cannot be resolved.
    """
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_manager(client, user_id, team_id):
        log_warning("authorization_denied", user_id=user_id, action=action_name)
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
