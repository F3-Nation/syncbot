"""Slack API wrappers with automatic retry and rate-limit handling."""

import hashlib
import json
import logging
import time as _time
from functools import wraps

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from helpers._cache import _USER_INFO_CACHE_TTL, _cache_get, _cache_set
from helpers.core import safe_get, synced_from_line_username
from helpers.message_blocks import blocks_include_body, event_layout_blocks

_logger = logging.getLogger(__name__)

_SLACK_MAX_RETRIES = 3
_SLACK_INITIAL_BACKOFF = 1.0  # seconds


def slack_retry(fn):
    """Decorator that retries Slack API calls on rate-limit and server errors."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        last_exc: Exception | None = None
        backoff = _SLACK_INITIAL_BACKOFF

        for attempt in range(_SLACK_MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except SlackApiError as exc:
                last_exc = exc
                status = exc.response.status_code if exc.response else 0

                if status == 429:
                    retry_after = float(exc.response.headers.get("Retry-After", backoff))
                    _logger.warning(f"{fn.__name__} rate-limited (attempt {attempt + 1}), sleeping {retry_after:.1f}s")
                    _time.sleep(retry_after)
                    backoff = min(backoff * 2, 30)
                elif 500 <= status < 600:
                    _logger.warning(
                        f"{fn.__name__} server error {status} (attempt {attempt + 1}), retrying in {backoff:.1f}s"
                    )
                    _time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                else:
                    raise
        raise last_exc

    return wrapper


@slack_retry
def _users_info(client: WebClient, user_id: str) -> dict:
    """Low-level wrapper so the retry decorator can catch SlackApiError."""
    return client.users_info(user=user_id)


def _token_fingerprint(client: WebClient) -> str | None:
    """Short hash of this client's token for cache keys. Never log the token."""
    token = getattr(client, "token", None)
    if not isinstance(token, str) or not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _get_auth_info(client: WebClient, *, bypass_cache: bool = False) -> dict | None:
    """Call ``auth.test`` and cache both bot_id and user_id per bot token.

    The cache key must include the token. A single process-wide entry would
    reuse workspace A's bot member ID on workspace B, and
    ``conversations.invite`` then fails with ``user_not_found``.
    """
    fingerprint = _token_fingerprint(client)
    cache_key = f"own_auth_info:{fingerprint}" if fingerprint else None
    if cache_key and not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    try:
        res = client.auth_test()
        info = {"bot_id": safe_get(res, "bot_id"), "user_id": safe_get(res, "user_id")}
        if cache_key:
            _cache_set(cache_key, info, ttl=3600)
        return info
    except Exception:
        _logger.warning("Could not determine own identity via auth.test")
        return None


def get_own_bot_id(client: WebClient, context: dict) -> str | None:
    """Return SyncBot's own ``bot_id`` for the current workspace."""
    bot_id = context.get("bot_id")
    if bot_id:
        return bot_id
    info = _get_auth_info(client)
    return info["bot_id"] if info else None


def get_own_bot_user_id(
    client: WebClient,
    context: dict | None = None,
    *,
    bypass_cache: bool = False,
) -> str | None:
    """Return SyncBot's own *user* ID (``U…``) for the current workspace.

    Prefer Bolt's request-scoped ``bot_user_id`` when present. ``auth.test`` is
    cached per bot token so a warm Lambda cannot hand workspace A's identity
    to workspace B.
    """
    if not bypass_cache and context:
        bot_user_id = context.get("bot_user_id")
        if bot_user_id:
            return bot_user_id
    info = _get_auth_info(client, bypass_cache=bypass_cache)
    return info["user_id"] if info else None


def get_bot_info_from_event(body: dict) -> tuple[str | None, str | None]:
    """Extract display name and icon URL from a bot_message event."""
    event = body.get("event", {})
    bot_name = event.get("username") or "Bot"
    icons = event.get("icons") or {}
    icon_url = icons.get("image_48") or icons.get("image_36") or icons.get("image_72")
    return bot_name, icon_url


def slack_error_code(exc: BaseException | None) -> str:
    """Return Slack's ``error`` string from a ``SlackApiError``, or empty."""
    if exc is None:
        return ""
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    if isinstance(resp, dict):
        err = resp.get("error")
        return str(err) if err else ""
    try:
        err = resp.get("error")
        if err:
            return str(err)
    except Exception:
        pass
    data = getattr(resp, "data", None)
    if isinstance(data, dict):
        err = data.get("error")
        return str(err) if err else ""
    return ""


def get_user_info(client: WebClient, user_id: str) -> tuple[str | None, str | None]:
    """Return (display_name, profile_image_url) for a Slack user."""
    fingerprint = _token_fingerprint(client)
    cache_key = f"user_info:{fingerprint}:{user_id}" if fingerprint else f"user_info:{user_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        res = _users_info(client, user_id)
    except SlackApiError as exc:
        _logger.debug(f"get_user_info: failed to look up user {user_id}: {exc}")
        return None, None

    user_name = (
        safe_get(res, "user", "profile", "display_name") or safe_get(res, "user", "profile", "real_name") or None
    )
    user_profile_url = safe_get(res, "user", "profile", "image_192")

    result = (user_name, user_profile_url)
    _cache_set(cache_key, result, ttl=_USER_INFO_CACHE_TTL)
    return result


@slack_retry
def _conversations_history(client: WebClient, **kwargs) -> dict:
    """Low-level wrapper so the retry decorator can catch SlackApiError."""
    return client.conversations_history(**kwargs)


def fetch_message_layout_blocks(client: WebClient, event: dict) -> list[dict]:
    """Load Block Kit from ``conversations.history`` when the Events payload omitted it.

    Bot posts sometimes arrive with flattened ``text`` and no ``blocks``. The
    client ``Show more`` control is not a second payload; history is the full
    message Slack stored.
    """
    if not event:
        return []
    nested = event.get("message") if isinstance(event.get("message"), dict) else {}
    channel = event.get("channel") or nested.get("channel")
    # message_changed uses event.ts as the edit-event id; the stored message is message.ts.
    ts = nested.get("ts") or event.get("ts")
    if not channel or not ts:
        return []
    try:
        res = _conversations_history(client, channel=channel, latest=str(ts), inclusive=True, limit=1)
    except SlackApiError as exc:
        _logger.debug("fetch_message_layout_blocks failed: %s", exc)
        return []
    messages = res.get("messages") if res is not None else None
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
        return []
    return event_layout_blocks(messages[0])


@slack_retry
def post_message(
    bot_token: str,
    channel_id: str,
    msg_text: str,
    user_name: str | None = None,
    user_profile_url: str | None = None,
    thread_ts: str | None = None,
    update_ts: str | None = None,
    workspace_name: str | None = None,
    blocks: list[dict] | None = None,
    reply_broadcast: bool = False,
) -> dict:
    """Post or update a message in a Slack channel."""
    slack_client = WebClient(bot_token)
    if blocks:
        if msg_text.strip() and not blocks_include_body(blocks):
            msg_block = {"type": "section", "text": {"type": "mrkdwn", "text": msg_text}}
            all_blocks = [msg_block] + blocks
        else:
            all_blocks = blocks
    else:
        all_blocks = []
    fallback_text = msg_text if msg_text.strip() else "Shared a file"
    if update_ts:
        res = slack_client.chat_update(
            channel=channel_id,
            text=fallback_text,
            ts=update_ts,
            blocks=all_blocks,
            unfurl_links=False,
            unfurl_media=False,
        )
    else:
        username_str = synced_from_line_username(user_name, workspace_name) if user_name else None
        kwargs: dict = {
            "channel": channel_id,
            "text": fallback_text,
            "username": username_str,
            "icon_url": user_profile_url,
            "thread_ts": thread_ts,
            "blocks": all_blocks,
            # Source permalinks must not unfurl as target messages. This does not
            # change Slack web treating archives/p URLs as the target (Private chip).
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if reply_broadcast:
            kwargs["reply_broadcast"] = True
        res = slack_client.chat_postMessage(**kwargs)
    return res


@slack_retry
def delete_message(bot_token: str, channel_id: str, ts: str) -> dict:
    """Delete a message from a Slack channel."""
    slack_client = WebClient(bot_token)
    res = slack_client.chat_delete(
        channel=channel_id,
        ts=ts,
    )
    return res


def update_modal(
    blocks: list[dict],
    client: WebClient,
    view_id: str,
    title_text: str,
    callback_id: str,
    submit_button_text: str = "Submit",
    parent_metadata: dict | None = None,
    close_button_text: str = "Close",
    notify_on_close: bool = False,
) -> None:
    """Replace the contents of an existing Slack modal."""
    view = {
        "type": "modal",
        "callback_id": callback_id,
        "title": {"type": "plain_text", "text": title_text},
        "submit": {"type": "plain_text", "text": submit_button_text},
        "close": {"type": "plain_text", "text": close_button_text},
        "notify_on_close": notify_on_close,
        "blocks": blocks,
    }
    if parent_metadata:
        view["private_metadata"] = json.dumps(parent_metadata)

    client.views_update(view_id=view_id, view=view)
