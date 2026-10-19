"""Discover subscribers and apply one envelope to each target."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from slack_sdk import WebClient

from db import schemas
from helpers.envelope import ACTION_CREATE, KIND_MESSAGE, get_post_id_for_post_records
from helpers.post_meta import get_post_records_for_post_id
from helpers.sync_apply import apply_target
from helpers.sync_participation import iter_publish_targets
from helpers.user_action_echo import slack_message_ts
from helpers.workspace_kind import (
    is_local_workspace,
    is_stub_workspace,
    peer_for_workspace,
    peer_is_trusted,
    workspace_allowed_for_peer,
)
from logger import log_debug, log_warning


def run_sync_pipeline(
    envelope: dict[str, Any],
    *,
    source_channel_id: str,
    source_client: WebClient | None = None,
    source_sync_channel: schemas.SyncChannel | None = None,
    thread_parent_ts_by_channel: dict[str, str] | None = None,
) -> list[schemas.PostMeta]:
    """Discover subscribers, dedupe, apply. Returns PostMeta rows to persist (targets only).

    Thread creates (``thread_post_id`` on a message create) fan out locally
    only to Channels that already have that parent PostMeta, never a sibling
    Sync as a new top-level message. Trusted cross-instance Channels still
    get the envelope; inbound 409s if the peer has no parent. Edits, deletes,
    and reactions fan out to every publish target; ``apply_target`` no-ops
    and inbound 409s when PostMeta is missing.

    Same-instance targets use ``apply_target``. Cross-instance targets use
    ``deliver_remote`` (files staged once per peer instance).
    """
    targets = iter_publish_targets(source_channel_id)
    records_post_id = get_post_id_for_post_records(envelope)
    post_records = get_post_records_for_post_id(records_post_id) if records_post_id else []
    post_records_by_channel = {sync_channel.channel_id: pm for pm, sync_channel, _ws in post_records}
    parent_ts_by_channel = thread_parent_ts_by_channel or {
        sync_channel.channel_id: slack_message_ts(pm.ts) for pm, sync_channel, _ws in post_records
    }
    discovered = [sync_channel.channel_id for sync_channel, _workspace in targets]
    is_thread_create = (
        envelope.get("kind") == KIND_MESSAGE
        and envelope.get("action") == ACTION_CREATE
        and bool(envelope.get("thread_post_id"))
    )
    if is_thread_create:
        allowed = set(post_records_by_channel)
        targets = [
            (sync_channel, workspace)
            for sync_channel, workspace in targets
            if is_stub_workspace(workspace) or sync_channel.channel_id in allowed
        ]
        if not targets:
            log_debug(
                "pipeline_no_targets",
                post_id=records_post_id,
                kind=envelope.get("kind"),
                action=envelope.get("action"),
                thread_post_id=envelope.get("thread_post_id"),
                source_channel_id=source_channel_id,
                allowed=sorted(allowed),
                discovered=discovered,
            )
            return []

    if not targets:
        return []

    post_list: list[schemas.PostMeta] = []
    name_probe_cache: dict = {}

    local_targets: list[tuple[schemas.SyncChannel, schemas.Workspace]] = []
    stub_by_peer: dict[str, list[tuple[schemas.SyncChannel, schemas.Workspace, schemas.Instance]]] = defaultdict(list)
    source_ws = None
    if envelope.get("source_workspace_id"):
        from helpers.workspace import get_workspace_by_id

        source_ws = get_workspace_by_id(envelope.get("source_workspace_id"))

    for sync_channel, workspace in targets:
        if is_thread_create and not is_stub_workspace(workspace):
            thread_ts = parent_ts_by_channel.get(sync_channel.channel_id)
            if not thread_ts:
                log_debug(
                    "pipeline_skip",
                    reason="no_parent_ts",
                    channel_id=sync_channel.channel_id,
                    post_id=envelope.get("post_id"),
                    thread_post_id=envelope.get("thread_post_id"),
                )
                continue

        if is_stub_workspace(workspace):
            fed_ws = peer_for_workspace(workspace)
            if not fed_ws or not peer_is_trusted(fed_ws):
                log_debug(
                    "pipeline_skip",
                    reason="stub_peer_unavailable",
                    channel_id=sync_channel.channel_id,
                    workspace_id=workspace.id,
                )
                continue
            if source_ws is not None and not workspace_allowed_for_peer(source_ws, fed_ws):
                log_debug(
                    "pipeline_skip",
                    reason="origin_not_allowed",
                    channel_id=sync_channel.channel_id,
                    workspace_id=getattr(source_ws, "id", None),
                    peer_instance_id=fed_ws.instance_id,
                )
                continue
            stub_by_peer[fed_ws.instance_id].append((sync_channel, workspace, fed_ws))
        elif is_local_workspace(workspace):
            local_targets.append((sync_channel, workspace))
        else:
            log_debug(
                "pipeline_skip",
                reason="workspace_not_deliverable",
                channel_id=sync_channel.channel_id,
                workspace_id=getattr(workspace, "id", None),
            )

    if stub_by_peer and source_client:
        from helpers.user_map import rewrite_envelope_channel_refs
        from helpers.workspace import get_workspace_by_id

        source_ws = None
        if envelope.get("source_workspace_id"):
            source_ws = get_workspace_by_id(envelope.get("source_workspace_id"))
        remote_envelope = dict(envelope)
        rewrite_envelope_channel_refs(remote_envelope, source_client, source_ws)
    else:
        remote_envelope = envelope

    for sync_channel, workspace in local_targets:
        try:
            target_meta = post_records_by_channel.get(sync_channel.channel_id)
            thread_ts = parent_ts_by_channel.get(sync_channel.channel_id) if is_thread_create else None
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
            post_list.extend(created.created)
        except Exception as exc:
            log_warning(
                "run_sync_pipeline_target_failed",
                channel_id=sync_channel.channel_id,
                error=str(exc),
            )

    if stub_by_peer:
        from federation.deliver import deliver_remote, stage_unique_files_for_peer

        for _peer_id, stub_targets in stub_by_peer.items():
            fed_ws = stub_targets[0][2]
            log_debug(
                "pipeline_remote",
                peer_instance_id=fed_ws.instance_id,
                channel_ids=[sc.channel_id for sc, _, _ in stub_targets],
                kind=envelope.get("kind"),
                action=envelope.get("action"),
                post_id=envelope.get("post_id"),
            )
            try:
                staged = stage_unique_files_for_peer(fed_ws, [envelope])
            except Exception as exc:
                log_warning("run_sync_pipeline_stage_failed", error=str(exc))
                staged = False
            for sync_channel, _workspace, peer in stub_targets:
                try:
                    env = dict(remote_envelope)
                    target_meta = post_records_by_channel.get(sync_channel.channel_id)
                    if target_meta is not None:
                        env["target_ts"] = slack_message_ts(target_meta.ts)
                    created = deliver_remote(
                        env,
                        peer,
                        sync_channel,
                        stage_files=not staged,
                        source_client=source_client,
                    )
                    post_list.extend(created)
                except Exception as exc:
                    log_warning(
                        "run_sync_pipeline_remote_failed",
                        channel_id=sync_channel.channel_id,
                        error=str(exc),
                    )

    return post_list
