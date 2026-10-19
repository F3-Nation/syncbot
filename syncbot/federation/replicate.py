"""Outbound federation membership / sync replication.

Pushes group and sync channel state to peer instances so stubs can receive
Subscribe/Create Sync without sharing integer PKs. Keys are ``uid``,
Slack ``team_id``, and Slack ``channel_id``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from db import DbManager, schemas
from federation import core as federation
from helpers.workspace_kind import is_stub_workspace, peer_for_workspace, workspace_allowed_for_peer
from logger import log_debug, log_error, log_warning


def _normalize_group_role(role: str | None) -> str:
    if role == "creator":
        return "member"
    if role == "owner":
        return "owner"
    return "member"


def mint_uid() -> str:
    return str(uuid.uuid4())


def ensure_group_uid(group: schemas.WorkspaceGroup) -> str:
    """Return a durable uid for *group*, minting if needed."""
    uid = getattr(group, "uid", None)
    if uid:
        return str(uid)
    uid = mint_uid()
    DbManager.update_records(
        schemas.WorkspaceGroup,
        [schemas.WorkspaceGroup.id == group.id],
        {schemas.WorkspaceGroup.uid: uid},
    )
    group.uid = uid
    return uid


def _sync_channel_payload(
    sync_uid: str,
    workspace: schemas.Workspace,
    sync_channel: schemas.SyncChannel,
) -> dict:
    """Fields for a SyncChannel, including a display name when known."""
    from helpers.workspace import lookup_channel_meta, remember_channel_name

    name = (getattr(sync_channel, "channel_name", None) or "").strip()
    if not name or name == sync_channel.channel_id:
        looked, _is_private = lookup_channel_meta(sync_channel.channel_id, workspace)
        if looked and looked != sync_channel.channel_id:
            remember_channel_name(sync_channel.channel_id, getattr(workspace, "id", None), looked)
            name = looked
        else:
            name = ""
    payload = {
        "sync_uid": sync_uid,
        "team_id": workspace.team_id,
        "channel_id": sync_channel.channel_id,
        "status": sync_channel.status or "active",
        "publishes": bool(getattr(sync_channel, "publishes", True)),
        "subscribes": bool(getattr(sync_channel, "subscribes", True)),
        "reaction_style": getattr(sync_channel, "reaction_style", None),
    }
    if name:
        payload["channel_name"] = name[:100]
    return payload


def ensure_sync_uid(sync: schemas.Sync) -> str:
    uid = getattr(sync, "uid", None)
    if uid:
        return str(uid)
    uid = mint_uid()
    DbManager.update_records(
        schemas.Sync,
        [schemas.Sync.id == sync.id],
        {schemas.Sync.uid: uid},
    )
    sync.uid = uid
    return uid


def peers_for_group(group_id: int) -> list[schemas.Instance]:
    """Instance rows reachable via stub members of *group_id*."""
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
            schemas.WorkspaceGroupMember.workspace_id.isnot(None),
        ],
    )
    peers: dict[str, schemas.Instance] = {}
    for member in members:
        ws = _workspace_by_pk(member.workspace_id)
        if not ws or not is_stub_workspace(ws):
            continue
        fed = peer_for_workspace(ws)
        if fed and fed.instance_id not in peers:
            peers[fed.instance_id] = fed
    return list(peers.values())


def _workspace_by_pk(workspace_id: int | None) -> schemas.Workspace | None:
    if workspace_id is None:
        return None
    rows = DbManager.find_records(schemas.Workspace, [schemas.Workspace.id == workspace_id])
    return rows[0] if rows else None


def _push_all(peers: Iterable[schemas.Instance], push_fn, payload: dict, *, label: str) -> None:
    for fed_ws in peers:
        try:
            push_fn(fed_ws, payload)
        except Exception:
            log_warning(
                "federation_replicate_failed",
                label=label,
                peer=getattr(fed_ws, "instance_id", None),
            )


def replicate_group_upsert(group: schemas.WorkspaceGroup) -> None:
    peers = peers_for_group(group.id)
    if not peers:
        return
    uid = ensure_group_uid(group)
    _push_all(
        peers,
        federation.push_group_upsert,
        {"uid": uid, "name": group.name},
        label="group_upsert",
    )


def replicate_group_invite(
    group: schemas.WorkspaceGroup,
    workspace: schemas.Workspace,
    *,
    peer: schemas.Instance | None = None,
) -> None:
    """Tell peers (or one *peer*) that *workspace* joined *group*."""
    uid = ensure_group_uid(group)
    role = "member"
    if getattr(workspace, "id", None) and group.id:
        memberships = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.group_id == group.id,
                schemas.WorkspaceGroupMember.workspace_id == workspace.id,
                schemas.WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )
        if memberships:
            role = _normalize_group_role(memberships[0].role)
    payload = {
        "uid": uid,
        "team_id": workspace.team_id,
        "workspace_name": workspace.workspace_name,
        "role": role,
    }
    targets = [peer] if peer is not None else peers_for_group(group.id)
    if peer is None and is_stub_workspace(workspace):
        fed = peer_for_workspace(workspace)
        if fed and all(t.instance_id != fed.instance_id for t in targets):
            targets = list(targets) + [fed]
    targets = [fed_ws for fed_ws in targets if workspace_allowed_for_peer(workspace, fed_ws)]
    if not targets:
        return
    # Ensure the peer has the group row before the invite.
    for fed_ws in targets:
        try:
            federation.push_group_upsert(fed_ws, {"uid": uid, "name": group.name})
            federation.push_group_invite(fed_ws, payload)
        except Exception:
            log_warning(
                "federation_replicate_failed",
                label="group_invite",
                peer=getattr(fed_ws, "instance_id", None),
            )


def replicate_group_leave(group: schemas.WorkspaceGroup, workspace: schemas.Workspace) -> None:
    peers = peers_for_group(group.id)
    if is_stub_workspace(workspace):
        fed = peer_for_workspace(workspace)
        if fed and all(p.instance_id != fed.instance_id for p in peers):
            peers = list(peers) + [fed]
    if not peers:
        return
    uid = ensure_group_uid(group)
    _push_all(
        peers,
        federation.push_group_leave,
        {"uid": uid, "team_id": workspace.team_id},
        label="group_leave",
    )


def replicate_sync_upsert(sync: schemas.Sync, group: schemas.WorkspaceGroup) -> None:
    peers = peers_for_group(group.id)
    if not peers:
        return
    group_uid = ensure_group_uid(group)
    sync_uid = ensure_sync_uid(sync)
    for fed_ws in peers:
        try:
            federation.push_group_upsert(fed_ws, {"uid": group_uid, "name": group.name})
            federation.push_sync_upsert(
                fed_ws,
                {
                    "uid": sync_uid,
                    "group_uid": group_uid,
                    "title": sync.title,
                    "description": sync.description,
                    "sync_mode": sync.sync_mode,
                },
            )
        except Exception:
            log_warning(
                "federation_replicate_failed",
                label="sync_upsert",
                peer=getattr(fed_ws, "instance_id", None),
            )


def replicate_sync_channel_upsert(
    sync: schemas.Sync,
    group: schemas.WorkspaceGroup,
    sync_channel: schemas.SyncChannel,
    workspace: schemas.Workspace,
) -> None:
    peers = peers_for_group(group.id)
    if not peers:
        return
    group_uid = ensure_group_uid(group)
    sync_uid = ensure_sync_uid(sync)
    payload = _sync_channel_payload(sync_uid, workspace, sync_channel)
    for fed_ws in peers:
        if not workspace_allowed_for_peer(workspace, fed_ws):
            continue
        try:
            federation.push_group_upsert(fed_ws, {"uid": group_uid, "name": group.name})
            federation.push_sync_upsert(
                fed_ws,
                {
                    "uid": sync_uid,
                    "group_uid": group_uid,
                    "title": sync.title,
                    "description": sync.description,
                    "sync_mode": sync.sync_mode,
                },
            )
            federation.push_sync_channel_upsert(fed_ws, payload)
        except Exception:
            log_warning(
                "federation_replicate_failed",
                label="sync_channel_upsert",
                peer=getattr(fed_ws, "instance_id", None),
            )


def replicate_sync_channel_remove(
    sync: schemas.Sync,
    group: schemas.WorkspaceGroup,
    *,
    team_id: str,
    channel_id: str,
) -> None:
    peers = peers_for_group(group.id)
    if not peers:
        return
    uid = getattr(sync, "uid", None) or ensure_sync_uid(sync)
    payload = {
        "sync_uid": str(uid),
        "team_id": team_id,
        "channel_id": channel_id,
    }
    _push_all(peers, federation.push_sync_channel_remove, payload, label="sync_channel_remove")


def replicate_sync_ended(
    sync: schemas.Sync,
    group: schemas.WorkspaceGroup,
    channels: list[tuple[schemas.SyncChannel, schemas.Workspace | None]],
) -> None:
    """Tell peers every channel in *sync* is gone (last publisher left)."""
    if not getattr(sync, "uid", None):
        ensure_sync_uid(sync)
    for sync_channel, workspace in channels:
        team_id = getattr(workspace, "team_id", None) if workspace is not None else None
        if not team_id:
            continue
        replicate_sync_channel_remove(
            sync,
            group,
            team_id=team_id,
            channel_id=sync_channel.channel_id,
        )


def _group_ids_for_workspace(workspace_id: int | None) -> set[int]:
    if not workspace_id:
        return set()
    memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id == workspace_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    return {row.group_id for row in memberships if getattr(row, "group_id", None)}


def _snapshot_group_to_peer(group: schemas.WorkspaceGroup, peer: schemas.Instance) -> None:
    uid = ensure_group_uid(group)
    federation.push_group_upsert(peer, {"uid": uid, "name": group.name})
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group.id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    for membership in members:
        workspace = _workspace_by_pk(membership.workspace_id)
        if not workspace or not getattr(workspace, "team_id", None):
            continue
        if not workspace_allowed_for_peer(workspace, peer):
            continue
        federation.push_group_invite(
            peer,
            {
                "uid": uid,
                "team_id": workspace.team_id,
                "workspace_name": workspace.workspace_name,
                "role": _normalize_group_role(membership.role),
            },
        )
    syncs = DbManager.find_records(
        schemas.Sync,
        [schemas.Sync.group_id == group.id],
    )
    for sync in syncs:
        sync_uid = ensure_sync_uid(sync)
        federation.push_sync_upsert(
            peer,
            {
                "uid": sync_uid,
                "group_uid": uid,
                "title": sync.title,
                "description": sync.description,
                "sync_mode": sync.sync_mode,
            },
        )
        channels = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == sync.id,
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
        for sync_channel in channels:
            workspace = _workspace_by_pk(sync_channel.workspace_id)
            if not workspace or not getattr(workspace, "team_id", None):
                continue
            if not workspace_allowed_for_peer(workspace, peer):
                continue
            federation.push_sync_channel_upsert(
                peer,
                _sync_channel_payload(sync_uid, workspace, sync_channel),
            )


def replicate_peer_snapshot(peer: schemas.Instance) -> None:
    """Push this instance's groups and Sync Channels to *peer* (pair and keep-warm).

    Only allowlisted local Workspaces and this peer's own stubs are invited.
    Import on the other instance only brings the moving Workspace. Never raises.
    """
    if peer is None or not getattr(peer, "instance_id", None):
        return
    try:
        group_ids: set[int] = set()
        stubs = DbManager.find_records(
            schemas.Workspace,
            [
                schemas.Workspace.instance_id == peer.instance_id,
                schemas.Workspace.deleted_at.is_(None),
            ],
        )
        for stub in stubs:
            group_ids.update(_group_ids_for_workspace(stub.id))
        allow = DbManager.find_records(
            schemas.FederationWorkspaceAllowlist,
            [schemas.FederationWorkspaceAllowlist.instance_id == peer.instance_id],
        )
        for row in allow:
            group_ids.update(_group_ids_for_workspace(row.workspace_id))
        for group_id in group_ids:
            group = DbManager.get_record(schemas.WorkspaceGroup, id=group_id)
            if not group or getattr(group, "status", None) not in (None, "active"):
                continue
            try:
                _snapshot_group_to_peer(group, peer)
            except Exception:
                log_warning(
                    "federation_snapshot",
                    peer_instance_id=peer.instance_id,
                    group_uid=getattr(group, "uid", None),
                    ok=False,
                )
        log_debug(
            "federation_snapshot",
            peer_instance_id=peer.instance_id,
            groups=len(group_ids),
        )
    except Exception:
        log_error("federation_snapshot", peer_instance_id=peer.instance_id, ok=False)
