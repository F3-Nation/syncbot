"""Message sync handlers — new posts, replies, edits, deletes, and files."""

import uuid
from logging import Logger

from slack_sdk.web import WebClient

import constants
import helpers
from db import DbManager, schemas
from db.event_claims import run_claimed
from handlers._common import EventContext
from logger import emit_metric, log_debug, log_error, log_info
from slack import orm


def _is_thread_reply(ts: str | None, thread_ts: str | None) -> bool:
    """True when Slack *thread_ts* is a parent, not this message's own ts."""
    if not ts or not thread_ts:
        return False
    return helpers.slack_message_ts(ts) != helpers.slack_message_ts(thread_ts)


def _build_envelope_people(
    ctx: EventContext,
    user_id: str | None,
    user_name: str | None,
    user_profile_url: str | None,
    workspace: schemas.Workspace | None,
) -> list[dict]:
    """Author plus mentioned users for the source-canonical envelope."""
    people = [
        helpers.build_people_entry(
            person["user_id"],
            name=person.get("user_name"),
            email=person.get("email"),
            avatar_url=person.get("user_profile_url"),
        )
        for person in (ctx.get("mentioned_users") or [])
        if person.get("user_id")
    ]
    if user_id:
        email = next(
            (person.get("email") for person in (ctx.get("mentioned_users") or []) if person.get("user_id") == user_id),
            None,
        )
        if not email and workspace is not None:
            rows = DbManager.find_records(
                schemas.UserDirectory,
                [
                    schemas.UserDirectory.workspace_id == workspace.id,
                    schemas.UserDirectory.slack_user_id == user_id,
                    schemas.UserDirectory.deleted_at.is_(None),
                ],
            )
            email = rows[0].email if rows else None
        people.insert(
            0,
            helpers.build_people_entry(user_id, name=user_name, email=email, avatar_url=user_profile_url),
        )
    return people


def _event_is_bot_post(event: dict) -> bool:
    """True for ``bot_message`` and for ``message_changed`` of a bot post."""
    if event.get("subtype") == "bot_message" or event.get("bot_id"):
        return True
    nested = event.get("message") if isinstance(event.get("message"), dict) else {}
    return bool(nested.get("bot_id") or nested.get("subtype") == "bot_message")


def _parse_event_fields_light(body: dict) -> EventContext:
    """Extract message fields without Slack API calls.

    Layout blocks and text come from the event payload only. Mention profiles
    and ``conversations.replies`` are filled later by
    :func:`_enrich_event_fields_for_sync` after membership and publish gates.
    """
    event: dict = body.get("event", {})
    layout_blocks = helpers.build_content_blocks_for_sync(helpers.get_event_layout_blocks(event))
    event_text = helpers.safe_get(event, "text") or helpers.safe_get(event, "message", "text")
    msg_text = helpers.choose_message_text(event_text, layout_blocks)
    ts = (
        helpers.safe_get(event, "message", "ts")
        or helpers.safe_get(event, "previous_message", "ts")
        or helpers.safe_get(event, "ts")
    )
    raw_thread = helpers.safe_get(event, "thread_ts") or helpers.safe_get(event, "message", "thread_ts")
    return EventContext(
        team_id=helpers.get_team_id_from_body(body),
        channel_id=helpers.safe_get(event, "channel"),
        user_id=(helpers.safe_get(event, "user") or helpers.safe_get(event, "message", "user")),
        msg_text=msg_text,
        mentioned_users=[],
        thread_ts=raw_thread if _is_thread_reply(ts, raw_thread) else None,
        ts=ts,
        event_subtype=helpers.safe_get(event, "subtype"),
        reply_broadcast=helpers.safe_get(event, "subtype") in ("thread_broadcast", "reply_broadcast"),
        content_blocks=layout_blocks,
    )


def _enrich_event_fields_for_sync(body: dict, client: WebClient, ctx: EventContext) -> EventContext:
    """Resolve mentions and bot layout via Slack after publish gates pass."""
    event: dict = body.get("event", {})
    layout_blocks = list(ctx.get("content_blocks") or [])
    if not layout_blocks and _event_is_bot_post(event):
        layout_blocks = helpers.build_content_blocks_for_sync(helpers.fetch_message_layout_blocks(client, event))
        event_text = helpers.safe_get(event, "text") or helpers.safe_get(event, "message", "text")
        ctx["msg_text"] = helpers.choose_message_text(event_text, layout_blocks)
        ctx["content_blocks"] = layout_blocks

    mentioned_users = helpers.parse_mentioned_users(ctx.get("msg_text") or "", client)
    extra_ids = [
        uid
        for uid in helpers.collect_user_ids_from_blocks(layout_blocks)
        if uid not in {u.get("user_id") for u in mentioned_users}
    ]
    if extra_ids:
        mentioned_users.extend(helpers.parse_mentioned_users("".join(f"<@{uid}>" for uid in extra_ids), client))
    ctx["mentioned_users"] = mentioned_users
    return ctx


def _parse_event_fields(body: dict, client: WebClient) -> EventContext:
    """Extract message fields including Slack mention/history enrichment.

    Prefer :func:`_parse_event_fields_light` on the hot path and enrich only
    after membership and publish checks.
    """
    return _enrich_event_fields_for_sync(body, client, _parse_event_fields_light(body))


def _build_file_context(body: dict, client: WebClient, logger: Logger) -> tuple[list[dict], list[dict]]:
    """Process files attached to a message event.

    Returns ``(photo_blocks, direct_files)`` where:

    * *photo_blocks* — Slack Block Kit ``image`` blocks for inline images
      (e.g. GIF picker URLs), ready for ``chat.postMessage``.
    * *direct_files* — files downloaded to ``/tmp`` for direct upload to
      each target channel.
    """
    event = body.get("event", {})
    files = helpers.safe_get(event, "files") or helpers.safe_get(event, "message", "files") or []
    event_subtype = helpers.safe_get(event, "subtype")

    photo_blocks: list[dict] = []
    direct_files: list[dict] = []
    is_edit = event_subtype in ("message_changed", "message_deleted")

    if not is_edit:
        direct_files = helpers.download_slack_files(files, client, logger)

    # Public GIF/image URLs (GIPHY, Slack GIF picker) stay on the message even
    # when a hosted file is attached too. Slack-hosted thumbs are the file.
    attachments = event.get("attachments") or helpers.safe_get(event, "message", "attachments") or []
    seen_urls: set[str] = set()
    for att in attachments:
        img_url = _attachment_image_url(att)
        if not img_url:
            log_info("attachment_no_image_url", att_keys=list(att.keys()), fallback=att.get("fallback"))
            continue
        if img_url in seen_urls or helpers.is_slack_hosted_media_url(img_url):
            continue
        seen_urls.add(img_url)
        name = att.get("fallback") or "attachment.gif"
        photo_blocks.append(orm.ImageBlock(image_url=img_url, alt_text=name).as_form_field())

    return photo_blocks, direct_files


def _attachment_image_url(att: dict) -> str | None:
    """Public image on a Slack attachment, including a GIF picker block."""
    img_url = att.get("image_url") or att.get("thumb_url")
    if img_url:
        return str(img_url)
    for blk in att.get("blocks") or []:
        if isinstance(blk, dict) and blk.get("type") == "image" and blk.get("image_url"):
            return str(blk["image_url"])
    return None


def _leave_unconfigured_channel(client: WebClient, channel_id: str, user_id: str | None, logger: Logger) -> None:
    """Tell the channel SyncBot is leaving, then leave."""
    if not user_id:
        return
    try:
        client.chat_postMessage(
            channel=channel_id,
            text=":wave: Hello! I'm SyncBot. I was added to this Channel, but this Channel "
            "doesn't seem to be part of a Channel Sync. I'm leaving now. Please open the SyncBot Home "
            "tab to Create Sync or Join Sync.",
        )
        client.conversations_leave(channel=channel_id)
    except Exception as e:
        log_error("failed_to_notify_and_leave_unconfigured_channel", channel_id=channel_id, error=str(e))


def _handle_new_post(
    body: dict,
    client: WebClient,
    logger: Logger,
    ctx: EventContext,
    photo_blocks: list[dict],
    direct_files: list[dict] | None = None,
) -> None:
    """Publish a brand-new top-level message through the target pipeline."""
    channel_id = ctx["channel_id"]
    user_id = ctx["user_id"]
    source_records = helpers.get_channel_memberships(channel_id)
    source_sync_channel = helpers.get_origin_sync_channel(channel_id)
    source_workspace = (
        next(
            (workspace for sync_channel, workspace in source_records if sync_channel.id == source_sync_channel.id),
            None,
        )
        if source_sync_channel
        else None
    )
    if not source_sync_channel or not source_workspace:
        helpers.cleanup_temp_files(None, direct_files)
        return

    user_name, user_profile_url = (
        helpers.get_user_info(client, user_id) if user_id else helpers.get_bot_info_from_event(body)
    )
    post_uuid = uuid.uuid4().hex
    source_ts = helpers.safe_get(body, "event", "ts")
    envelope = helpers.build_envelope(
        kind=helpers.KIND_MESSAGE,
        action=helpers.ACTION_CREATE,
        post_id=post_uuid,
        source_channel_id=channel_id,
        source_workspace_id=source_workspace.id,
        source_team_id=ctx.get("team_id"),
        source_sync_channel_id=source_sync_channel.id,
        people=_build_envelope_people(ctx, user_id, user_name, user_profile_url, source_workspace),
        text=ctx.get("msg_text") or "",
        blocks=ctx.get("content_blocks") or [],
        file_refs=direct_files or [],
        images=photo_blocks,
        source_user_id=user_id,
        user_name=user_name,
        user_avatar_url=user_profile_url,
        workspace_name=helpers.resolve_workspace_name(source_workspace),
        source_ts=source_ts,
        reply_broadcast=bool(ctx.get("reply_broadcast")),
    )
    post_list = helpers.build_origin_post_meta_rows(
        source_records,
        channel_id,
        post_uuid,
        source_ts,
        user_id,
        source_workspace.id,
        source_sync_channel=source_sync_channel,
    )
    if not post_list:
        helpers.cleanup_temp_files(None, direct_files)
        return
    try:
        synced = helpers.run_sync_pipeline(
            envelope,
            source_channel_id=channel_id,
            source_client=client,
            source_sync_channel=source_sync_channel,
        )
    finally:
        helpers.cleanup_temp_files(None, direct_files)
    DbManager.create_records(post_list)
    emit_metric("messages_synced", value=len(synced), sync_type="new_post")


def _handle_thread_reply(
    body: dict,
    client: WebClient,
    logger: Logger,
    ctx: EventContext,
    photo_blocks: list[dict],
    direct_files: list[dict] | None = None,
) -> bool | None:
    """Publish a threaded reply through the target pipeline.

    Return ``False`` when the parent PostMeta is not stored yet so the
    claim is released and Slack can retry. Other returns complete the claim.
    """
    channel_id = ctx["channel_id"]
    user_id = ctx["user_id"]
    thread_ts = ctx["thread_ts"]
    post_records = helpers.get_post_records(thread_ts)
    if not post_records:
        helpers.cleanup_temp_files(None, direct_files)
        log_debug(
            "message_not_ready",
            reason="parent_missing",
            channel=channel_id,
            thread_ts=thread_ts,
        )
        return False
    source_rows = helpers.get_publishing_post_records(post_records, channel_id)
    if not source_rows:
        helpers.cleanup_temp_files(None, direct_files)
        return
    parent_meta, source_sync_channel, source_workspace = source_rows[0]
    source_records = [(sync_channel, workspace) for _meta, sync_channel, workspace in source_rows]
    user_name, user_profile_url = (
        helpers.get_user_info(client, user_id) if user_id else helpers.get_bot_info_from_event(body)
    )
    post_uuid = uuid.uuid4().hex
    source_ts = helpers.safe_get(body, "event", "ts")
    envelope = helpers.build_envelope(
        kind=helpers.KIND_MESSAGE,
        action=helpers.ACTION_CREATE,
        post_id=post_uuid,
        source_channel_id=channel_id,
        source_workspace_id=source_workspace.id,
        source_team_id=ctx.get("team_id"),
        source_sync_channel_id=source_sync_channel.id,
        people=_build_envelope_people(ctx, user_id, user_name, user_profile_url, source_workspace),
        text=ctx.get("msg_text") or "",
        blocks=ctx.get("content_blocks") or [],
        file_refs=direct_files or [],
        images=photo_blocks,
        thread_post_id=str(parent_meta.post_id),
        reply_broadcast=bool(ctx.get("reply_broadcast")),
        source_user_id=user_id,
        user_name=user_name,
        user_avatar_url=user_profile_url,
        workspace_name=helpers.resolve_workspace_name(source_workspace),
        source_ts=source_ts,
    )
    post_list = helpers.build_origin_post_meta_rows(
        source_records,
        channel_id,
        post_uuid,
        source_ts,
        user_id,
        source_workspace.id,
        source_sync_channel=source_sync_channel,
    )
    if not post_list:
        helpers.cleanup_temp_files(None, direct_files)
        return
    parent_ts_by_channel = {
        sync_channel.channel_id: helpers.slack_message_ts(post_meta.ts)
        for post_meta, sync_channel, _workspace in post_records
    }
    try:
        synced = helpers.run_sync_pipeline(
            envelope,
            source_channel_id=channel_id,
            source_client=client,
            source_sync_channel=source_sync_channel,
            thread_parent_ts_by_channel=parent_ts_by_channel,
        )
    finally:
        helpers.cleanup_temp_files(None, direct_files)
    DbManager.create_records(post_list)
    emit_metric("messages_synced", value=len(synced), sync_type="thread_reply")
    log_debug(
        "thread_reply_applied",
        post_id=post_uuid,
        thread_post_id=str(parent_meta.post_id),
        source_channel=channel_id,
        copied=len(synced),
        parent_channels=sorted(parent_ts_by_channel),
    )


def _handle_message_edit(
    client: WebClient,
    logger: Logger,
    ctx: EventContext,
    photo_blocks: list[dict],
) -> bool | None:
    """Publish an edited message through the target pipeline.

    Return ``False`` when PostMeta is not stored yet so Slack can retry.
    """
    channel_id = ctx["channel_id"]
    ts = ctx["ts"]
    post_records = helpers.get_post_records(ts)
    if not post_records:
        return False
    source_rows = helpers.get_publishing_post_records(post_records, channel_id)
    if not source_rows:
        return
    post_meta, source_sync_channel, workspace = source_rows[0]
    user_id = ctx.get("user_id")
    envelope = helpers.build_envelope(
        kind=helpers.KIND_MESSAGE,
        action=helpers.ACTION_EDIT,
        post_id=str(post_meta.post_id),
        source_channel_id=channel_id,
        source_workspace_id=workspace.id,
        source_team_id=ctx.get("team_id"),
        source_sync_channel_id=source_sync_channel.id,
        people=_build_envelope_people(ctx, user_id, None, None, workspace),
        text=ctx.get("msg_text") or "",
        blocks=ctx.get("content_blocks") or [],
        images=photo_blocks,
        source_user_id=user_id,
        workspace_name=helpers.resolve_workspace_name(workspace),
        source_ts=ts,
    )
    helpers.run_sync_pipeline(
        envelope,
        source_channel_id=channel_id,
        source_client=client,
        source_sync_channel=source_sync_channel,
    )


def _handle_message_delete(
    ctx: EventContext,
    logger: Logger,
) -> bool | None:
    """Publish a deleted message through the target pipeline.

    Return ``False`` when PostMeta is not stored yet so Slack can retry.
    """
    channel_id = ctx["channel_id"]
    ts = ctx["ts"]
    post_records = helpers.get_post_records(ts)
    if not post_records:
        return False
    source_rows = helpers.get_publishing_post_records(post_records, channel_id)
    if not source_rows:
        return
    post_meta, source_sync_channel, workspace = source_rows[0]
    envelope = helpers.build_envelope(
        kind=helpers.KIND_MESSAGE,
        action=helpers.ACTION_DELETE,
        post_id=str(post_meta.post_id),
        source_channel_id=channel_id,
        source_workspace_id=workspace.id,
        source_team_id=ctx.get("team_id"),
        source_sync_channel_id=source_sync_channel.id,
        source_user_id=ctx.get("user_id"),
        workspace_name=helpers.resolve_workspace_name(workspace),
        source_ts=ts,
    )
    helpers.run_sync_pipeline(
        envelope,
        source_channel_id=channel_id,
        source_sync_channel=source_sync_channel,
    )


def _is_own_bot_message(body: dict, client: WebClient, context: dict) -> bool:
    """Return *True* if the event was generated by SyncBot itself.

    Compares the ``bot_id`` in the event payload against SyncBot's own
    bot ID.  This replaces the old blanket ``bot_message`` filter so
    that messages from *other* bots are synced normally while SyncBot's
    own re-posts are still ignored (preventing infinite loops).
    """
    event = body.get("event", {})
    event_bot_id = (
        event.get("bot_id")
        or helpers.safe_get(event, "message", "bot_id")
        or helpers.safe_get(event, "previous_message", "bot_id")
    )
    if not event_bot_id:
        return False

    own_bot_id = helpers.get_own_bot_id(client, context)
    return event_bot_id == own_bot_id


def _try_handle_reaction_notice_delete(
    body: dict,
    client: WebClient,
    context: dict,
    ctx: EventContext,
) -> bool:
    """Tombstone a user-deleted Hybrid reaction notice on this channel only."""
    if ctx.get("event_subtype") != "message_deleted":
        return False

    channel_id = ctx.get("channel_id")
    ts = ctx.get("ts")
    if not channel_id or not ts:
        return False

    team_id = helpers.get_team_id_from_body(body)
    workspace = helpers.get_workspace_record(team_id, body, context, client) if team_id else None
    if not workspace:
        return False

    sync_channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.workspace_id == workspace.id, schemas.SyncChannel.channel_id == channel_id],
    )
    if not sync_channels:
        return False

    from helpers.reaction_notice import get_post_meta_by_channel_ts, tombstone_reaction_notice_locally

    notice = get_post_meta_by_channel_ts(sync_channels[0].id, ts)
    if (
        not notice
        or getattr(notice, "kind", constants.POST_META_KIND_MESSAGE) != constants.POST_META_KIND_REACTION_NOTICE
    ):
        return False

    bot_client = WebClient(token=helpers.get_bot_token(workspace))
    tombstone_reaction_notice_locally(
        notice=notice,
        sync_channel=sync_channels[0],
        client=bot_client,
    )
    return True


def respond_to_message_event(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Dispatch incoming message events to the appropriate sub-handler."""
    ctx = _parse_event_fields_light(body)
    event_type = helpers.safe_get(body, "event", "type")
    event_subtype = ctx["event_subtype"]

    if event_type != "message":
        return

    event_files = helpers.safe_get(body, "event", "files") or helpers.safe_get(body, "event", "message", "files") or []
    if not isinstance(event_files, list):
        event_files = []
    _SYNCED_SUBTYPES = frozenset(
        {
            None,
            "bot_message",
            "file_share",
            "thread_broadcast",
            "reply_broadcast",
            "me_message",
            "message_changed",
            "message_deleted",
        }
    )

    def _sync_message() -> bool | None:
        if event_subtype == "message_deleted" and _try_handle_reaction_notice_delete(body, client, context, ctx):
            return

        # Skip messages from SyncBot itself to prevent infinite sync loops.
        # Messages from OTHER bots are synced normally.
        if _is_own_bot_message(body, client, context):
            file_ids = helpers.file_ids_from_message_event(body)
            recorded = helpers.complete_copy_ts_from_pending_share(
                ctx.get("team_id"),
                ctx.get("channel_id"),
                ctx.get("ts"),
                file_ids,
            )
            if recorded is False:
                return False
            if recorded:
                log_debug(
                    "apply_create",
                    source="own_bot_share",
                    channel_id=ctx.get("channel_id"),
                    ts=ctx.get("ts"),
                )
                return
            log_debug(
                "message_skip",
                reason="own_bot",
                channel=ctx.get("channel_id"),
                subtype=event_subtype,
                ts=ctx.get("ts"),
            )
            return

        if event_subtype not in _SYNCED_SUBTYPES:
            log_info(
                "unhandled_message_subtype", subtype=event_subtype, channel=helpers.safe_get(body, "event", "channel")
            )
            return

        channel_id = ctx.get("channel_id")
        user_id = ctx.get("user_id")
        ts = ctx.get("ts")
        team_id = ctx.get("team_id")
        if not helpers.channel_has_membership(channel_id):
            _leave_unconfigured_channel(client, channel_id, user_id, logger)
            return

        is_reply = bool(ctx.get("thread_ts"))
        event = body.get("event") if isinstance(body.get("event"), dict) else {}
        is_new_file_share = helpers.event_is_new_file_share(event)
        this_message_files = helpers.event_keeps_hosted_files(event, is_reply=is_reply, text=ctx.get("msg_text") or "")
        hosted_media = helpers.event_has_slack_hosted_media_blocks(event)
        trace = {
            "channel": channel_id,
            "subtype": event_subtype,
            "ts": ts,
            "thread_ts": ctx.get("thread_ts"),
            "file_count": len(event_files),
            "upload": event.get("upload"),
            "new_file_share": is_new_file_share,
            "parent_user_id": helpers.safe_get(body, "event", "parent_user_id"),
        }
        if (
            not event_subtype
            and not is_reply
            and not helpers.event_has_downloadable_hosted_file(event)
            and (event_files or hosted_media)
        ):
            log_debug("message_skip", reason="pending_file_share", **trace)
            return

        if team_id and user_id:
            if this_message_files and any(
                helpers.has_user_action_echo(team_id, user_id, "file", file_id)
                for file_id in helpers.file_ids_from_message_event(body)
            ):
                echo_file_ids = helpers.file_ids_from_message_event(body)
                recorded = helpers.complete_copy_ts_from_pending_share(
                    team_id,
                    channel_id,
                    ts,
                    echo_file_ids,
                )
                if recorded is False:
                    return False
                if recorded:
                    log_debug(
                        "apply_create",
                        source="file_echo_share",
                        channel_id=channel_id,
                        ts=ts,
                    )
                    return
                log_debug("message_skip", reason="file_echo", **trace)
                return
            if ts:
                fingerprint = f"{channel_id}:{helpers.slack_message_ts(ts)}"
                if helpers.has_user_action_echo(team_id, user_id, "message", fingerprint):
                    log_debug("message_skip", reason="message_echo", **trace)
                    return
        is_create = event_subtype in (
            None,
            "bot_message",
            "file_share",
            "thread_broadcast",
            "reply_broadcast",
            "me_message",
        )
        if is_create and helpers.post_meta_exists_for_channel_ts(channel_id, ts):
            log_debug("message_skip", reason="copy_exists", **trace)
            return
        if not helpers.origin_publishes_anywhere(channel_id):
            log_debug("message_skip", reason="not_publishing", **trace)
            return

        has_targets = bool(helpers.iter_publish_targets(channel_id))
        file_body = body
        if event_files and not this_message_files:
            event = dict(body.get("event") or {})
            event.pop("files", None)
            nested = event.get("message")
            if isinstance(nested, dict):
                event["message"] = {**nested, "files": []}
            file_body = {**body, "event": event}
        if has_targets:
            _enrich_event_fields_for_sync(file_body, client, ctx)
            photo_blocks, direct_files = _build_file_context(file_body, client, logger)
        else:
            # Origin PostMeta still needed for later join/thread/reaction; skip Slack.
            photo_blocks, direct_files = [], []

        has_files = bool(photo_blocks or direct_files)
        text_blank = not (ctx.get("msg_text") or "").strip()
        download_files = (
            helpers.safe_get(file_body, "event", "files")
            or helpers.safe_get(file_body, "event", "message", "files")
            or []
        )
        if not isinstance(download_files, list):
            download_files = []
        expected_files = [
            item for item in download_files if isinstance(item, dict) and helpers.hosted_file_fetch_url(item)
        ]
        file_missing = bool(
            is_create
            and has_targets
            and (
                (expected_files and len(direct_files) < len(expected_files))
                or (this_message_files and not has_files)
                or (hosted_media and is_new_file_share and not has_files)
            )
        )
        if file_missing:
            log_error(
                "file_share_failed",
                reason="download_empty",
                channel=channel_id,
                ts=ts,
                thread_ts=ctx.get("thread_ts"),
                file_count=len(event_files),
            )
            helpers.notify_source_user_error(
                source_client=client,
                source_user_id=user_id,
                summary=":warning: SyncBot could not copy your file to the other Channels.",
                details={
                    "event": "file_share_failed",
                    "reason": "download_empty",
                    "channel": channel_id,
                    "ts": ts,
                },
            )
            log_debug("message_skip", reason="empty_file_share", has_files=has_files, **trace)
            return
        if is_create and text_blank and not has_files and has_targets:
            log_debug("message_skip", reason="empty_file_share", has_files=has_files, **trace)
            return
        if is_create:
            if not ctx["thread_ts"]:
                _handle_new_post(body, client, logger, ctx, photo_blocks, direct_files)
                return
            return _handle_thread_reply(body, client, logger, ctx, photo_blocks, direct_files)
        if event_subtype == "message_changed":
            if has_targets:
                return _handle_message_edit(client, logger, ctx, photo_blocks)
            return
        if event_subtype == "message_deleted":
            return _handle_message_delete(ctx, logger)

    run_claimed(body, _sync_message)
