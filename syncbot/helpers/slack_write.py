"""Actor that posts, updates, deletes, or uploads on a target Channel."""

from __future__ import annotations

import logging
import re
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from helpers.conversations import get_user_token
from helpers.core import format_file_share_notice, safe_get
from helpers.encryption import decrypt_bot_token
from helpers.files import upload_files_to_slack
from helpers.message_blocks import rewrite_content_blocks, trim_target_blocks
from helpers.slack_api import delete_message, post_message, slack_error_code
from helpers.user_action_echo import remember_user_action, slack_message_ts
from helpers.user_map import (
    apply_mentioned_users,
    get_display_name_and_icon_for_synced_message,
    parse_mentioned_users,
    resolve_channel_references,
    resolve_mention_for_workspace,
    unmapped_author_label,
)

_logger = logging.getLogger(__name__)

_NO_AUTHORIZE_ERRORS = frozenset({"invalid_auth", "not_authed", "token_revoked", "missing_scope", "account_inactive"})
_USER_WRITE_ERRORS = _NO_AUTHORIZE_ERRORS | frozenset({"not_in_channel", "channel_not_found"})


def _bot_token_for(workspace) -> str:
    return decrypt_bot_token(workspace.bot_token)


def pick_write_token(workspace, mapped_user_id: str | None) -> tuple[str, str | None]:
    """Return ``(token, posted_as_user_id)``. Null posted_as means bot customize."""
    if mapped_user_id:
        user_token = get_user_token(workspace.team_id, mapped_user_id)
        if user_token:
            return user_token, mapped_user_id
    return _bot_token_for(workspace), None


def remember_message_echo(team_id: str | None, user_id: str | None, channel_id: str, ts: str) -> None:
    if team_id and user_id and channel_id and ts:
        remember_user_action(team_id, user_id, "message", f"{channel_id}:{slack_message_ts(ts)}")


def build_target_blocks(
    *,
    content_blocks: list[dict],
    photo_blocks: list[dict],
    source_client: WebClient,
    target_client: WebClient,
    source_workspace_id: int,
    target_workspace_id: int,
    source_ws,
    source_workspace_name: str | None,
    mentioned_users: list[dict],
) -> list[dict]:
    """Content blocks rewritten for the target, plus image blocks."""
    if not content_blocks:
        return list(photo_blocks or [])

    def rewrite_mrkdwn(text: str) -> str:
        adapted = resolve_channel_references(text, source_client, source_ws)

        def repl(match: re.Match) -> str:
            return resolve_mention_for_workspace(
                source_client,
                match.group(1),
                source_workspace_id,
                target_client,
                target_workspace_id,
            )

        return re.sub(r"<@(\w+)>", repl, adapted or "")

    resolved_by_uid: dict[str, str] = {}

    def map_user_id(uid: str) -> str | None:
        tag = resolve_mention_for_workspace(
            source_client,
            uid,
            source_workspace_id,
            target_client,
            target_workspace_id,
        )
        resolved_by_uid[uid] = tag
        m = re.fullmatch(r"<@(\w+)>", tag or "")
        return m.group(1) if m else None

    names = {u.get("user_id"): u.get("user_name") for u in mentioned_users or []}

    def unmapped_label(uid: str) -> str:
        tag = resolved_by_uid.get(uid)
        if tag and not re.fullmatch(r"<@\w+>", tag):
            return tag
        return unmapped_author_label(names.get(uid) or uid, source_workspace_name)

    rewritten = rewrite_content_blocks(content_blocks, rewrite_mrkdwn, map_user_id, unmapped_label)
    return trim_target_blocks(rewritten + (photo_blocks or []))


def slack_write_create(
    *,
    envelope: dict[str, Any],
    sync_channel,
    workspace,
    source_client: WebClient | None = None,
    thread_ts: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Create a message (and optional files) on the target. Returns (ts, split_file_ts, posted_as)."""
    from helpers.workspace import get_workspace_by_id

    source_workspace_id = envelope.get("source_workspace_id") or 0
    source_user_id = envelope.get("source_user_id")
    user_name = envelope.get("user_name")
    user_avatar_url = envelope.get("user_avatar_url")
    workspace_name = envelope.get("workspace_name")
    msg_text = envelope.get("text") or ""
    reply_broadcast = bool(envelope.get("reply_broadcast"))
    file_refs = list(envelope.get("file_refs") or [])
    content_blocks = list(envelope.get("blocks") or [])
    images = list(envelope.get("images") or [])

    bot_token = _bot_token_for(workspace)
    target_client = WebClient(token=bot_token)
    mapped_user_id = envelope.get("mapped_user_id")
    if mapped_user_id:
        target_display_name, target_icon_url, author_is_mapped = user_name, user_avatar_url, True
    else:
        target_display_name, target_icon_url, author_is_mapped, mapped_user_id = (
            get_display_name_and_icon_for_synced_message(
                source_user_id or "",
                source_workspace_id,
                user_name,
                user_avatar_url,
                target_client,
                workspace.id,
                source_client=source_client,
            )
        )
    name_for_target = target_display_name or user_name or "Someone"
    remote_workspace_label = None if author_is_mapped else workspace_name
    file_notice = format_file_share_notice(name_for_target, remote_workspace_label)

    write_token, posted_as = pick_write_token(workspace, mapped_user_id)
    use_customize = posted_as is None

    mentioned_users: list[dict] = []
    if source_client and msg_text:
        mentioned_users = parse_mentioned_users(msg_text, source_client)

    adapted_text = msg_text
    source_ws = get_workspace_by_id(source_workspace_id) if source_workspace_id else None
    if source_client:
        adapted_text = apply_mentioned_users(
            msg_text,
            source_client,
            target_client,
            mentioned_users,
            source_workspace_id=source_workspace_id,
            target_workspace_id=workspace.id,
        )
        adapted_text = resolve_channel_references(adapted_text, source_client, source_ws)

    target_blocks: list[dict] = []
    if source_client and (content_blocks or images):
        target_blocks = build_target_blocks(
            content_blocks=content_blocks,
            photo_blocks=images,
            source_client=source_client,
            target_client=target_client,
            source_workspace_id=source_workspace_id,
            target_workspace_id=workspace.id,
            source_ws=source_ws,
            source_workspace_name=workspace_name,
            mentioned_users=mentioned_users,
        )
    elif images:
        target_blocks = list(images)

    def _write(token: str, customize: bool, as_user: str | None) -> tuple[str | None, str | None, str | None]:
        if file_refs and not msg_text.strip() and not content_blocks:
            _, file_ts = upload_files_to_slack(
                bot_token=token,
                channel_id=sync_channel.channel_id,
                files=file_refs,
                initial_comment=file_notice,
                thread_ts=thread_ts,
                reply_broadcast=reply_broadcast,
            )
            if as_user and file_ts:
                remember_message_echo(workspace.team_id, as_user, sync_channel.channel_id, file_ts)
            return file_ts, None, as_user

        post_kwargs: dict[str, Any] = {
            "bot_token": token,
            "channel_id": sync_channel.channel_id,
            "msg_text": adapted_text,
            "blocks": target_blocks or None,
            "thread_ts": thread_ts,
            "reply_broadcast": reply_broadcast,
        }
        if customize:
            post_kwargs["user_name"] = name_for_target
            post_kwargs["user_profile_url"] = target_icon_url or user_avatar_url
            post_kwargs["workspace_name"] = remote_workspace_label

        res = post_message(**post_kwargs)
        ts = safe_get(res, "ts")
        split_file_ts: str | None = None

        if file_refs and ts:
            file_thread_ts = thread_ts or ts
            file_broadcast = reply_broadcast or thread_ts is None
            _, split_file_ts = upload_files_to_slack(
                bot_token=token,
                channel_id=sync_channel.channel_id,
                files=file_refs,
                initial_comment=file_notice,
                thread_ts=file_thread_ts,
                reply_broadcast=file_broadcast,
            )

        if as_user and ts:
            remember_message_echo(workspace.team_id, as_user, sync_channel.channel_id, ts)
        if as_user and split_file_ts:
            remember_message_echo(workspace.team_id, as_user, sync_channel.channel_id, split_file_ts)
        return ts, split_file_ts, as_user

    try:
        return _write(write_token, use_customize, posted_as)
    except SlackApiError as exc:
        code = slack_error_code(exc)
        if posted_as and code in _USER_WRITE_ERRORS:
            _logger.info(
                "slack_write_user_fallback_bot",
                extra={"channel_id": sync_channel.channel_id, "error": code},
            )
            try:
                return _write(bot_token, True, None)
            except Exception as retry_exc:
                _logger.warning(
                    "slack_write_bot_fallback_failed",
                    extra={"channel_id": sync_channel.channel_id, "error": str(retry_exc)},
                )
                return None, None, None
        _logger.warning(
            "slack_write_create_failed",
            extra={"channel_id": sync_channel.channel_id, "error": str(exc)},
        )
        return None, None, None
    except Exception as exc:
        _logger.warning(
            "slack_write_create_failed",
            extra={"channel_id": sync_channel.channel_id, "error": str(exc)},
        )
        return None, None, None


def slack_write_edit(
    *,
    envelope: dict[str, Any],
    sync_channel,
    workspace,
    target_post_meta,
    source_client: WebClient | None = None,
) -> bool:
    """Edit an existing target message. Sticky token from posted_as_user_id."""
    from helpers.workspace import get_workspace_by_id

    posted_as = getattr(target_post_meta, "posted_as_user_id", None)
    update_ts = slack_message_ts(target_post_meta.ts)
    msg_text = envelope.get("text") or ""
    content_blocks = list(envelope.get("blocks") or [])
    images = list(envelope.get("images") or [])
    source_workspace_id = envelope.get("source_workspace_id") or 0
    workspace_name = envelope.get("workspace_name")

    bot_token = _bot_token_for(workspace)
    target_client = WebClient(token=bot_token)
    token = bot_token
    if posted_as:
        user_token = get_user_token(workspace.team_id, posted_as)
        if user_token:
            token = user_token
        else:
            _logger.warning(
                "slack_write_edit_skip_no_user_token",
                extra={"channel_id": sync_channel.channel_id, "posted_as": posted_as},
            )
            return False

    mentioned_users: list[dict] = []
    adapted_text = msg_text
    target_blocks: list[dict] = []
    source_ws = get_workspace_by_id(source_workspace_id) if source_workspace_id else None
    if source_client:
        mentioned_users = parse_mentioned_users(msg_text, source_client)
        adapted_text = apply_mentioned_users(
            msg_text,
            source_client,
            target_client,
            mentioned_users,
            source_workspace_id=source_workspace_id,
            target_workspace_id=workspace.id,
        )
        adapted_text = resolve_channel_references(adapted_text, source_client, source_ws)
        if content_blocks or images:
            target_blocks = build_target_blocks(
                content_blocks=content_blocks,
                photo_blocks=images,
                source_client=source_client,
                target_client=target_client,
                source_workspace_id=source_workspace_id,
                target_workspace_id=workspace.id,
                source_ws=source_ws,
                source_workspace_name=workspace_name,
                mentioned_users=mentioned_users,
            )
    elif images:
        target_blocks = list(images)

    try:
        post_message(
            bot_token=token,
            channel_id=sync_channel.channel_id,
            msg_text=adapted_text,
            update_ts=update_ts,
            blocks=target_blocks or None,
        )
        if posted_as:
            remember_message_echo(workspace.team_id, posted_as, sync_channel.channel_id, update_ts)
        return True
    except Exception as exc:
        _logger.warning(
            "slack_write_edit_failed",
            extra={"channel_id": sync_channel.channel_id, "error": str(exc)},
        )
        return False


def slack_write_delete(*, sync_channel, workspace, target_post_meta) -> bool:
    """Delete a target message. Sticky token from posted_as_user_id."""
    posted_as = getattr(target_post_meta, "posted_as_user_id", None)
    ts = slack_message_ts(target_post_meta.ts)
    bot_token = _bot_token_for(workspace)
    token = bot_token
    if posted_as:
        user_token = get_user_token(workspace.team_id, posted_as)
        if user_token:
            token = user_token
        else:
            _logger.warning(
                "slack_write_delete_skip_no_user_token",
                extra={"channel_id": sync_channel.channel_id, "posted_as": posted_as},
            )
            return False
    try:
        delete_message(bot_token=token, channel_id=sync_channel.channel_id, ts=ts)
        if posted_as:
            remember_message_echo(workspace.team_id, posted_as, sync_channel.channel_id, ts)
        return True
    except Exception as exc:
        _logger.warning(
            "slack_write_delete_failed",
            extra={"channel_id": sync_channel.channel_id, "error": str(exc)},
        )
        return False
