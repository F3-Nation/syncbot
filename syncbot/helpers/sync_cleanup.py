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
here would be a circular import at Lambda cold start.
"""

import logging

from db import DbManager, schemas
from helpers._cache import _cache_delete_prefix

_logger = logging.getLogger(__name__)


def _invalidate_channel_memberships(channel_ids) -> None:
    """Drop cached membership entries for *channel_ids*."""
    for channel_id in channel_ids:
        if channel_id:
            _cache_delete_prefix(f"channel_memberships:{channel_id}:")


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

    _logger.info(
        "sync_purged",
        extra={"sync_id": sync_id, "sync_channels": len(channels)},
    )


def purge_sync_channels(channels) -> None:
    """Hard-delete specific ``SyncChannel`` rows and their ``post_meta``.

    Used when a workspace leaves a sync that other workspaces still use, so the
    parent ``Sync`` must survive. Also invalidates the fan-out cache, which the
    call sites previously forgot.
    """
    for channel in channels:
        DbManager.delete_records(
            schemas.PostMeta,
            [schemas.PostMeta.sync_channel_id == channel.id],
        )
        DbManager.delete_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == channel.id],
        )

    _invalidate_channel_memberships(channel.channel_id for channel in channels)


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

    # Syncs this workspace originally created are removed outright, along with every
    # subscriber's copy — the same authority the last publisher's Leave Sync uses.
    published = DbManager.find_records(
        schemas.Sync,
        [schemas.Sync.publisher_workspace_id == workspace_id],
    )
    for sync in published:
        purge_sync(sync.id)

    # Channels this workspace contributed to syncs owned by someone else.
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

    # Direct syncs aimed at this workspace: the column is nullable, and the
    # sync itself may still belong to a publisher that still exists.
    DbManager.update_records(
        schemas.Sync,
        [schemas.Sync.target_workspace_id == workspace_id],
        {schemas.Sync.target_workspace_id: None},
    )

    # delete_record(Workspace, ...) would filter on team_id, not id.
    DbManager.delete_records(schemas.Workspace, [schemas.Workspace.id == workspace_id])

    # The retention purge is the only involuntary loss of ownership, because it
    # is permanent. Imported here rather than at module scope to keep this
    # module free of helpers-package imports.
    from helpers.group_roles import succeed_ownership

    for group_id in owned_group_ids:
        succeed_ownership(group_id, departing_workspace_id=workspace_id)

    _logger.info(
        "workspace_purged",
        extra={
            "workspace_id": workspace_id,
            "published_syncs": len(published),
            "sync_channels": len(own_channels),
        },
    )
