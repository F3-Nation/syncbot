"""Discover subscribers and apply one envelope to each target."""

from __future__ import annotations

import logging
from typing import Any

from slack_sdk import WebClient

from db import schemas
from helpers.envelope import post_id_for_post_records
from helpers.post_meta import get_post_records_for_post_id
from helpers.sync_apply import apply_target
from helpers.sync_participation import iter_publish_targets
from helpers.user_action_echo import slack_message_ts
from helpers.workspace import get_federated_workspace_for_sync

_logger = logging.getLogger(__name__)


def run_sync_pipeline(
    envelope: dict[str, Any],
    *,
    source_channel_id: str,
    source_client: WebClient | None = None,
    source_sync_channel: schemas.SyncChannel | None = None,
    origin_ts: str | None = None,
    thread_parent_ts_by_channel: dict[str, str] | None = None,
) -> list[schemas.PostMeta]:
    """Discover subscribers, dedupe, apply. Returns PostMeta rows to persist (targets only).

    Follow-up envelopes (thread replies, files in a thread, edits, deletes,
    reactions) carry ``thread_post_id`` or the parent ``post_id``. Fan-out is
    only the PostMeta records for that id, never every other Sync on the Channel.
    """
    del origin_ts  # Callers still pass origin_ts; PostMeta identity is on the envelope.
    targets = iter_publish_targets(source_channel_id)
    records_post_id = post_id_for_post_records(envelope)
    post_records = get_post_records_for_post_id(records_post_id) if records_post_id else []
    post_records_by_channel = {sync_channel.channel_id: pm for pm, sync_channel, _ws in post_records}
    parent_ts_by_channel = thread_parent_ts_by_channel or {
        sync_channel.channel_id: slack_message_ts(pm.ts) for pm, sync_channel, _ws in post_records
    }
    if records_post_id is not None:
        allowed = set(post_records_by_channel)
        targets = [
            (sync_channel, workspace) for sync_channel, workspace in targets if sync_channel.channel_id in allowed
        ]

    if not targets:
        return []

    post_list: list[schemas.PostMeta] = []
    name_probe_cache: dict = {}
    source_workspace_id = envelope.get("source_workspace_id")
    is_thread_create = bool(envelope.get("thread_post_id"))

    for sync_channel, workspace in targets:
        try:
            fed_ws = get_federated_workspace_for_sync(sync_channel.sync_id)
            is_remote = bool(fed_ws and source_workspace_id and workspace.id != source_workspace_id)
            target_meta = post_records_by_channel.get(sync_channel.channel_id)
            thread_ts = parent_ts_by_channel.get(sync_channel.channel_id) if is_thread_create else None
            if is_thread_create and not thread_ts:
                continue

            if is_remote and fed_ws:
                from federation.deliver import deliver_remote

                env = dict(envelope)
                env["sync_id"] = sync_channel.sync_id
                if target_meta is not None:
                    env["target_ts"] = slack_message_ts(target_meta.ts)
                deliver_remote(env, fed_ws, sync_channel.channel_id)
                continue

            created = apply_target(
                envelope,
                sync_channel,
                workspace,
                source_client=source_client,
                source_sync_channel=source_sync_channel,
                thread_ts=thread_ts,
                target_post_meta=target_meta,
                name_probe_cache=name_probe_cache,
            )
            post_list.extend(created)
        except Exception as exc:
            _logger.warning(
                "run_sync_pipeline_target_failed",
                extra={"channel_id": sync_channel.channel_id, "error": str(exc)},
            )
    return post_list
