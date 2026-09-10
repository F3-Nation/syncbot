"""Per-channel reaction style and apply helpers."""

from __future__ import annotations

import logging
from typing import Literal

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import constants
from db import DbManager, schemas
from helpers.conversations import get_user_token
from helpers.core import safe_get
from helpers.encryption import decrypt_bot_token
from helpers.reaction_notice import delete_notices_for_unreact, reaction_notice_post_id
from helpers.slack_api import slack_error_code
from helpers.sync_participation import channel_subscribes
from helpers.user_action_echo import reaction_echo_fingerprint, remember_user_action, slack_message_ts

_logger = logging.getLogger(__name__)

ApplyResult = Literal["direct", "thread", "skipped", "failed"]
_slack_error_code = slack_error_code


def reaction_style(sync_channel: schemas.SyncChannel | None) -> str | None:
    raw = getattr(sync_channel, "reaction_style", None)
    if raw is None or str(raw).strip() == "":
        if channel_subscribes(sync_channel):
            return constants.DEFAULT_REACTION_STYLE_EXISTING
        return None
    return str(raw).strip()


def default_reaction_style_for_new_channel(*, subscribes: bool) -> str | None:
    if subscribes:
        return constants.DEFAULT_REACTION_STYLE_NEW_RECEIVE
    return None


def _mapped_user_for_target(
    source_user_id: str | None,
    source_workspace_id: int | None,
    target_workspace_id: int,
    *,
    source_client: WebClient | None = None,
    target_client: WebClient | None = None,
    target_workspace: schemas.Workspace | None = None,
) -> str | None:
    from helpers.user_map import ensure_mapped_target_user_id, get_mapped_target_user_id

    if not source_user_id or not source_workspace_id:
        return None

    existing = get_mapped_target_user_id(source_user_id, source_workspace_id, target_workspace_id)
    if existing:
        return existing

    # Build Slack clients only when we may need an on-the-fly email map.
    if target_client is None and target_workspace is not None and getattr(target_workspace, "bot_token", None):
        try:
            target_client = WebClient(token=decrypt_bot_token(target_workspace.bot_token))
        except Exception:
            target_client = None
    if source_client is None:
        try:
            from helpers.workspace import get_workspace_by_id

            source_ws = get_workspace_by_id(source_workspace_id)
            if source_ws and source_ws.bot_token:
                source_client = WebClient(token=decrypt_bot_token(source_ws.bot_token))
        except Exception:
            source_client = None

    return ensure_mapped_target_user_id(
        source_user_id,
        source_workspace_id,
        target_workspace_id,
        source_client=source_client,
        target_client=target_client,
    )


def _post_threaded_reaction_notice(
    *,
    target_client: WebClient,
    sync_channel: schemas.SyncChannel,
    post_meta: schemas.PostMeta,
    reaction: str,
    display_name: str,
    icon_url: str | None,
    posted_from: str,
    author_is_mapped: bool,
    source_user_id: str | None,
    source_workspace_id: int | None,
    federated_instance_id: str | None = None,
) -> schemas.PostMeta | None:
    target_msg_ts = f"{post_meta.ts:.6f}"
    reaction_username_suffix = "" if author_is_mapped else posted_from
    permalink = None
    try:
        plink_resp = target_client.chat_getPermalink(
            channel=sync_channel.channel_id,
            message_ts=target_msg_ts,
        )
        permalink = safe_get(plink_resp, "permalink")
    except Exception as exc:
        _logger.debug(
            "reaction_permalink_lookup_failed",
            extra={"channel_id": sync_channel.channel_id, "message_ts": target_msg_ts, "error": str(exc)},
        )

    if permalink:
        msg_text = f"reacted with :{reaction}: to <{permalink}|this message>"
    else:
        msg_text = f"reacted with :{reaction}:"

    resp = target_client.chat_postMessage(
        channel=sync_channel.channel_id,
        text=msg_text,
        username=f"{display_name} {reaction_username_suffix}".strip(),
        icon_url=icon_url,
        thread_ts=target_msg_ts,
        unfurl_links=False,
        unfurl_media=False,
    )
    ts = safe_get(resp, "ts")
    if not ts or not source_user_id:
        return None

    parent_post_id = str(getattr(post_meta, "post_id", None) or "")
    notice_post_id = reaction_notice_post_id(
        parent_post_id=parent_post_id,
        reaction=reaction,
        source_user_id=source_user_id,
        source_workspace_id=source_workspace_id,
        federated_instance_id=federated_instance_id,
    )
    return schemas.PostMeta(
        post_id=notice_post_id,
        sync_channel_id=sync_channel.id,
        ts=float(ts),
        kind=constants.POST_META_KIND_REACTION_NOTICE,
        parent_post_id=parent_post_id,
        reaction=reaction,
        source_user_id=source_user_id,
        source_workspace_id=source_workspace_id,
    )


_NO_AUTHORIZE_ERRORS = frozenset({"invalid_auth", "not_authed", "token_revoked", "missing_scope", "account_inactive"})
_IDEMPOTENT_ADD_ERRORS = frozenset({"already_reacted", "already_added"})


def _target_reaction_name_is_invalid(
    bot_client: WebClient,
    *,
    team_id: str | None,
    channel_id: str,
    target_ts: str,
    reaction: str,
    cache: dict[tuple[str, str], bool] | None = None,
) -> bool | None:
    """Whether the target Slack workspace rejects this emoji name.

    Returns ``True`` for ``invalid_name``, ``False`` when the name exists, and
    ``None`` when the probe did not settle it. Called only on the Hybrid thread
    path (no target user token, or that token hit an auth error). ``invalid_name``
    is per workspace; pass *cache* so one event does not probe the same target
    name twice.
    """
    cache_key = (str(team_id or ""), reaction)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    result: bool | None
    try:
        bot_client.reactions_add(channel=channel_id, timestamp=target_ts, name=reaction)
    except SlackApiError as exc:
        error_code = _slack_error_code(exc)
        if error_code == "invalid_name":
            result = True
        elif error_code in _IDEMPOTENT_ADD_ERRORS:
            result = False
        else:
            _logger.debug(
                "reaction_name_probe_failed",
                extra={"channel_id": channel_id, "error": error_code or str(exc)},
            )
            result = None
        if cache is not None and result is not None:
            cache[cache_key] = result
        return result
    try:
        bot_client.reactions_remove(channel=channel_id, timestamp=target_ts, name=reaction)
    except SlackApiError as exc:
        _logger.debug(
            "reaction_name_probe_remove_failed",
            extra={"channel_id": channel_id, "error": _slack_error_code(exc) or str(exc)},
        )
    if cache is not None:
        cache[cache_key] = False
    return False


def _remember_native_reaction(
    *,
    team_id: str | None,
    user_id: str | None,
    action: str,
    channel_id: str,
    target_ts: str,
    reaction: str,
) -> None:
    if not team_id or not user_id:
        return
    kind = "reaction_added" if action == "add" else "reaction_removed"
    remember_user_action(
        team_id,
        user_id,
        kind,
        reaction_echo_fingerprint(channel_id, target_ts, reaction),
    )


def apply_reaction_to_target(
    *,
    action: str,
    reaction: str,
    source_user_id: str | None,
    source_workspace_id: int | None,
    source_sync_channel: schemas.SyncChannel | None,
    target_post_meta: schemas.PostMeta,
    target_sync_channel: schemas.SyncChannel,
    target_workspace: schemas.Workspace,
    display_name: str,
    icon_url: str | None,
    posted_from: str,
    author_is_mapped: bool,
    mapped_user_id: str | None = None,
    name_probe_cache: dict[tuple[str, str], bool] | None = None,
    federated_instance_id: str | None = None,
    event_workspace_id: int | None = None,
) -> tuple[ApplyResult, schemas.PostMeta | None]:
    """Apply a reaction add/remove on a target channel. Never writes to the origin.

    User-token add/remove runs first when the target has an ``xoxp``. Bot name
    probe (`_target_reaction_name_is_invalid`) runs only on the Hybrid thread
    path: no target token, or that token hit ``_NO_AUTHORIZE_ERRORS``. Skip the
    probe only when source and target are the same Slack workspace. Federation inbound and
    same-instance cross-workspace still probe — origin having the emoji does not
    mean the target has it. Direct-only never probes. Off does not apply add or remove.
    ``invalid_name`` always skips; it never becomes a thread notice.
    """
    if source_sync_channel is not None and source_sync_channel.channel_id == target_sync_channel.channel_id:
        return "skipped", None

    if not channel_subscribes(target_sync_channel):
        return "skipped", None

    style = reaction_style(target_sync_channel)
    if style == constants.REACTION_STYLE_OFF:
        return "skipped", None

    target_ts = slack_message_ts(target_post_meta.ts)
    channel_id = target_sync_channel.channel_id
    parent_post_id = str(getattr(target_post_meta, "post_id", None) or "")
    bot_client: WebClient | None = None

    def target_bot_client() -> WebClient:
        nonlocal bot_client
        if bot_client is None:
            bot_client = WebClient(token=decrypt_bot_token(target_workspace.bot_token))
        return bot_client

    resolved_user = mapped_user_id or _mapped_user_for_target(
        source_user_id,
        source_workspace_id,
        target_workspace.id,
        target_workspace=target_workspace,
    )
    if resolved_user:
        author_is_mapped = True
    user_token = get_user_token(target_workspace.team_id, resolved_user)

    if user_token and style in (constants.REACTION_STYLE_DIRECT_ONLY, constants.REACTION_STYLE_THREADED_AND_DIRECT):
        user_client = WebClient(token=user_token)
        try:
            if action == "add":
                user_client.reactions_add(channel=channel_id, timestamp=target_ts, name=reaction)
            else:
                user_client.reactions_remove(channel=channel_id, timestamp=target_ts, name=reaction)
            _remember_native_reaction(
                team_id=target_workspace.team_id,
                user_id=resolved_user,
                action=action,
                channel_id=channel_id,
                target_ts=target_ts,
                reaction=reaction,
            )
            if action == "remove":
                ws_id = event_workspace_id if event_workspace_id is not None else target_workspace.id
                actor_user = source_user_id or resolved_user
                if actor_user:
                    delete_notices_for_unreact(
                        parent_post_id=parent_post_id,
                        reaction=reaction,
                        sync_channel=target_sync_channel,
                        event_workspace_id=ws_id,
                        event_user_id=actor_user,
                        client=user_client,
                    )
            return "direct", None
        except SlackApiError as exc:
            error_code = _slack_error_code(exc)
            if action == "remove":
                ws_id = event_workspace_id if event_workspace_id is not None else target_workspace.id
                actor_user = source_user_id or resolved_user
                if actor_user:
                    delete_notices_for_unreact(
                        parent_post_id=parent_post_id,
                        reaction=reaction,
                        sync_channel=target_sync_channel,
                        event_workspace_id=ws_id,
                        event_user_id=actor_user,
                        client=target_bot_client(),
                    )
                return "skipped", None
            if error_code in _IDEMPOTENT_ADD_ERRORS:
                return "direct", None
            if error_code == "invalid_name":
                return "skipped", None
            if error_code in _NO_AUTHORIZE_ERRORS:
                user_token = None
            else:
                _logger.warning(
                    "reaction_direct_failed",
                    extra={"channel_id": channel_id, "error": error_code or str(exc)},
                )
                return "failed", None

    if action != "add":
        ws_id = event_workspace_id if event_workspace_id is not None else (source_workspace_id or target_workspace.id)
        actor_user = source_user_id
        if actor_user and ws_id is not None:
            delete_notices_for_unreact(
                parent_post_id=parent_post_id,
                reaction=reaction,
                sync_channel=target_sync_channel,
                event_workspace_id=ws_id,
                event_user_id=actor_user,
                client=target_bot_client(),
            )
        return "skipped", None

    if style != constants.REACTION_STYLE_THREADED_AND_DIRECT:
        return "skipped", None

    # Same Slack workspace shares the emoji catalog. Cross-workspace — including
    # federation inbound (source_workspace_id is None) — must probe target names.
    skip_probe = source_workspace_id is not None and source_workspace_id == target_workspace.id
    if not skip_probe:
        name_invalid = _target_reaction_name_is_invalid(
            target_bot_client(),
            team_id=getattr(target_workspace, "team_id", None),
            channel_id=channel_id,
            target_ts=target_ts,
            reaction=reaction,
            cache=name_probe_cache,
        )
        if name_invalid is not False:
            return "skipped", None

    notice = _post_threaded_reaction_notice(
        target_client=target_bot_client(),
        sync_channel=target_sync_channel,
        post_meta=target_post_meta,
        reaction=reaction,
        display_name=display_name,
        icon_url=icon_url,
        posted_from=posted_from,
        author_is_mapped=author_is_mapped,
        source_user_id=source_user_id,
        source_workspace_id=source_workspace_id,
        federated_instance_id=federated_instance_id,
    )
    return ("thread", notice) if notice else ("failed", None)


def find_source_sync_channel(
    records: list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]],
    event_channel_id: str,
) -> schemas.SyncChannel | None:
    for _pm, sync_channel, _ws in records:
        if sync_channel.channel_id == event_channel_id:
            return sync_channel
    return None


def update_sync_channel_reactions(
    sync_channel_id: int,
    *,
    style: str | None,
    publishes: bool | None = None,
    subscribes: bool | None = None,
) -> None:
    values = {
        schemas.SyncChannel.reaction_style: style,
    }
    if publishes is not None:
        values[schemas.SyncChannel.publishes] = publishes
    if subscribes is not None:
        values[schemas.SyncChannel.subscribes] = subscribes
    DbManager.update_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.id == sync_channel_id],
        values,
    )
    rows = DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.id == sync_channel_id])
    if rows:
        from helpers.sync_participation import invalidate_channel_memberships

        invalidate_channel_memberships(rows[0].channel_id)
