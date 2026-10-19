"""Actor that posts, updates, deletes, or uploads on a target Channel."""

from __future__ import annotations

import re
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from helpers.conversations import get_user_token
from helpers.core import format_synced_from_line, safe_get
from helpers.files import upload_files_to_slack
from helpers.message_blocks import blocks_include_body, rewrite_content_blocks, trim_target_blocks
from helpers.notifications import notify_source_user_error
from helpers.slack_api import delete_message, post_message, slack_error_code
from helpers.user_action_echo import remember_pending_file_share, remember_user_action, slack_message_ts
from helpers.user_map import (
    apply_mentioned_users,
    format_unmapped_author_label,
    get_display_name_and_icon_for_synced_message,
    parse_mentioned_users,
    resolve_channel_references,
    resolve_mention_for_workspace,
)
from helpers.workspace import get_bot_token
from logger import log_debug, log_error, log_info, log_warning

_NO_AUTHORIZE_ERRORS = frozenset({"invalid_auth", "not_authed", "token_revoked", "missing_scope", "account_inactive"})
_USER_WRITE_ERRORS = _NO_AUTHORIZE_ERRORS | frozenset({"not_in_channel", "channel_not_found"})


def _bot_token_for(workspace) -> str:
    token = get_bot_token(workspace)
    if not token:
        raise ValueError("bot_token_unavailable")
    return token


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


def remember_file_echoes(team_id: str | None, user_id: str | None, file_ids) -> None:
    """Remember Slack file ids so inbound ``file_share`` events can be skipped."""
    if not team_id or not user_id:
        return
    for file_id in file_ids:
        if file_id:
            remember_user_action(team_id, user_id, "file", str(file_id))


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
        return format_unmapped_author_label(names.get(uid) or uid, source_workspace_name)

    rewritten = rewrite_content_blocks(content_blocks, rewrite_mrkdwn, map_user_id, unmapped_label)
    return trim_target_blocks(rewritten + (photo_blocks or []))


def _notify_file_write_failed(
    *,
    envelope: dict[str, Any],
    source_client: WebClient | None,
    channel_id: str,
    error: str,
) -> None:
    log_error(
        "file_share_failed",
        reason="upload_failed",
        channel_id=channel_id,
        error=error,
        post_id=envelope.get("post_id"),
        thread_post_id=envelope.get("thread_post_id"),
    )
    summary = (
        ":warning: SyncBot could not copy your file because that workspace's file storage is full."
        if error == "storage_limit_reached"
        else ":warning: SyncBot could not copy your file to the other Channels."
    )
    notify_source_user_error(
        source_client=source_client,
        source_user_id=envelope.get("source_user_id"),
        summary=summary,
        details={
            "event": "file_share_failed",
            "reason": "upload_failed",
            "error": error,
            "channel": channel_id,
        },
    )


def _blocks_for_file_share(adapted_text: str, target_blocks: list[dict] | None) -> list[dict] | None:
    """Message body for a file share. Slack drops blocks when a comment is also set."""
    blocks = list(target_blocks or [])
    text = (adapted_text or "").strip()
    if text and not blocks_include_body(blocks):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}] + blocks
    return blocks or None


def _upload_target_files(
    *,
    token: str,
    channel_id: str,
    files: list,
    initial_comment: str | None,
    thread_ts: str | None,
    reply_broadcast: bool,
    team_id: str | None,
    as_user: str | None,
    post_id: str | None = None,
    sync_channel_id: int | None = None,
    source_user_id: str | None = None,
    source_workspace_id: int | None = None,
    blocks: list[dict] | None = None,
    username: str | None = None,
    icon_url: str | None = None,
) -> str | None:
    """Upload files on the target; remember echo when posted as a mapped user."""

    uploaded_ids: list[str] = []

    def _remember_files(file_ids) -> None:
        uploaded_ids.extend(str(file_id) for file_id in file_ids if file_id)
        if as_user:
            remember_file_echoes(team_id, as_user, file_ids)

    def _remember_ts(ts: str) -> None:
        if as_user:
            remember_message_echo(team_id, as_user, channel_id, ts)

    _, file_ts = upload_files_to_slack(
        bot_token=token,
        channel_id=channel_id,
        files=files,
        initial_comment=initial_comment,
        blocks=blocks,
        username=username,
        icon_url=icon_url,
        thread_ts=thread_ts,
        reply_broadcast=reply_broadcast,
        after_upload=_remember_files,
        after_share_ts=_remember_ts,
    )
    if not file_ts and post_id and team_id and sync_channel_id:
        for file_id in uploaded_ids:
            remember_pending_file_share(
                team_id,
                channel_id,
                file_id,
                post_id,
                sync_channel_id=sync_channel_id,
                source_user_id=source_user_id,
                source_workspace_id=source_workspace_id,
                posted_as_user_id=as_user,
            )
        if uploaded_ids:
            log_debug(
                "file_share_ts",
                channel_id=channel_id,
                ts=None,
                source="pending",
                post_id=post_id,
                file_id=uploaded_ids[0],
            )
    return file_ts


def _post_target_text(
    *,
    token: str,
    channel_id: str,
    adapted_text: str,
    target_blocks: list[dict] | None,
    thread_ts: str | None,
    reply_broadcast: bool,
    customize: bool,
    name_for_target: str,
    target_icon_url: str | None,
    user_avatar_url: str | None,
    remote_workspace_label: str | None,
    team_id: str | None,
    as_user: str | None,
) -> tuple[str | None, str | None]:
    """Post a target message that has no file."""
    post_kwargs: dict[str, Any] = {
        "bot_token": token,
        "channel_id": channel_id,
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
    if as_user and ts:
        remember_message_echo(team_id, as_user, channel_id, ts)
    return ts, as_user


def _mentioned_users_from_envelope(envelope: dict[str, Any]) -> list[dict]:
    """Mention rows from envelope ``people`` (author excluded)."""
    people = envelope.get("people") or []
    source_user_id = envelope.get("source_user_id")
    out: list[dict] = []
    for person in people:
        uid = person.get("user_id")
        if not uid or uid == source_user_id:
            continue
        out.append(
            {
                "user_id": uid,
                "user_name": person.get("name") or uid,
                "email": person.get("email"),
                "user_profile_url": person.get("avatar_url"),
            }
        )
    return out


def slack_write_create(
    *,
    envelope: dict[str, Any],
    sync_channel,
    workspace,
    source_client: WebClient | None = None,
    thread_ts: str | None = None,
) -> tuple[str | None, str | None]:
    """Create a message (and optional files) on the target. Returns (ts, posted_as)."""
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
    post_id = envelope.get("post_id")

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

    write_token, posted_as = pick_write_token(workspace, mapped_user_id)
    use_customize = posted_as is None

    mentioned_users = _mentioned_users_from_envelope(envelope)
    if source_client and msg_text and "<@" in msg_text and not mentioned_users:
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
    elif content_blocks:
        target_blocks = trim_target_blocks(list(content_blocks) + list(images or []))
    elif images:
        target_blocks = list(images)

    pending_apply = {
        "sync_channel_id": sync_channel.id,
        "source_user_id": source_user_id or None,
        "source_workspace_id": envelope.get("source_workspace_id") or None,
    }

    def _write(token: str, customize: bool, as_user: str | None) -> tuple[str | None, str | None]:
        # The file share is the message. Blocks and a comment together drop the blocks.
        # Bot customize sets the from line on every share, including file-only.
        if file_refs:
            share_blocks = _blocks_for_file_share(adapted_text, target_blocks) if target_blocks else None
            file_ts = _upload_target_files(
                token=token,
                channel_id=sync_channel.channel_id,
                files=file_refs,
                initial_comment=None if share_blocks else ((adapted_text or "").strip() or None),
                blocks=share_blocks,
                username=format_synced_from_line(name_for_target, remote_workspace_label) if customize else None,
                icon_url=(target_icon_url or user_avatar_url) if customize else None,
                thread_ts=thread_ts,
                reply_broadcast=reply_broadcast,
                team_id=workspace.team_id,
                as_user=as_user,
                post_id=post_id,
                **pending_apply,
            )
            return file_ts, as_user
        if not (adapted_text or "").strip() and not target_blocks:
            raise RuntimeError("empty_message_create")
        return _post_target_text(
            token=token,
            channel_id=sync_channel.channel_id,
            adapted_text=adapted_text,
            target_blocks=target_blocks,
            thread_ts=thread_ts,
            reply_broadcast=reply_broadcast,
            customize=customize,
            name_for_target=name_for_target,
            target_icon_url=target_icon_url,
            user_avatar_url=user_avatar_url,
            remote_workspace_label=remote_workspace_label,
            team_id=workspace.team_id,
            as_user=as_user,
        )

    def _fail_create(exc: Exception) -> tuple[None, None]:
        log_warning("slack_write_create_failed", channel_id=sync_channel.channel_id, error=str(exc))
        if file_refs:
            _notify_file_write_failed(
                envelope=envelope,
                source_client=source_client,
                channel_id=sync_channel.channel_id,
                error=slack_error_code(exc) or str(exc),
            )
        return None, None

    try:
        return _write(write_token, use_customize, posted_as)
    except SlackApiError as exc:
        code = slack_error_code(exc)
        if posted_as and code in _USER_WRITE_ERRORS:
            log_info("slack_write_user_fallback_bot", channel_id=sync_channel.channel_id, error=code)
            try:
                return _write(bot_token, True, None)
            except Exception as retry_exc:
                return _fail_create(retry_exc)
        return _fail_create(exc)
    except Exception as exc:
        return _fail_create(exc)


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
    used_user_token = False
    if posted_as:
        user_token = get_user_token(workspace.team_id, posted_as)
        if user_token:
            token = user_token
            used_user_token = True
        else:
            log_info("slack_write_user_fallback_bot", channel_id=sync_channel.channel_id, error="no_user_token")

    mentioned_users: list[dict] = []
    adapted_text = msg_text
    target_blocks: list[dict] = []
    source_ws = get_workspace_by_id(source_workspace_id) if source_workspace_id else None
    if source_client:
        mentioned_users = _mentioned_users_from_envelope(envelope)
        if msg_text and "<@" in msg_text and not mentioned_users:
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
    elif content_blocks:
        target_blocks = trim_target_blocks(list(content_blocks) + list(images or []))
    elif images:
        target_blocks = list(images)

    def _update(write_token: str, as_user: str | None) -> bool:
        post_message(
            bot_token=write_token,
            channel_id=sync_channel.channel_id,
            msg_text=adapted_text,
            update_ts=update_ts,
            blocks=target_blocks or None,
        )
        if as_user:
            remember_message_echo(workspace.team_id, as_user, sync_channel.channel_id, update_ts)
        return True

    try:
        return _update(token, posted_as if used_user_token else None)
    except SlackApiError as exc:
        code = slack_error_code(exc)
        if used_user_token and code in _USER_WRITE_ERRORS | {"cant_update_message"}:
            log_info("slack_write_user_fallback_bot", channel_id=sync_channel.channel_id, error=code)
            try:
                return _update(bot_token, None)
            except SlackApiError as retry_exc:
                retry_code = slack_error_code(retry_exc)
                log_warning(
                    "slack_write_edit_failed", channel_id=sync_channel.channel_id, error=retry_code or str(retry_exc)
                )
                return False
            except Exception as retry_exc:
                log_warning("slack_write_edit_failed", channel_id=sync_channel.channel_id, error=str(retry_exc))
                return False
        log_warning("slack_write_edit_failed", channel_id=sync_channel.channel_id, error=code or str(exc))
        return False
    except Exception as exc:
        log_warning("slack_write_edit_failed", channel_id=sync_channel.channel_id, error=str(exc))
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
            log_warning(
                "slack_write_delete_skip_no_user_token", channel_id=sync_channel.channel_id, posted_as=posted_as
            )
            return False
    try:
        delete_message(bot_token=token, channel_id=sync_channel.channel_id, ts=ts)
        if posted_as:
            remember_message_echo(workspace.team_id, posted_as, sync_channel.channel_id, ts)
        return True
    except Exception as exc:
        log_warning("slack_write_delete_failed", channel_id=sync_channel.channel_id, error=str(exc))
        return False
