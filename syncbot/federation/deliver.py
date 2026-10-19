"""Push a sync envelope (and file bytes) to a federated peer."""

from __future__ import annotations

import math
import os
from typing import Any

from slack_sdk import WebClient

from db import DbManager, schemas
from federation.core import (
    push_delete,
    push_edit,
    push_file_offer,
    push_file_part,
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
from helpers.files import hashed_file_path, hashed_file_ready, pin_hashed_file, unpin_hashed_file
from helpers.notifications import notify_source_user_error
from helpers.user_action_echo import post_meta_ts, slack_message_ts
from logger import log_warning


def federation_image_payloads(images: list[dict] | None) -> list[dict]:
    """Remote envelope images (``url`` / ``alt_text``)."""
    payloads: list[dict] = []
    for img in images or []:
        url = img.get("url") or img.get("image_url") or ""
        if not url:
            continue
        payloads.append({"url": url, "alt_text": img.get("alt_text") or "Shared image"})
    return payloads


def _strip_local_file_refs(file_refs: list[dict] | None) -> list[dict]:
    """Strip local-only fields from file_refs for the remote envelope."""
    out: list[dict] = []
    for item in file_refs or []:
        sha256 = item.get("sha256")
        if not sha256:
            continue
        out.append(
            {
                "sha256": sha256,
                "name": item.get("name") or "file",
                "mimetype": item.get("mimetype") or "application/octet-stream",
                "size": int(item.get("size") or 0),
            }
        )
    return out


def build_remote_envelope(envelope: dict[str, Any], channel_id: str) -> dict[str, Any]:
    """Serialize a source-canonical envelope for the peer plus target channel_id."""
    remote = dict(envelope)
    remote["channel_id"] = channel_id
    remote.pop("path", None)
    if remote.get("file_refs"):
        remote["file_refs"] = _strip_local_file_refs(remote.get("file_refs"))
    if remote.get("images"):
        remote["images"] = federation_image_payloads(remote.get("images"))
    # Never put tokens, mapped ids, or origin integer PKs on the envelope.
    remote.pop("mapped_user_id", None)
    remote.pop("sync_id", None)
    remote.pop("source_sync_channel_id", None)
    remote.pop("source_workspace_id", None)
    if remote.get("target_ts"):
        remote["target_ts"] = slack_message_ts(remote.get("target_ts"))
    for ref in remote.get("file_refs") or []:
        ref.pop("path", None)
        ref.pop("slack_file_id", None)
        ref.pop("fetch_url", None)
    return remote


def _peer_part_bytes(chunk_mb: int) -> int | None:
    """Max part size to POST. ``0`` or omitted is unlimited (one part)."""
    try:
        mb = int(chunk_mb)
    except (TypeError, ValueError):
        return None
    if mb <= 0:
        return None
    return mb * 1024 * 1024


def _retry_chunk_mb_after_413(result: dict | None, used_mb: int) -> int | None:
    """Retry only when the peer 413 body advertises a smaller positive ``file_chunk_mb``."""
    if not isinstance(result, dict) or result.get("file_chunk_mb") is None:
        return None
    try:
        advertised = int(result["file_chunk_mb"])
    except (TypeError, ValueError):
        return None
    if advertised <= 0:
        return None
    if used_mb <= 0:
        return advertised
    return advertised if advertised < used_mb else None


def _stage_files_to_peer(fed_ws: schemas.Instance, file_refs: list[dict]) -> bool:
    """Offer/parts for each unique sha256. Returns False if staging fails."""
    seen: set[str] = set()
    try:
        for item in file_refs or []:
            sha256 = item.get("sha256")
            size = int(item.get("size") or 0)
            path = item.get("path") or (hashed_file_path(sha256) if sha256 else None)
            if not sha256 or sha256 in seen:
                continue
            seen.add(sha256)
            if not path or not hashed_file_ready(sha256, size):
                if path and os.path.isfile(path):
                    size = os.path.getsize(path)
                else:
                    log_warning("stage_files_missing_local", sha256=sha256)
                    return False
            pin_hashed_file(sha256)
            if not _offer_and_post_parts(fed_ws, sha256, size, path):
                return False
        return True
    finally:
        for sha256 in seen:
            unpin_hashed_file(sha256)


def _offer_and_post_parts(
    fed_ws: schemas.Instance,
    sha256: str,
    size: int,
    path: str,
) -> bool:
    offer = push_file_offer(fed_ws, sha256, size) or {}
    if offer.get("have"):
        return True
    chunk_mb = offer.get("file_chunk_mb")
    try:
        chunk_mb = int(chunk_mb) if chunk_mb is not None else 0
    except (TypeError, ValueError):
        chunk_mb = 0
    if chunk_mb < 0:
        chunk_mb = 0
    return _post_parts(fed_ws, sha256, size, path, chunk_mb)


def _post_parts(
    fed_ws: schemas.Instance,
    sha256: str,
    size: int,
    path: str,
    chunk_mb: int,
    *,
    retried: bool = False,
) -> bool:
    part_bytes = _peer_part_bytes(chunk_mb)
    if part_bytes is None:
        part_bytes = max(size, 1)
    total = max(1, math.ceil(size / part_bytes)) if size else 1
    with open(path, "rb") as handle:
        for index in range(total):
            payload = handle.read(part_bytes)
            if payload is None:
                payload = b""
            result = push_file_part(
                fed_ws,
                sha256=sha256,
                part_index=index,
                total=total,
                size=size,
                payload=payload,
            )
            status = (result or {}).get("_http_status", 200 if result else 500)
            if status == 413 and not retried:
                retry_mb = _retry_chunk_mb_after_413(result, int(chunk_mb))
                if retry_mb is not None:
                    return _post_parts(fed_ws, sha256, size, path, retry_mb, retried=True)
            if status != 200:
                log_warning("stage_file_part_failed", sha256=sha256, index=index, status=status)
                return False
    return True


def _postmeta_from_peer_response(
    envelope: dict[str, Any],
    sync_channel: schemas.SyncChannel,
    result: dict | None,
) -> list[schemas.PostMeta]:
    if not result or result.get("_http_status") not in (None, 200):
        return []
    ts = result.get("ts")
    if not ts:
        return []
    posted_as = result.get("posted_as_user_id")
    created: list[schemas.PostMeta] = [
        schemas.PostMeta(
            post_id=str(envelope.get("post_id") or ""),
            sync_channel_id=sync_channel.id,
            ts=post_meta_ts(ts),
            posted_as_user_id=posted_as,
            source_user_id=envelope.get("source_user_id"),
            source_workspace_id=envelope.get("source_workspace_id"),
        )
    ]
    DbManager.create_records(created)
    return created


_FILE_COPY_FAILED = ":warning: SyncBot could not copy your file to the other Channels."


def _notify_remote_file_failed(
    envelope: dict[str, Any],
    source_client: WebClient | None,
    channel_id: str,
    *,
    reason: str,
    error: str | None = None,
) -> None:
    notify_source_user_error(
        source_client=source_client,
        source_user_id=envelope.get("source_user_id"),
        summary=_FILE_COPY_FAILED,
        details={
            "event": "file_share_failed",
            "reason": reason,
            "error": error,
            "channel": channel_id,
            "post_id": envelope.get("post_id"),
        },
    )


def deliver_remote(
    envelope: dict[str, Any],
    fed_ws: schemas.Instance,
    sync_channel: schemas.SyncChannel,
    *,
    stage_files: bool = True,
    source_client: WebClient | None = None,
) -> list[schemas.PostMeta]:
    """Bridge one envelope to a peer stub SyncChannel. Returns origin PostMeta rows."""
    kind = envelope.get("kind")
    action = envelope.get("action")
    channel_id = sync_channel.channel_id

    if kind == KIND_MESSAGE and action == ACTION_CREATE:
        file_refs = list(envelope.get("file_refs") or [])
        if stage_files and file_refs and not _stage_files_to_peer(fed_ws, file_refs):
            _notify_remote_file_failed(envelope, source_client, channel_id, reason="stage_failed")
            return []
        remote = build_remote_envelope(envelope, channel_id)
        result = push_message(fed_ws, remote)
        status = (result or {}).get("_http_status")
        error = (result or {}).get("error")
        if status == 409 and file_refs and error == "incomplete_file":
            if not _stage_files_to_peer(fed_ws, file_refs):
                _notify_remote_file_failed(envelope, source_client, channel_id, reason="stage_failed")
                return []
            result = push_message(fed_ws, remote)
            status = (result or {}).get("_http_status")
            error = (result or {}).get("error")
            if status == 409 and error == "incomplete_file":
                log_warning("deliver_remote_incomplete_files", channel_id=channel_id, post_id=envelope.get("post_id"))
                _notify_remote_file_failed(envelope, source_client, channel_id, reason="incomplete_file", error=error)
                return []
        if status == 409 and error == "assemble_failed":
            log_warning("deliver_remote_assemble_failed", channel_id=channel_id, post_id=envelope.get("post_id"))
            _notify_remote_file_failed(envelope, source_client, channel_id, reason="assemble_failed", error=error)
            return []
        if status == 409 and error == "parent_missing":
            log_warning(
                "deliver_remote_parent_missing",
                channel_id=channel_id,
                post_id=envelope.get("post_id"),
                thread_post_id=envelope.get("thread_post_id"),
            )
            return []
        if status not in (None, 200):
            log_warning(
                "deliver_remote_failed",
                channel_id=channel_id,
                status=status,
                error=error,
                post_id=envelope.get("post_id"),
                thread_post_id=envelope.get("thread_post_id"),
            )
            return []
        return _postmeta_from_peer_response(envelope, sync_channel, result)

    if kind == KIND_MESSAGE and action == ACTION_EDIT:
        remote = build_remote_envelope(envelope, channel_id)
        result = push_edit(fed_ws, remote)
        status = (result or {}).get("_http_status")
        if status not in (None, 200):
            log_warning(
                "deliver_remote_failed",
                channel_id=channel_id,
                status=status,
                error=(result or {}).get("error"),
                post_id=envelope.get("post_id"),
            )
        return []

    if kind == KIND_MESSAGE and action == ACTION_DELETE:
        remote = build_remote_envelope(envelope, channel_id)
        result = push_delete(fed_ws, remote)
        status = (result or {}).get("_http_status")
        if status not in (None, 200):
            log_warning(
                "deliver_remote_failed",
                channel_id=channel_id,
                status=status,
                error=(result or {}).get("error"),
                post_id=envelope.get("post_id"),
            )
        return []

    if kind == KIND_REACTION and action in (ACTION_ADD, ACTION_REMOVE):
        remote = build_remote_envelope(envelope, channel_id)
        result = push_reaction(fed_ws, remote)
        status = (result or {}).get("_http_status")
        if status not in (None, 200):
            log_warning(
                "deliver_remote_failed",
                channel_id=channel_id,
                status=status,
                error=(result or {}).get("error"),
                post_id=envelope.get("post_id"),
            )
        return []

    log_warning("deliver_remote_unsupported", kind=kind, action=action)
    return []


def stage_unique_files_for_peer(fed_ws: schemas.Instance, envelopes: list[dict]) -> bool:
    """Stage each unique sha256 once for *fed_ws* across several envelopes."""
    refs: list[dict] = []
    seen: set[str] = set()
    for env in envelopes:
        for item in env.get("file_refs") or []:
            sha = item.get("sha256")
            if sha and sha not in seen:
                seen.add(sha)
                refs.append(item)
    if not refs:
        return True
    return _stage_files_to_peer(fed_ws, refs)
