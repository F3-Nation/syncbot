"""Channel publish/subscribe participation and fan-out discovery."""

from __future__ import annotations

import logging

from db import DbManager, schemas
from helpers._cache import _cache_delete_prefix, _cache_get, _cache_set

_logger = logging.getLogger(__name__)


def invalidate_channel_memberships(channel_id: str | None) -> None:
    """Drop cached membership entries for *channel_id* (all active/all keys)."""
    if channel_id:
        _cache_delete_prefix(f"channel_memberships:{channel_id}:")


def participation_flags(value: str | None) -> tuple[bool, bool]:
    """Map a participation radio value to ``(publishes, subscribes)``.

    Leftover ``subscribe_and_publish`` is the same as Publish and Subscribe.
    """
    raw = (value or "").strip()
    if raw == "publish_only":
        return True, False
    if raw == "subscribe_only":
        return False, True
    return True, True


def participation_label(sync_channel: schemas.SyncChannel | None) -> str:
    """Radio wording for this membership: Publish only, Subscribe only, or Publish and Subscribe."""
    publishes = channel_publishes(sync_channel)
    subscribes = channel_subscribes(sync_channel)
    if publishes and not subscribes:
        return "Publish only"
    if subscribes and not publishes:
        return "Subscribe only"
    return "Publish and Subscribe"


def channel_publishes(sync_channel: schemas.SyncChannel | None) -> bool:
    """True when this membership originates messages and reactions."""
    if sync_channel is None:
        return False
    return bool(getattr(sync_channel, "publishes", True))


def channel_subscribes(sync_channel: schemas.SyncChannel | None) -> bool:
    """True when this membership receives messages and reactions."""
    if sync_channel is None:
        return False
    return bool(getattr(sync_channel, "subscribes", True))


def find_channel_memberships(
    channel_id: str,
    *,
    active_only: bool = True,
) -> list[tuple[schemas.SyncChannel, schemas.Workspace]]:
    """Return every live (SyncChannel, Workspace) row for *channel_id*.

    Unlike the old first-sync peer list, this returns memberships across all
    syncs so leave/Home/pause see every relationship.
    """
    if not channel_id:
        return []

    cache_key = f"channel_memberships:{channel_id}:{'active' if active_only else 'all'}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    filters = [
        schemas.SyncChannel.channel_id == channel_id,
        schemas.SyncChannel.deleted_at.is_(None),
    ]
    if active_only:
        filters.append(schemas.SyncChannel.status == "active")

    rows = DbManager.find_join_records2(
        left_cls=schemas.SyncChannel,
        right_cls=schemas.Workspace,
        filters=filters,
    )

    seen: set[tuple[int, int]] = set()
    deduped: list[tuple[schemas.SyncChannel, schemas.Workspace]] = []
    for sc, ws in rows:
        key = (sc.sync_id, ws.id)
        if key not in seen:
            seen.add(key)
            deduped.append((sc, ws))

    _cache_set(cache_key, deduped)
    return deduped


def channel_has_membership(channel_id: str) -> bool:
    """True when any non-deleted SyncChannel exists for *channel_id* (paused or active)."""
    if not channel_id:
        return False
    rows = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.channel_id == channel_id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    return bool(rows)


def origin_publishes_anywhere(channel_id: str) -> bool:
    """True when *channel_id* has at least one active membership with publishes=True."""
    return any(
        channel_publishes(sync_channel)
        for sync_channel, _workspace in find_channel_memberships(channel_id, active_only=True)
    )


def find_origin_sync_channel(channel_id: str) -> schemas.SyncChannel | None:
    """Return one publishing SyncChannel for *channel_id*, or None if none publish."""
    memberships = find_channel_memberships(channel_id, active_only=True)
    for sc, _ws in memberships:
        if channel_publishes(sc):
            return sc
    return None


def iter_publish_targets(
    channel_id: str,
) -> list[tuple[schemas.SyncChannel, schemas.Workspace]]:
    """Subscribers of syncs where *channel_id* publishes; dedupe (workspace, channel).

    Does not walk the target Channel's other groups (no hop).
    """
    if not channel_id:
        return []

    publish_sync_ids: set[int] = set()
    origin_keys: set[tuple[int, str]] = set()
    for sc, ws in find_channel_memberships(channel_id, active_only=True):
        if not channel_publishes(sc):
            continue
        publish_sync_ids.add(sc.sync_id)
        origin_keys.add((ws.id, sc.channel_id))

    if not publish_sync_ids:
        return []

    targets: list[tuple[schemas.SyncChannel, schemas.Workspace]] = []
    seen: set[tuple[int, str]] = set()
    for sync_id in publish_sync_ids:
        sync = DbManager.get_record(schemas.Sync, sync_id)
        allowed_workspaces: set[int] | None = None
        if sync is not None and getattr(sync, "sync_mode", None) == "direct" and sync.target_workspace_id:
            allowed_workspaces = {sync.target_workspace_id}
            if sync.publisher_workspace_id:
                allowed_workspaces.add(sync.publisher_workspace_id)
        peers = DbManager.find_join_records2(
            left_cls=schemas.SyncChannel,
            right_cls=schemas.Workspace,
            filters=[
                schemas.SyncChannel.sync_id == sync_id,
                schemas.SyncChannel.deleted_at.is_(None),
                schemas.SyncChannel.status == "active",
            ],
        )
        for sc, ws in peers:
            if allowed_workspaces is not None and ws.id not in allowed_workspaces:
                continue
            key = (ws.id, sc.channel_id)
            if key in origin_keys or key in seen:
                continue
            if not channel_subscribes(sc):
                continue
            seen.add(key)
            targets.append((sc, ws))
    return targets


def already_subscribed_to_source(
    *,
    workspace_id: int,
    source_workspace_id: int,
    source_channel_id: str,
    exclude_sync_id: int | None = None,
) -> bool:
    """True if *workspace_id* already subscribes to this published source in another sync."""
    rows = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.workspace_id == workspace_id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    for sc in rows:
        if exclude_sync_id is not None and sc.sync_id == exclude_sync_id:
            continue
        if not channel_subscribes(sc):
            continue
        publishers = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == sc.sync_id,
                schemas.SyncChannel.workspace_id == source_workspace_id,
                schemas.SyncChannel.channel_id == source_channel_id,
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
        if any(channel_publishes(p) for p in publishers):
            return True
    return False
