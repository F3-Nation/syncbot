"""PostMeta lookups and origin-row construction."""

from __future__ import annotations

from db import DbManager, schemas
from helpers.sync_participation import channel_publishes


def _dedupe_post_records(
    post_records: list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]],
) -> list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]]:
    post_records.sort(key=lambda row: row[0].id)
    seen: set[tuple[int, str]] = set()
    deduped: list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]] = []
    for pm, sc, ws in post_records:
        key = (ws.id, sc.channel_id)
        if key not in seen:
            seen.add(key)
            deduped.append((pm, sc, ws))
    return deduped


def get_post_records_for_post_id(
    post_id: str,
) -> list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]]:
    """Every live PostMeta row for *post_id* (origin and copies)."""
    if not post_id:
        return []
    post_records = DbManager.find_join_records3(
        left_cls=schemas.PostMeta,
        right_cls1=schemas.SyncChannel,
        right_cls2=schemas.Workspace,
        filters=[
            schemas.PostMeta.post_id == post_id,
            schemas.SyncChannel.status == "active",
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    return _dedupe_post_records(post_records)


def get_post_records(thread_ts: str) -> list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]]:
    """Look up all PostMeta records that share a ``post_id`` with this timestamp.

    A Channel that publishes in more than one sync stores one origin row per
    membership, so several ``PostMeta`` rows can share the same ``ts``. Union
    every matching ``post_id`` rather than taking ``post[0]`` only.
    """
    posts = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.ts == float(thread_ts)])
    post_ids = {row.post_id for row in posts if row.post_id}
    if not post_ids:
        return []
    post_records = DbManager.find_join_records3(
        left_cls=schemas.PostMeta,
        right_cls1=schemas.SyncChannel,
        right_cls2=schemas.Workspace,
        filters=[
            schemas.PostMeta.post_id.in_(post_ids),
            schemas.SyncChannel.status == "active",
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    return _dedupe_post_records(post_records)


def post_meta_exists_for_channel_ts(channel_id: str, ts: str | float) -> bool:
    """True when any PostMeta row already records this channel+ts (copy guard)."""
    try:
        float_ts = float(ts)
    except (TypeError, ValueError):
        return False
    sync_channels = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.channel_id == channel_id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    if not sync_channels:
        return False
    ids = [sc.id for sc in sync_channels]
    rows = DbManager.find_records(
        schemas.PostMeta,
        [
            schemas.PostMeta.sync_channel_id.in_(ids),
            schemas.PostMeta.ts == float_ts,
        ],
    )
    return bool(rows)


def find_publishing_post_records(
    post_records: list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]],
    channel_id: str,
) -> list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]]:
    """Publishing memberships on *channel_id* that already have these PostMeta records."""
    rows: list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]] = []
    seen: set[int] = set()
    for post_meta, sync_channel, workspace in post_records:
        if getattr(sync_channel, "channel_id", None) != channel_id:
            continue
        if not channel_publishes(sync_channel):
            continue
        if sync_channel.id in seen:
            continue
        seen.add(sync_channel.id)
        rows.append((post_meta, sync_channel, workspace))
    return rows


def target_post_meta_for(post_id: str, sync_channel: schemas.SyncChannel) -> schemas.PostMeta | None:
    """The PostMeta copy of *post_id* on *sync_channel*, if any."""
    rows = DbManager.find_records(
        schemas.PostMeta,
        [
            schemas.PostMeta.post_id == post_id,
            schemas.PostMeta.sync_channel_id == sync_channel.id,
        ],
    )
    return rows[0] if rows else None


def origin_post_meta_rows(
    memberships: list[tuple],
    channel_id: str,
    post_uuid: str,
    source_ts: str,
    user_id: str | None,
    source_workspace_id: int,
    source_sync_channel=None,
) -> list[schemas.PostMeta]:
    """One origin PostMeta per publishing membership (same post_id, each sync)."""
    rows: list[schemas.PostMeta] = []
    seen: set[int] = set()
    for sync_channel, _workspace in memberships or []:
        if getattr(sync_channel, "channel_id", None) != channel_id:
            continue
        if not channel_publishes(sync_channel):
            continue
        if sync_channel.id in seen:
            continue
        seen.add(sync_channel.id)
        rows.append(
            schemas.PostMeta(
                post_id=post_uuid,
                sync_channel_id=sync_channel.id,
                ts=float(source_ts),
                source_user_id=user_id,
                source_workspace_id=source_workspace_id,
            )
        )
    if not rows and source_sync_channel is not None:
        rows.append(
            schemas.PostMeta(
                post_id=post_uuid,
                sync_channel_id=source_sync_channel.id,
                ts=float(source_ts),
                source_user_id=user_id,
                source_workspace_id=source_workspace_id,
            )
        )
    return rows
