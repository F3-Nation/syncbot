"""Push a sync envelope to a federated peer."""

from __future__ import annotations

import logging
from typing import Any

from db import schemas
from federation.core import (
    build_delete_payload,
    build_edit_payload,
    build_message_payload,
    build_reaction_payload,
    push_delete,
    push_edit,
    push_message,
    push_reaction,
)
from helpers.envelope import (
    ACTION_ADD,
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_EDIT,
    ACTION_REMOVE,
    KIND_MESSAGE,
    KIND_REACTION,
)

_logger = logging.getLogger(__name__)


def federation_image_payloads(images: list[dict] | None) -> list[dict]:
    """Wire-format images for federation (``url`` / ``alt_text``).

    Same-instance envelopes carry Block Kit ``image_url`` blocks. Inbound
    federation expects ``url``. Accept either so GIFs survive both directions.
    """
    payloads: list[dict] = []
    for img in images or []:
        url = img.get("url") or img.get("image_url") or ""
        if not url:
            continue
        payloads.append({"url": url, "alt_text": img.get("alt_text") or "Shared image"})
    return payloads


def deliver_remote(envelope: dict[str, Any], fed_ws: schemas.FederatedWorkspace, channel_id: str) -> dict | None:
    """Push the envelope to a federated peer using today's thin HTTP payloads."""
    kind = envelope.get("kind")
    action = envelope.get("action")
    post_id = envelope.get("post_id")

    if kind == KIND_MESSAGE and action == ACTION_CREATE:
        payload = build_message_payload(
            sync_id=envelope.get("sync_id") or 0,
            post_id=post_id,
            channel_id=channel_id,
            user_name=envelope.get("user_name"),
            user_avatar_url=envelope.get("user_avatar_url"),
            workspace_name=envelope.get("workspace_name"),
            text=envelope.get("text") or "",
            images=federation_image_payloads(envelope.get("images")),
            timestamp=envelope.get("source_ts"),
            user_id=envelope.get("source_user_id"),
            reply_broadcast=bool(envelope.get("reply_broadcast")),
            thread_post_id=envelope.get("thread_post_id"),
        )
        return push_message(fed_ws, payload)

    if kind == KIND_MESSAGE and action == ACTION_EDIT:
        payload = build_edit_payload(
            post_id=str(post_id),
            channel_id=channel_id,
            text=envelope.get("text") or "",
            timestamp=envelope.get("target_ts") or envelope.get("source_ts"),
            images=federation_image_payloads(envelope.get("images")),
        )
        return push_edit(fed_ws, payload)

    if kind == KIND_MESSAGE and action == ACTION_DELETE:
        payload = build_delete_payload(
            post_id=str(post_id),
            channel_id=channel_id,
            timestamp=envelope.get("target_ts") or envelope.get("source_ts"),
        )
        return push_delete(fed_ws, payload)

    if kind == KIND_REACTION and action in (ACTION_ADD, ACTION_REMOVE):
        payload = build_reaction_payload(
            post_id=str(post_id),
            channel_id=channel_id,
            reaction=envelope.get("reaction"),
            action=action,
            user_name=envelope.get("user_name") or envelope.get("source_user_id") or "Someone",
            user_avatar_url=envelope.get("user_avatar_url"),
            workspace_name=envelope.get("workspace_name"),
            timestamp=envelope.get("target_ts") or envelope.get("source_ts"),
            user_id=envelope.get("source_user_id"),
        )
        return push_reaction(fed_ws, payload)

    _logger.warning("deliver_remote_unsupported", extra={"kind": kind, "action": action})
    return None
