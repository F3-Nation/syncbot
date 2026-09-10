"""Hybrid reaction notice identity, actor matching, and delete helpers."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import constants
from db import DbManager, schemas
from helpers.slack_api import slack_error_code
from helpers.user_action_echo import slack_message_ts

_logger = logging.getLogger(__name__)
_slack_error_code = slack_error_code


def actor_key_for_notice(
    source_workspace_id: int | None,
    *,
    federated_instance_id: str | None = None,
) -> str:
    if source_workspace_id is not None:
        return str(source_workspace_id)
    if federated_instance_id:
        return f"fed:{federated_instance_id}"
    return "fed:unknown"


def reaction_notice_post_id(
    *,
    parent_post_id: str,
    reaction: str,
    source_user_id: str,
    source_workspace_id: int | None,
    federated_instance_id: str | None = None,
) -> str:
    """Shared ``post_id`` for every target copy of the same logical Hybrid reaction."""
    actor_key = actor_key_for_notice(source_workspace_id, federated_instance_id=federated_instance_id)
    payload = f"{parent_post_id}\0{reaction}\0{actor_key}\0{source_user_id}"
    digest = hashlib.sha256(payload.encode()).hexdigest()[:32]
    return f"rxn-{digest}"


def equivalent_actor_pairs(event_workspace_id: int, event_user_id: str) -> set[tuple[int | None, str]]:
    """Actor pairs that should match stored notice ``(source_workspace_id, source_user_id)``."""
    pairs: set[tuple[int | None, str]] = {(event_workspace_id, event_user_id)}

    forward = DbManager.find_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.source_workspace_id == event_workspace_id,
            schemas.UserMapping.source_user_id == event_user_id,
            schemas.UserMapping.target_user_id.isnot(None),
            schemas.UserMapping.map_method != "none",
        ],
    )
    for mapping in forward:
        pairs.add((mapping.target_workspace_id, mapping.target_user_id))

    reverse = DbManager.find_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.target_workspace_id == event_workspace_id,
            schemas.UserMapping.target_user_id == event_user_id,
            schemas.UserMapping.map_method != "none",
        ],
    )
    for mapping in reverse:
        pairs.add((mapping.source_workspace_id, mapping.source_user_id))

    return pairs


def chat_delete_notice(client: WebClient, channel_id: str, ts: float | str) -> None:
    """Delete a notice Slack message; ``message_not_found`` is success."""
    try:
        client.chat_delete(channel=channel_id, ts=slack_message_ts(ts))
    except SlackApiError as exc:
        if _slack_error_code(exc) == "message_not_found":
            return
        raise


def _hard_delete_post_meta_rows(rows: Iterable[schemas.PostMeta]) -> None:
    for row in rows:
        DbManager.delete_records(schemas.PostMeta, [schemas.PostMeta.id == row.id])


def _child_notices_on_channel(parent_post_id: str, sync_channel_id: int) -> list[schemas.PostMeta]:
    return DbManager.find_records(
        schemas.PostMeta,
        [
            schemas.PostMeta.sync_channel_id == sync_channel_id,
            schemas.PostMeta.kind == constants.POST_META_KIND_REACTION_NOTICE,
            schemas.PostMeta.parent_post_id == parent_post_id,
        ],
    )


def _delete_notice_subtree(
    notice: schemas.PostMeta,
    *,
    sync_channel: schemas.SyncChannel,
    client: WebClient,
    depth: int = 0,
) -> None:
    if depth >= constants.NOTICE_TREE_MAX_DEPTH:
        _logger.warning(
            "reaction_notice_delete_depth_cap",
            extra={"post_id": notice.post_id, "depth": depth},
        )
        return

    children = _child_notices_on_channel(notice.post_id, sync_channel.id)
    for child in children:
        _delete_notice_subtree(child, sync_channel=sync_channel, client=client, depth=depth + 1)

    chat_delete_notice(client, sync_channel.channel_id, notice.ts)
    _hard_delete_post_meta_rows([notice])


def find_notices_for_unreact(
    *,
    parent_post_id: str,
    reaction: str,
    sync_channel_id: int,
    actor_pairs: set[tuple[int | None, str]],
) -> list[schemas.PostMeta]:
    rows = DbManager.find_records(
        schemas.PostMeta,
        [
            schemas.PostMeta.sync_channel_id == sync_channel_id,
            schemas.PostMeta.kind == constants.POST_META_KIND_REACTION_NOTICE,
            schemas.PostMeta.parent_post_id == parent_post_id,
            schemas.PostMeta.reaction == reaction,
        ],
    )
    actor_user_ids = {uid for _ws, uid in actor_pairs if uid}
    matched: list[schemas.PostMeta] = []
    for row in rows:
        if not row.source_user_id:
            continue
        pair = (row.source_workspace_id, row.source_user_id)
        if pair in actor_pairs:
            matched.append(row)
            continue
        # Federation inbound stores source_workspace_id=None; match on user id.
        if row.source_workspace_id is None and row.source_user_id in actor_user_ids:
            matched.append(row)
    return matched


def delete_notices_for_unreact(
    *,
    parent_post_id: str,
    reaction: str,
    sync_channel: schemas.SyncChannel,
    event_workspace_id: int,
    event_user_id: str,
    client: WebClient,
) -> None:
    """Delete matching Hybrid notices on one target channel (children first)."""
    actor_pairs = equivalent_actor_pairs(event_workspace_id, event_user_id)
    notices = find_notices_for_unreact(
        parent_post_id=parent_post_id,
        reaction=reaction,
        sync_channel_id=sync_channel.id,
        actor_pairs=actor_pairs,
    )
    if notices:
        for notice in notices:
            _delete_notice_subtree(notice, sync_channel=sync_channel, client=client)
        return

    style = (getattr(sync_channel, "reaction_style", None) or "").strip()
    if style in (constants.REACTION_STYLE_DIRECT_ONLY, constants.REACTION_STYLE_OFF):
        return
    if not style:
        return

    _delete_leftover_thread_notices(
        parent_post_id=parent_post_id,
        reaction=reaction,
        sync_channel=sync_channel,
        client=client,
    )


def _looks_like_hybrid_notice_text(text: str, reaction: str) -> bool:
    """True for the Hybrid notice template, not a human mention of the emoji."""
    return f"reacted with :{reaction}:".lower() in (text or "").lower()


def _delete_leftover_thread_notices(
    *,
    parent_post_id: str,
    reaction: str,
    sync_channel: schemas.SyncChannel,
    client: WebClient,
) -> None:
    """Best-effort cleanup for pre-1.4.1 uuid notices when no new-style rows exist.

    Only deletes bot-posted thread messages that match the Hybrid notice
    template. A human reply that merely mentions the emoji is left alone.
    """
    parent_rows = DbManager.find_records(
        schemas.PostMeta,
        [
            schemas.PostMeta.post_id == parent_post_id,
            schemas.PostMeta.sync_channel_id == sync_channel.id,
        ],
    )
    if not parent_rows:
        return
    parent_ts = slack_message_ts(parent_rows[0].ts)
    channel_id = sync_channel.channel_id
    try:
        resp = client.conversations_replies(channel=channel_id, ts=parent_ts, limit=200)
    except SlackApiError as exc:
        _logger.debug(
            "leftover_notice_thread_scan_failed",
            extra={"channel_id": channel_id, "error": _slack_error_code(exc) or str(exc)},
        )
        return

    messages = resp.get("messages") or []
    for msg in messages[1:]:
        if not msg.get("bot_id"):
            continue
        if not _looks_like_hybrid_notice_text(msg.get("text") or "", reaction):
            continue
        ts = msg.get("ts")
        if not ts:
            continue
        row = DbManager.find_records(
            schemas.PostMeta,
            [
                schemas.PostMeta.sync_channel_id == sync_channel.id,
                schemas.PostMeta.ts == float(ts),
            ],
        )
        if (
            row
            and getattr(row[0], "kind", constants.POST_META_KIND_MESSAGE) == constants.POST_META_KIND_REACTION_NOTICE
        ):
            continue
        chat_delete_notice(client, channel_id, ts)


def find_post_meta_by_channel_ts(sync_channel_id: int, msg_ts: str | float) -> schemas.PostMeta | None:
    rows = DbManager.find_records(
        schemas.PostMeta,
        [
            schemas.PostMeta.sync_channel_id == sync_channel_id,
            schemas.PostMeta.ts == float(msg_ts),
        ],
    )
    return rows[0] if rows else None


def tombstone_reaction_notice_locally(
    *,
    notice: schemas.PostMeta,
    sync_channel: schemas.SyncChannel,
    client: WebClient,
) -> None:
    """Target user deleted a Hybrid notice — local tombstone only (no origin unreact)."""
    children = _child_notices_on_channel(notice.post_id, sync_channel.id)
    for child in children:
        chat_delete_notice(client, sync_channel.channel_id, child.ts)
        _hard_delete_post_meta_rows([child])
    _hard_delete_post_meta_rows([notice])
