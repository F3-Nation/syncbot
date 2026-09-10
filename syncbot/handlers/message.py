"""Message sync handlers — new posts, replies, edits, deletes, and files."""

import logging
import uuid
from logging import Logger

from slack_sdk.web import WebClient

import constants
import helpers
from db import DbManager, schemas
from db.event_claims import run_claimed
from handlers._common import EventContext
from logger import emit_metric
from slack import orm

_logger = logging.getLogger(__name__)


def _build_envelope_people(
    ctx: EventContext,
    user_id: str | None,
    user_name: str | None,
    user_profile_url: str | None,
) -> list[dict]:
    """Author plus mentioned users for the source-canonical envelope."""
    people = [
        helpers.people_entry(
            person["user_id"],
            name=person.get("user_name"),
            email=person.get("email"),
            avatar_url=person.get("user_profile_url"),
        )
        for person in (ctx.get("mentioned_users") or [])
        if person.get("user_id")
    ]
    if user_id:
        people.insert(0, helpers.people_entry(user_id, name=user_name, avatar_url=user_profile_url))
    return people


def _event_is_bot_post(event: dict) -> bool:
    """True for ``bot_message`` and for ``message_changed`` of a bot post."""
    if event.get("subtype") == "bot_message" or event.get("bot_id"):
        return True
    nested = event.get("message") if isinstance(event.get("message"), dict) else {}
    return bool(nested.get("bot_id") or nested.get("subtype") == "bot_message")


def _parse_event_fields(body: dict, client: WebClient) -> EventContext:
    """Extract the common fields every message handler needs."""
    event: dict = body.get("event", {})
    layout_blocks = helpers.content_blocks_for_sync(helpers.event_layout_blocks(event))
    if not layout_blocks and _event_is_bot_post(event):
        layout_blocks = helpers.content_blocks_for_sync(helpers.fetch_message_layout_blocks(client, event))
    event_text = helpers.safe_get(event, "text") or helpers.safe_get(event, "message", "text")
    msg_text = helpers.choose_message_text(event_text, layout_blocks)
    mentioned_users = helpers.parse_mentioned_users(msg_text, client)
    extra_ids = [
        uid
        for uid in helpers.collect_user_ids_from_blocks(layout_blocks)
        if uid not in {u.get("user_id") for u in mentioned_users}
    ]
    if extra_ids:
        mentioned_users.extend(helpers.parse_mentioned_users("".join(f"<@{uid}>" for uid in extra_ids), client))

    return EventContext(
        team_id=helpers.safe_get(body, "team_id"),
        channel_id=helpers.safe_get(event, "channel"),
        user_id=(helpers.safe_get(event, "user") or helpers.safe_get(event, "message", "user")),
        msg_text=msg_text,
        mentioned_users=mentioned_users,
        thread_ts=helpers.safe_get(event, "thread_ts"),
        ts=(
            helpers.safe_get(event, "message", "ts")
            or helpers.safe_get(event, "previous_message", "ts")
            or helpers.safe_get(event, "ts")
        ),
        event_subtype=helpers.safe_get(event, "subtype"),
        reply_broadcast=helpers.safe_get(event, "subtype") in ("thread_broadcast", "reply_broadcast"),
        content_blocks=layout_blocks,
    )


def _build_file_context(body: dict, client: WebClient, logger: Logger) -> tuple[list[dict], list[dict]]:
    """Process files attached to a message event.

    Returns ``(photo_blocks, direct_files)`` where:

    * *photo_blocks* — Slack Block Kit ``image`` blocks for inline images
      (e.g. GIF picker URLs), ready for ``chat.postMessage``.
    * *direct_files* — files downloaded to ``/tmp`` for direct upload to
      each target channel via ``files_upload_v2``.
    """
    event = body.get("event", {})
    files = (helpers.safe_get(event, "files") or helpers.safe_get(event, "message", "files") or [])[:20]
    event_subtype = helpers.safe_get(event, "subtype")

    photo_blocks: list[dict] = []
    direct_files: list[dict] = []
    is_edit = event_subtype in ("message_changed", "message_deleted")

    if not is_edit:
        direct_files = helpers.download_slack_files(files, client, logger)

    # Public GIF/image URLs (GIPHY, Slack GIF picker). Include edits so federation
    # thread/edit payloads can carry the same image blocks as new posts.
    if not files:
        attachments = event.get("attachments") or helpers.safe_get(event, "message", "attachments") or []
        for att in attachments:
            img_url = att.get("image_url") or att.get("thumb_url")

            # Slack's built-in GIF picker nests the image inside blocks
            if not img_url:
                for blk in att.get("blocks") or []:
                    if blk.get("type") == "image" and blk.get("image_url"):
                        img_url = blk["image_url"]
                        break

            # Also check top-level event blocks for image blocks
            if not img_url:
                for blk in event.get("blocks") or []:
                    if blk.get("type") == "image" and blk.get("image_url"):
                        img_url = blk["image_url"]
                        break

            if not img_url:
                _logger.info(
                    "attachment_no_image_url", extra={"att_keys": list(att.keys()), "fallback": att.get("fallback")}
                )
                continue

            name = att.get("fallback") or "attachment.gif"
            photo_blocks.append(orm.ImageBlock(image_url=img_url, alt_text=name).as_form_field())

    return photo_blocks, direct_files


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
        logger.error(f"Failed to notify and leave unconfigured channel {channel_id}: {e}")


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
    source_records = helpers.find_channel_memberships(channel_id)
    source_sync_channel = helpers.find_origin_sync_channel(channel_id)
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
        people=_build_envelope_people(ctx, user_id, user_name, user_profile_url),
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
    post_list = helpers.origin_post_meta_rows(
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
        post_list.extend(
            helpers.run_sync_pipeline(
                envelope,
                source_channel_id=channel_id,
                source_client=client,
                source_sync_channel=source_sync_channel,
                origin_ts=source_ts,
            )
        )
    finally:
        helpers.cleanup_temp_files(None, direct_files)
    DbManager.create_records(post_list)
    emit_metric("messages_synced", value=max(0, len(post_list) - 1), sync_type="new_post")


def _handle_thread_reply(
    body: dict,
    client: WebClient,
    logger: Logger,
    ctx: EventContext,
    photo_blocks: list[dict],
    direct_files: list[dict] | None = None,
) -> None:
    """Publish a threaded reply through the target pipeline."""
    channel_id = ctx["channel_id"]
    user_id = ctx["user_id"]
    thread_ts = ctx["thread_ts"]
    post_records = helpers.get_post_records(thread_ts)
    if not post_records:
        helpers.cleanup_temp_files(None, direct_files)
        return
    source_rows = helpers.find_publishing_post_records(post_records, channel_id)
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
        people=_build_envelope_people(ctx, user_id, user_name, user_profile_url),
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
    post_list = helpers.origin_post_meta_rows(
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
        post_list.extend(
            helpers.run_sync_pipeline(
                envelope,
                source_channel_id=channel_id,
                source_client=client,
                source_sync_channel=source_sync_channel,
                origin_ts=source_ts,
                thread_parent_ts_by_channel=parent_ts_by_channel,
            )
        )
    finally:
        helpers.cleanup_temp_files(None, direct_files)
    DbManager.create_records(post_list)
    emit_metric("messages_synced", value=max(0, len(post_list) - 1), sync_type="thread_reply")


def _handle_message_edit(
    client: WebClient,
    logger: Logger,
    ctx: EventContext,
    photo_blocks: list[dict],
) -> None:
    """Publish an edited message through the target pipeline."""
    channel_id = ctx["channel_id"]
    ts = ctx["ts"]
    post_records = helpers.get_post_records(ts)
    if not post_records:
        return
    source_rows = helpers.find_publishing_post_records(post_records, channel_id)
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
        people=_build_envelope_people(ctx, user_id, None, None),
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
        origin_ts=ts,
    )


def _handle_message_delete(
    ctx: EventContext,
    logger: Logger,
) -> None:
    """Publish a deleted message through the target pipeline."""
    channel_id = ctx["channel_id"]
    ts = ctx["ts"]
    post_records = helpers.get_post_records(ts)
    if not post_records:
        return
    source_rows = helpers.find_publishing_post_records(post_records, channel_id)
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
        origin_ts=ts,
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

    team_id = helpers.safe_get(body, "team_id") or helpers.safe_get(body, "team", "id")
    workspace = helpers.get_workspace_record(team_id, body, context, client) if team_id else None
    if not workspace:
        return False

    sync_channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.workspace_id == workspace.id, schemas.SyncChannel.channel_id == channel_id],
    )
    if not sync_channels:
        return False

    from helpers.reaction_notice import find_post_meta_by_channel_ts, tombstone_reaction_notice_locally

    notice = find_post_meta_by_channel_ts(sync_channels[0].id, ts)
    if (
        not notice
        or getattr(notice, "kind", constants.POST_META_KIND_MESSAGE) != constants.POST_META_KIND_REACTION_NOTICE
    ):
        return False

    bot_client = WebClient(token=helpers.decrypt_bot_token(workspace.bot_token))
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
    ctx = _parse_event_fields(body, client)
    event_type = helpers.safe_get(body, "event", "type")
    event_subtype = ctx["event_subtype"]

    if event_type != "message":
        return

    if event_subtype == "message_deleted" and _try_handle_reaction_notice_delete(body, client, context, ctx):
        return

    # Skip messages from SyncBot itself to prevent infinite sync loops.
    # Messages from OTHER bots are synced normally.
    if _is_own_bot_message(body, client, context):
        return

    # Slack sends a plain message event and then a file_share for the same post; process only file_share
    # so we do not sync twice (and avoid downloading files twice). thread_broadcast carries files on
    # the same event — do not wait for a second file_share.
    event_has_files = bool(
        helpers.safe_get(body, "event", "files") or helpers.safe_get(body, "event", "message", "files")
    )
    if not event_subtype and event_has_files:
        _logger.debug(
            "skip_message_pending_file_share",
            extra={"channel": helpers.safe_get(body, "event", "channel")},
        )
        return

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
    if event_subtype not in _SYNCED_SUBTYPES:
        _logger.info(
            "unhandled_message_subtype",
            extra={"subtype": event_subtype, "channel": helpers.safe_get(body, "event", "channel")},
        )
        return

    def _sync_message() -> None:
        channel_id = ctx.get("channel_id")
        user_id = ctx.get("user_id")
        ts = ctx.get("ts")
        team_id = ctx.get("team_id")
        if not helpers.channel_has_membership(channel_id):
            _leave_unconfigured_channel(client, channel_id, user_id, logger)
            return
        if team_id and user_id and ts:
            fingerprint = f"{channel_id}:{helpers.slack_message_ts(ts)}"
            if helpers.take_user_action_echo(team_id, user_id, "message", fingerprint):
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
            return
        if not helpers.origin_publishes_anywhere(channel_id):
            return
        photo_blocks, direct_files = _build_file_context(body, client, logger)
        has_files = bool(photo_blocks or direct_files)
        if is_create and (event_subtype != "file_share" or ctx["msg_text"] != "" or has_files):
            if not ctx["thread_ts"]:
                _handle_new_post(body, client, logger, ctx, photo_blocks, direct_files)
            else:
                _handle_thread_reply(body, client, logger, ctx, photo_blocks, direct_files)
        elif event_subtype == "message_changed":
            _handle_message_edit(client, logger, ctx, photo_blocks)
        elif event_subtype == "message_deleted":
            _handle_message_delete(ctx, logger)

    run_claimed(body, _sync_message)
