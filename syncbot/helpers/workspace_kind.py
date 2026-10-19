"""Workspace kind helpers: local install vs federated stub.

Import this submodule from handlers/helpers. Do not ``import helpers`` here.
Live vs stub is ``workspaces.instance_id`` vs this install's fingerprint — not
token presence.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from slack_sdk import WebClient

import constants
from db import DbManager, schemas
from helpers._cache import _cache_get, _cache_set
from logger import log_debug, log_error

HealResult = Literal["converted", "refused_live", "missing", "already_stub", "already_local", "blocked"]


def _self_instance_id() -> str:
    from federation.core import get_instance_id

    return get_instance_id()


def workspace_allowed_for_peer(workspace: schemas.Workspace | None, peer: schemas.Instance | None) -> bool:
    """True when *workspace* may be replicated or delivered to *peer*.

    When this peer has an allowlist, only those local Workspaces use the
    connection. With no allowlist rows, any local Workspace may. A stub is
    only that peer's own Workspace.
    """
    if workspace is None or peer is None or not getattr(peer, "instance_id", None):
        return False
    if is_stub_workspace(workspace):
        return getattr(workspace, "instance_id", None) == peer.instance_id
    if not is_local_workspace(workspace):
        return False
    workspace_id = getattr(workspace, "id", None)
    if not workspace_id:
        return False
    allowlist = DbManager.find_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == peer.instance_id],
    )
    if not allowlist:
        return True
    return workspace_id in {row.workspace_id for row in allowlist if row.workspace_id}


def is_local_workspace(workspace: schemas.Workspace | None) -> bool:
    """True when *workspace* is a live install on this instance."""
    if workspace is None or getattr(workspace, "deleted_at", None) is not None:
        return False
    instance_id = getattr(workspace, "instance_id", None)
    if not instance_id:
        return False
    try:
        return instance_id == _self_instance_id()
    except Exception:
        return False


def is_stub_workspace(workspace: schemas.Workspace | None) -> bool:
    """True when *workspace* is a remote peer stub (peer instance_id, not deleted)."""
    if workspace is None or getattr(workspace, "deleted_at", None) is not None:
        return False
    instance_id = getattr(workspace, "instance_id", None)
    if not instance_id:
        return False
    try:
        return instance_id != _self_instance_id()
    except Exception:
        return bool(instance_id)


def peer_for_workspace(workspace: schemas.Workspace | None) -> schemas.Instance | None:
    """Return the peer Instance for a stub, or None."""
    if not is_stub_workspace(workspace):
        return None
    peer_id = workspace.instance_id
    if not peer_id:
        return None
    peer = DbManager.get_record(schemas.Instance, id=peer_id)
    if not peer or peer.status != "active":
        return None
    return peer


def peer_is_trusted(peer: schemas.Instance | None) -> bool:
    if peer is None:
        return False
    return (getattr(peer, "trust_status", None) or "trusted") == "trusted"


def is_deliverable_workspace(workspace: schemas.Workspace | None) -> bool:
    """Local with this instance, or stub whose peer is active and trusted."""
    if is_local_workspace(workspace):
        return True
    if not is_stub_workspace(workspace):
        return False
    peer = peer_for_workspace(workspace)
    return bool(peer and peer_is_trusted(peer))


def can_select_channels(workspace: schemas.Workspace | None) -> bool:
    return is_local_workspace(workspace)


def can_publish_home(workspace: schemas.Workspace | None) -> bool:
    return is_local_workspace(workspace)


def can_dm(workspace: schemas.Workspace | None) -> bool:
    return is_local_workspace(workspace)


def can_promote(workspace: schemas.Workspace | None) -> bool:
    """True when *workspace* can be promoted to group owner (live install only)."""
    return is_local_workspace(workspace)


def mark_peer_trusted(peer: schemas.Instance) -> None:
    """Mark *peer* trusted and unpause SyncChannels on its stub workspaces."""
    if peer is None or not getattr(peer, "instance_id", None):
        return
    DbManager.update_records(
        schemas.Instance,
        [schemas.Instance.instance_id == peer.instance_id],
        {schemas.Instance.trust_status: "trusted"},
    )
    stubs = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.instance_id == peer.instance_id,
            schemas.Workspace.deleted_at.is_(None),
        ],
    )
    stub_ids = [ws.id for ws in stubs if ws.id]
    if stub_ids:
        DbManager.update_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.workspace_id.in_(stub_ids),
                schemas.SyncChannel.deleted_at.is_(None),
                schemas.SyncChannel.status == "paused",
            ],
            {schemas.SyncChannel.status: "active"},
        )


def mark_peer_untrusted(peer: schemas.Instance) -> None:
    """Mark *peer* untrusted and pause SyncChannels on its stub workspaces."""
    if peer is None or not getattr(peer, "instance_id", None):
        return
    DbManager.update_records(
        schemas.Instance,
        [schemas.Instance.instance_id == peer.instance_id],
        {schemas.Instance.trust_status: "untrusted"},
    )
    stubs = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.instance_id == peer.instance_id,
            schemas.Workspace.deleted_at.is_(None),
        ],
    )
    stub_ids = [ws.id for ws in stubs if ws.id]
    if stub_ids:
        DbManager.update_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.workspace_id.in_(stub_ids),
                schemas.SyncChannel.deleted_at.is_(None),
            ],
            {schemas.SyncChannel.status: "paused"},
        )


def _remember_pending_stub(workspace_id: int, peer_instance_id: str) -> None:
    existing = DbManager.find_records(
        schemas.FederationPendingStub,
        [
            schemas.FederationPendingStub.workspace_id == workspace_id,
            schemas.FederationPendingStub.instance_id == peer_instance_id,
        ],
    )
    if existing:
        return
    DbManager.create_record(
        schemas.FederationPendingStub(
            workspace_id=workspace_id,
            instance_id=peer_instance_id,
        )
    )


def _clear_pending_stub(workspace_id: int, peer_instance_id: str | None = None) -> None:
    filters = [schemas.FederationPendingStub.workspace_id == workspace_id]
    if peer_instance_id:
        filters.append(schemas.FederationPendingStub.instance_id == peer_instance_id)
    DbManager.delete_records(schemas.FederationPendingStub, filters)


def _unpause_memberships_and_channels(workspace_id: int) -> None:
    """Clear deleted_at on group memberships and sync channels (stub heal, not live restore)."""
    soft_memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id == workspace_id,
            schemas.WorkspaceGroupMember.deleted_at.isnot(None),
            schemas.WorkspaceGroupMember.status == "active",
        ],
    )
    restored_group_ids: set[int] = set()
    for membership in soft_memberships:
        group = DbManager.get_record(schemas.WorkspaceGroup, id=membership.group_id)
        if not group or group.status != "active":
            continue
        DbManager.update_records(
            schemas.WorkspaceGroupMember,
            [schemas.WorkspaceGroupMember.id == membership.id],
            {schemas.WorkspaceGroupMember.deleted_at: None},
        )
        restored_group_ids.add(membership.group_id)

    if not restored_group_ids:
        return

    soft_channels = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.workspace_id == workspace_id,
            schemas.SyncChannel.deleted_at.isnot(None),
        ],
    )
    for sync_channel in soft_channels:
        sync = DbManager.get_record(schemas.Sync, id=sync_channel.sync_id)
        if sync and sync.group_id in restored_group_ids:
            DbManager.update_records(
                schemas.SyncChannel,
                [schemas.SyncChannel.id == sync_channel.id],
                {schemas.SyncChannel.deleted_at: None, schemas.SyncChannel.status: "active"},
            )


def _log_heal(
    result: str,
    team_id: str,
    peer_instance_id: str,
    *,
    workspace_id: int | None = None,
    source: str | None = None,
) -> None:
    fields: dict[str, Any] = {
        "result": result,
        "team_id": team_id,
        "peer_instance_id": peer_instance_id,
    }
    if workspace_id is not None:
        fields["workspace_id"] = workspace_id
    if source:
        fields["source"] = source
    log_debug("heal_stub", **fields)


def notify_live_workspace_conflict(team_id: str, peer_instance_id: str) -> None:
    """DM workspace and primary admins once when a peer claims a still-live team."""
    cache_key = f"fed_live_conflict:{team_id}:{peer_instance_id}"
    if _cache_get(cache_key):
        return
    _cache_set(cache_key, True, ttl=86400)

    from helpers.notifications import notify_admins_dm
    from helpers.workspace import get_bot_token

    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    if not workspace:
        return
    workspace_name = workspace.workspace_name or workspace.team_id
    message = (
        f":globe_with_meridians: `{workspace_name}` is also installed on a connected SyncBot instance.\n"
        ":warning: This install remains active and remote federation traffic for this Workspace is blocked. "
        "Uninstall this SyncBot if the Workspace should move to the connected instance."
    )

    targets = [workspace]
    primary_team_id = (os.environ.get(constants.PRIMARY_WORKSPACE) or "").strip()
    if primary_team_id and primary_team_id != team_id:
        primary = DbManager.get_record(schemas.Workspace, id=primary_team_id)
        if primary:
            targets.append(primary)

    notified = 0
    for target in targets:
        if target.deleted_at is not None:
            continue
        token = get_bot_token(target)
        if not token:
            continue
        try:
            notify_admins_dm(
                WebClient(token=token),
                message,
                include_managers=False,
                team_id=target.team_id,
            )
            notified += 1
        except Exception:
            log_error(
                "federation_live_conflict_dm_failed",
                team_id=team_id,
                notification_team_id=target.team_id,
                peer_instance_id=peer_instance_id,
                exc_info=True,
            )
    log_debug(
        "live_conflict",
        team_id=team_id,
        peer_instance_id=peer_instance_id,
        notified=notified,
    )


def heal_workspace_to_local(team_id: str, *, source: str | None = None) -> HealResult:
    """Convert *team_id* to a live install on this instance. PostMeta stays.

    Reverse of :func:`heal_workspace_to_stub`: reinstall, import, or reclaim
    after Leave Connection.
    """
    team_id = str(team_id).strip()
    if not team_id:
        _log_heal("missing", team_id, "", source=source)
        return "missing"

    from helpers.settings import team_id_is_blocked

    if team_id_is_blocked(team_id):
        _log_heal("blocked", team_id, "", source=source)
        return "blocked"

    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    if not workspace:
        _log_heal("missing", team_id, "", source=source)
        return "missing"

    self_id = _self_instance_id()
    if is_local_workspace(workspace):
        _clear_pending_stub(workspace.id)
        _log_heal(
            "already_local",
            team_id,
            self_id,
            workspace_id=workspace.id,
            source=source,
        )
        return "already_local"

    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.id == workspace.id],
        {
            schemas.Workspace.instance_id: self_id,
            schemas.Workspace.deleted_at: None,
        },
    )
    _unpause_memberships_and_channels(workspace.id)
    _clear_pending_stub(workspace.id)
    _log_heal(
        "converted",
        team_id,
        self_id,
        workspace_id=workspace.id,
        source=source,
    )
    return "converted"


def heal_workspace_to_stub(
    team_id: str,
    peer_instance_id: str,
    *,
    source: str | None = None,
) -> HealResult:
    """Convert *team_id* to a stub for *peer_instance_id*, or refuse if still live.

    Returns:
      * ``converted`` — soft-deleted (or already convertible) row became a stub
      * ``refused_live`` — still installed here; pending stub remembered
      * ``already_stub`` — already a stub for this peer
      * ``missing`` — no workspace row for *team_id*
    """
    team_id = str(team_id).strip()
    peer_instance_id = str(peer_instance_id).strip()
    if not team_id or not peer_instance_id:
        _log_heal("missing", team_id, peer_instance_id, source=source)
        return "missing"

    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    if not workspace:
        _log_heal("missing", team_id, peer_instance_id, source=source)
        return "missing"

    if (
        getattr(workspace, "deleted_at", None) is None
        and workspace.instance_id == peer_instance_id
        and peer_instance_id != _self_instance_id()
    ):
        _clear_pending_stub(workspace.id, peer_instance_id)
        _log_heal(
            "already_stub",
            team_id,
            peer_instance_id,
            workspace_id=workspace.id,
            source=source,
        )
        return "already_stub"

    if is_local_workspace(workspace):
        _remember_pending_stub(workspace.id, peer_instance_id)
        notify_live_workspace_conflict(team_id, peer_instance_id)
        _log_heal(
            "refused_live",
            team_id,
            peer_instance_id,
            workspace_id=workspace.id,
            source=source,
        )
        return "refused_live"

    # Soft-deleted on this instance, or already pointing elsewhere: become stub.
    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.id == workspace.id],
        {
            schemas.Workspace.instance_id: peer_instance_id,
            schemas.Workspace.deleted_at: None,
        },
    )
    _unpause_memberships_and_channels(workspace.id)
    _clear_pending_stub(workspace.id, peer_instance_id)
    _log_heal(
        "converted",
        team_id,
        peer_instance_id,
        workspace_id=workspace.id,
        source=source,
    )
    return "converted"


def ensure_stub_workspace(
    *,
    team_id: str,
    workspace_name: str | None,
    instance_id: str,
) -> schemas.Workspace | None:
    """Upsert a stub Workspace for *team_id* on peer *instance_id*.

    Does **not** strip a live install. If the team is still live here, records a
    pending stub and returns the live row unchanged (caller should treat as refuse).
    """
    team_id = str(team_id).strip()
    peer_instance_id = str(instance_id).strip()
    existing = DbManager.get_record(schemas.Workspace, id=team_id)
    name = (workspace_name or team_id)[:100]

    if existing and is_local_workspace(existing):
        heal_workspace_to_stub(team_id, peer_instance_id, source="ensure_stub")
        return existing

    if existing:
        result = heal_workspace_to_stub(team_id, peer_instance_id, source="ensure_stub")
        if result in ("converted", "already_stub"):
            updates: dict[Any, Any] = {}
            if workspace_name and existing.workspace_name != name:
                updates[schemas.Workspace.workspace_name] = name
            if updates:
                DbManager.update_record(schemas.Workspace, team_id, updates)
            return DbManager.get_record(schemas.Workspace, id=team_id)
        return existing

    stub = schemas.Workspace(
        team_id=team_id,
        workspace_name=name,
        instance_id=peer_instance_id,
        deleted_at=None,
    )
    created = DbManager.create_record(stub)
    _log_heal(
        "created",
        team_id,
        peer_instance_id,
        workspace_id=getattr(created, "id", None),
        source="ensure_stub",
    )
    return DbManager.get_record(schemas.Workspace, id=team_id)


def restore_peer_stubs(peer_instance_id: str, *, source: str = "pair") -> dict[str, str]:
    """Unpause soft-deleted stubs for *peer_instance_id* (reconnect within retention)."""
    peer_instance_id = str(peer_instance_id or "").strip()
    if not peer_instance_id:
        return {}
    rows = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.instance_id == peer_instance_id,
            schemas.Workspace.deleted_at.isnot(None),
        ],
    )
    results: dict[str, str] = {}
    for workspace in rows:
        team_id = str(getattr(workspace, "team_id", None) or "").strip()
        if not team_id:
            continue
        results[team_id] = heal_workspace_to_stub(team_id, peer_instance_id, source=source)
    return results
