"""PostMeta lookups and origin-row construction."""

from __future__ import annotations

import constants
from db import DbManager, schemas
from helpers.sync_participation import channel_publishes
from helpers.user_action_echo import post_meta_ts
from logger import log_debug


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
    try:
        ts_value = post_meta_ts(thread_ts)
    except (TypeError, ValueError):
        return []
    posts = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.ts == ts_value])
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
        ts_value = post_meta_ts(ts)
    except (TypeError, ValueError):
        return False
    from helpers.sync_participation import get_channel_memberships

    memberships = get_channel_memberships(channel_id, active_only=False)
    if not memberships:
        return False
    ids = [sc.id for sc, _ws in memberships]
    rows = DbManager.find_records(
        schemas.PostMeta,
        [
            schemas.PostMeta.sync_channel_id.in_(ids),
            schemas.PostMeta.ts == ts_value,
        ],
    )
    return bool(rows)


def get_publishing_post_records(
    post_records: list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]],
    channel_id: str,
) -> list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]]:
    """Publishing memberships on *channel_id* that already have these PostMeta records.

    A copy on a publishing Channel still originates follow-ups (reactions,
    thread replies, edits, deletes) using the shared ``post_id``. Inbound
    *creates* of that copy are skipped earlier by echo and
    :func:`post_meta_exists_for_channel_ts`. Hybrid notices never originate.
    """
    rows: list[tuple[schemas.PostMeta, schemas.SyncChannel, schemas.Workspace]] = []
    seen: set[int] = set()
    for post_meta, sync_channel, workspace in post_records:
        if getattr(sync_channel, "channel_id", None) != channel_id:
            continue
        if not channel_publishes(sync_channel):
            continue
        kind = getattr(post_meta, "kind", None) or constants.POST_META_KIND_MESSAGE
        if kind == constants.POST_META_KIND_REACTION_NOTICE:
            continue
        if sync_channel.id in seen:
            continue
        seen.add(sync_channel.id)
        rows.append((post_meta, sync_channel, workspace))
    return rows


def get_target_post_meta(post_id: str, sync_channel: schemas.SyncChannel) -> schemas.PostMeta | None:
    """The PostMeta copy of *post_id* on *sync_channel*, if any."""
    rows = DbManager.find_records(
        schemas.PostMeta,
        [
            schemas.PostMeta.post_id == post_id,
            schemas.PostMeta.sync_channel_id == sync_channel.id,
        ],
    )
    return rows[0] if rows else None


def build_origin_post_meta_rows(
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
                ts=post_meta_ts(source_ts),
                source_user_id=user_id,
                source_workspace_id=source_workspace_id,
            )
        )
    if not rows and source_sync_channel is not None:
        rows.append(
            schemas.PostMeta(
                post_id=post_uuid,
                sync_channel_id=source_sync_channel.id,
                ts=post_meta_ts(source_ts),
                source_user_id=user_id,
                source_workspace_id=source_workspace_id,
            )
        )
    return rows


def complete_copy_ts_from_pending_share(
    team_id: str | None,
    channel_id: str | None,
    ts: str | None,
    file_ids: list[str],
) -> bool | None:
    """Commit copy PostMeta for an in-flight file apply once Slack emits the share ts.

    ``True`` wrote a row. ``False`` means retry (SyncChannel row not visible yet).
    ``None`` means this event is not that apply, or the ts was already stored.

    The pending row is the apply that already ran: envelope ``post_id``,
    ``sync_channel_id``, and source fields. A different ts already stored
    on this SyncChannel is not this share and must not block it.
    """
    if not team_id or not channel_id or not ts or not file_ids:
        return None
    from helpers.user_action_echo import find_pending_file_share, take_pending_file_share

    pending = None
    pending_file_id = None
    for file_id in file_ids:
        pending = find_pending_file_share(team_id, channel_id, file_id)
        if pending:
            pending_file_id = file_id
            break
    if not pending or pending_file_id is None:
        return None
    if post_meta_exists_for_channel_ts(channel_id, ts):
        take_pending_file_share(team_id, channel_id, pending_file_id)
        return None
    sync_channel = DbManager.get_record(schemas.SyncChannel, pending.sync_channel_id)
    if not sync_channel or sync_channel.channel_id != channel_id:
        log_debug(
            "pending_share_copy",
            reason="sync_channel_missing",
            channel_id=channel_id,
            post_id=pending.post_id,
            sync_channel_id=pending.sync_channel_id,
        )
        return False
    DbManager.create_records(
        [
            schemas.PostMeta(
                post_id=pending.post_id,
                sync_channel_id=sync_channel.id,
                ts=post_meta_ts(ts),
                posted_as_user_id=pending.posted_as_user_id,
                source_user_id=pending.source_user_id,
                source_workspace_id=pending.source_workspace_id,
            )
        ]
    )
    take_pending_file_share(team_id, channel_id, pending_file_id)
    return True
