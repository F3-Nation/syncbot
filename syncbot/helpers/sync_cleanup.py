"""Ordered hard-delete helpers for syncs and workspaces.

``syncs`` -> ``sync_channels`` -> ``post_meta`` are plain foreign keys with no
``ON DELETE CASCADE``, so MySQL rejects a parent-first delete with error 1451.
Every hard delete in this module therefore removes children before parents.

Two properties matter to callers:

* **Not atomic.** Each :class:`~db.DbManager` call opens and commits its own
  session, so a failure part-way through leaves earlier deletes committed.
  Child-first ordering is what makes that safe — the worst case is a parent row
  with no children, which is harmless and cleaned up by re-running.
* **Idempotent.** Calling either function twice for the same id is a no-op the
  second time, so a retry after a partial failure always converges.

This module imports **submodules only** (``db``, ``db.schemas``,
``helpers._cache``) and never the ``helpers`` package, because
``helpers/__init__`` imports ``helpers.notifications`` (which calls
:func:`purge_workspace`) before ``helpers.workspace``. Importing the package
here would be a circular import at process start.
"""

from db import DbManager, schemas
from helpers._cache import _cache_delete_prefix
from logger import log_info


def _invalidate_channel_memberships(channel_ids) -> None:
    """Drop cached membership and publish-target entries for *channel_ids*."""
    for channel_id in channel_ids:
        if channel_id:
            _cache_delete_prefix(f"channel_memberships:{channel_id}:")
            _cache_delete_prefix(f"publish_targets:{channel_id}")


def purge_sync(sync_id: int) -> None:
    """Hard-delete a sync and everything beneath it, children first.

    ``syncs`` -> ``sync_channels`` -> ``post_meta`` have no ``ON DELETE
    CASCADE``, so MySQL rejects a parent-first delete. Soft-deleted channels are
    included on purpose: ``deleted_at`` only hides a row from the UI, it still
    references ``syncs`` and would block the parent delete.
    """
    if not sync_id:
        return

    channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.sync_id == sync_id],
    )

    for channel in channels:
        DbManager.delete_records(
            schemas.PostMeta,
            [schemas.PostMeta.sync_channel_id == channel.id],
        )

    if channels:
        DbManager.delete_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.sync_id == sync_id],
        )

    DbManager.delete_records(schemas.Sync, [schemas.Sync.id == sync_id])

    _invalidate_channel_memberships(channel.channel_id for channel in channels)

    log_info("sync_purged", sync_id=sync_id, sync_channels=len(channels))


def purge_sync_channels(channels) -> None:
    """Hard-delete specific ``SyncChannel`` rows and their ``post_meta``.

    Used when a workspace leaves a sync that other workspaces still use, so the
    parent ``Sync`` must survive. Invalidates fan-out for every Channel in the
    same syncs, including peers that still publish, so Leave stops delivery on
    this container instead of waiting for the 60s cache TTL.
    """
    channels = list(channels)
    sync_ids = {channel.sync_id for channel in channels if getattr(channel, "sync_id", None)}
    fanout_ids = {channel.channel_id for channel in channels if channel.channel_id}
    if sync_ids:
        peers = DbManager.find_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.sync_id.in_(list(sync_ids))],
        )
        fanout_ids.update(channel.channel_id for channel in peers if channel.channel_id)

    for channel in channels:
        DbManager.delete_records(
            schemas.PostMeta,
            [schemas.PostMeta.sync_channel_id == channel.id],
        )
        DbManager.delete_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == channel.id],
        )

    _invalidate_channel_memberships(fanout_ids)


def purge_workspace(workspace_id: int) -> None:
    """Hard-delete a workspace and every row that references it, children first.

    Groups deliberately survive. Membership rows need two different operations
    because ``workspace_group_members`` points at ``workspaces.id`` through two
    columns: rows where this workspace is the *member* are deleted, while rows
    where it was merely the *inviter* keep their membership and only lose the
    ``invited_by_workspace_id`` back-reference. Deleting the latter would evict
    another workspace's legitimate member.
    """
    if not workspace_id:
        return

    # Purge only this workspace's SyncChannels — participation owns publishing.
    # Do not delete whole Syncs because a leftover publisher_workspace_id matched.
    own_channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.workspace_id == workspace_id],
    )
    purge_sync_channels(own_channels)

    DbManager.delete_records(
        schemas.UserDirectory,
        [schemas.UserDirectory.workspace_id == workspace_id],
    )

    DbManager.delete_records(
        schemas.UserMapping,
        [
            (schemas.UserMapping.source_workspace_id == workspace_id)
            | (schemas.UserMapping.target_workspace_id == workspace_id)
        ],
    )

    # Capture the groups this workspace belonged to before its rows go away, so
    # ownership can be handed on afterwards.
    memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.workspace_id == workspace_id],
    )
    owned_group_ids = sorted({m.group_id for m in memberships if m.role == "owner" and m.group_id})

    DbManager.delete_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.workspace_id == workspace_id],
    )

    # Keep other workspaces' memberships; just drop the dangling inviter link.
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.invited_by_workspace_id == workspace_id],
        {schemas.WorkspaceGroupMember.invited_by_workspace_id: None},
    )

    # delete_record(Workspace, ...) would filter on team_id, not id.
    DbManager.delete_records(schemas.Workspace, [schemas.Workspace.id == workspace_id])

    # The retention purge is the only involuntary loss of ownership. It promotes
    # the earliest-joined remaining local member or disbands. Imported here
    # rather than at module scope to keep this module free of helpers-package
    # imports.
    from helpers.group_roles import succeed_ownership

    for group_id in owned_group_ids:
        succeed_ownership(group_id, departing_workspace_id=workspace_id)

    log_info("workspace_purged", workspace_id=workspace_id, sync_channels=len(own_channels))
