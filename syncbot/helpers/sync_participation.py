"""Channel publish/subscribe participation and fan-out discovery."""

from __future__ import annotations

from db import DbManager, schemas
from helpers._cache import _cache_delete_prefix, _cache_get, _cache_set


def invalidate_channel_memberships(channel_id: str | None) -> None:
    """Drop cached membership and publish-target entries for *channel_id*."""
    if channel_id:
        _cache_delete_prefix(f"channel_memberships:{channel_id}:")
        _cache_delete_prefix(f"publish_targets:{channel_id}")


def invalidate_sync_fanout_for_syncs(sync_ids) -> None:
    """Drop fan-out caches for every Channel that belongs to *sync_ids*.

    Includes soft-deleted rows so Pause, Leave, and uninstall still clear the
    origin ``publish_targets:`` entry that pointed at this Channel.
    """
    ids = [sid for sid in sync_ids if sid]
    if not ids:
        return
    channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id.in_(ids)],
    )
    for channel in channels:
        invalidate_channel_memberships(channel.channel_id)


def get_live_sync_channel(sync_channel: schemas.SyncChannel | None) -> schemas.SyncChannel | None:
    """Re-read *sync_channel* so pause or leave wins over a stale fan-out cache."""
    if sync_channel is None:
        return None
    row_id = getattr(sync_channel, "id", None)
    if not row_id:
        return None
    live = DbManager.get_record(schemas.SyncChannel, id=row_id)
    if live is None or live.deleted_at is not None:
        return None
    if (live.status or "active") != "active":
        return None
    return live


def parse_participation_flags(value: str | None) -> tuple[bool, bool]:
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


def get_channel_memberships(
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
    return bool(get_channel_memberships(channel_id, active_only=False))


def origin_publishes_anywhere(channel_id: str) -> bool:
    """True when *channel_id* has at least one active membership with publishes=True."""
    return any(
        channel_publishes(sync_channel)
        for sync_channel, _workspace in get_channel_memberships(channel_id, active_only=True)
    )


def get_origin_sync_channel(channel_id: str) -> schemas.SyncChannel | None:
    """Return one publishing SyncChannel for *channel_id*, or None if none publish."""
    memberships = get_channel_memberships(channel_id, active_only=True)
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

    cache_key = f"publish_targets:{channel_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    publish_sync_ids: set[int] = set()
    origin_keys: set[tuple[int, str]] = set()
    for sc, ws in get_channel_memberships(channel_id, active_only=True):
        if not channel_publishes(sc):
            continue
        publish_sync_ids.add(sc.sync_id)
        origin_keys.add((ws.id, sc.channel_id))

    if not publish_sync_ids:
        _cache_set(cache_key, [])
        return []

    sync_id_list = list(publish_sync_ids)

    peers = DbManager.find_join_records2(
        left_cls=schemas.SyncChannel,
        right_cls=schemas.Workspace,
        filters=[
            schemas.SyncChannel.sync_id.in_(sync_id_list),
            schemas.SyncChannel.deleted_at.is_(None),
            schemas.SyncChannel.status == "active",
        ],
    )

    targets: list[tuple[schemas.SyncChannel, schemas.Workspace]] = []
    seen: set[tuple[int, str]] = set()
    for sc, ws in peers:
        key = (ws.id, sc.channel_id)
        if key in origin_keys or key in seen:
            continue
        if not channel_subscribes(sc):
            continue
        seen.add(key)
        targets.append((sc, ws))

    _cache_set(cache_key, targets)
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
    candidate_sync_ids = [
        sc.sync_id
        for sc in rows
        if channel_subscribes(sc) and (exclude_sync_id is None or sc.sync_id != exclude_sync_id)
    ]
    if not candidate_sync_ids:
        return False

    publishers = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.sync_id.in_(candidate_sync_ids),
            schemas.SyncChannel.workspace_id == source_workspace_id,
            schemas.SyncChannel.channel_id == source_channel_id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    return any(channel_publishes(p) for p in publishers)
