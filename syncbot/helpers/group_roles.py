"""Group ownership rules.

``role`` on ``workspace_group_members`` is descriptive except for ``owner``,
which is load-bearing: owners may promote another workspace, may leave only
while another active owner remains, and may disband a group they solely own and
solely publish into.

The invariant is **every group has an owner** — an active owner, or a retained
uninstalled owner during the retention window. Multiple owners is a normal,
legal state. It is a per-group aggregate, not a row property, so no database
constraint can express it; it lives here plus the backfill in migration
``003_group_roles``.

Ownership is lost only by choice. Uninstall, token revocation, and every other
offline event leave it intact: remaining members still see the group, with no
owner listed, until retention ends. After that window, the purge promotes the
earliest-joined remaining local member, or disbands the group if none remain.
``PRIMARY_WORKSPACE`` may join or be invited like any other workspace; it is
never added or made owner for being primary. Home does not promote on read.

These are workspace-level gates that layer *on top of*
``helpers.is_workspace_manager``. Both must pass; neither replaces the other.

Imports submodules only, per the import-direction constraint in
``helpers/sync_cleanup.py``.
"""

from datetime import datetime

from db import DbManager, schemas
from logger import log_info

OWNER = "owner"
MEMBER = "member"


def get_active_members(group_id: int) -> list[schemas.WorkspaceGroupMember]:
    """Return every active, non-deleted membership row for *group_id*."""
    return DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )


def get_active_owners(group_id: int) -> list[schemas.WorkspaceGroupMember]:
    """Return the active owners of *group_id*."""
    return [member for member in get_active_members(group_id) if member.role == OWNER]


def get_retained_owners(group_id: int) -> list[schemas.WorkspaceGroupMember]:
    """Return owners that are active *or* merely soft-deleted.

    A soft-deleted owner is **retained**, not missing: uninstall and token
    revocation soft-delete the membership, and treating that as "no owner" would
    silently strip ownership during the retention window.
    """
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.role == OWNER,
        ],
    )
    return list(members)


def is_workspace_owner(group_id: int, workspace_id: int) -> bool:
    """Return whether *workspace_id* is an active owner of *group_id*."""
    if not group_id or not workspace_id:
        return False
    return any(member.workspace_id == workspace_id for member in get_active_owners(group_id))


def _is_local_member(member: schemas.WorkspaceGroupMember) -> bool:
    """True when the member's workspace is a live install on this instance."""
    if not member.workspace_id:
        return False
    from helpers.workspace import get_workspace_by_id
    from helpers.workspace_kind import can_promote

    return can_promote(get_workspace_by_id(member.workspace_id))


def _promotion_candidate(
    members: list[schemas.WorkspaceGroupMember],
    exclude_workspace_id: int | None = None,
) -> schemas.WorkspaceGroupMember | None:
    """Pick the earliest-joined active local member, excluding a departing workspace.

    Federated members are never eligible: promoting one would hand group control
    to a remote instance.
    """
    eligible = [
        member
        for member in members
        if member.workspace_id
        and member.workspace_id != exclude_workspace_id
        and member.role != OWNER
        and _is_local_member(member)
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda m: (m.joined_at is None, m.joined_at or datetime.max, m.id))


def _set_role(member_id: int, role: str) -> None:
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member_id],
        {schemas.WorkspaceGroupMember.role: role},
    )


def can_workspace_leave(group_id: int, workspace_id: int) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for *workspace_id* leaving *group_id*.

    Plain members may always leave. An owner may leave only while at least one
    other active owner remains. A sole member uses Disband Group instead; Home
    hides Leave in that case.
    """
    if not is_workspace_owner(group_id, workspace_id):
        return True, ""

    others = [member for member in get_active_members(group_id) if member.workspace_id != workspace_id]
    if any(member.role == OWNER for member in others):
        return True, ""
    return False, "sole_owner"


def succeed_ownership(group_id: int, departing_workspace_id: int | None = None) -> schemas.WorkspaceGroupMember | None:
    """Promote the earliest-joined remaining local member, or disband the group.

    Only the retention purge calls this, after the last owner's membership is
    permanently deleted. An uninstalled owner still on the row (retention)
    blocks it. Only an existing local member can be promoted. Federated stubs
    are never promoted. ``PRIMARY_WORKSPACE`` is never added as a member; if it
    already joined, it may be promoted like any other local member after
    retention.
    """
    if not group_id:
        return None

    if get_retained_owners(group_id):
        return None

    members = get_active_members(group_id)
    candidate = _promotion_candidate(members, exclude_workspace_id=departing_workspace_id)
    if candidate:
        _set_role(candidate.id, OWNER)
        log_info(
            "group_owner_succeeded",
            group_id=group_id,
            member_id=candidate.id,
            workspace_id=candidate.workspace_id,
            reason="earliest_active_local_member",
        )
        return candidate

    _disband_group(group_id)
    return None


def _disband_group(group_id: int) -> None:
    """Hard-delete a group and its remaining children. Used when purge has no successor."""
    from helpers.sync_cleanup import purge_sync

    for sync in DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group_id]):
        purge_sync(sync.id)
    DbManager.delete_records(schemas.UserMapping, [schemas.UserMapping.group_id == group_id])
    DbManager.delete_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.group_id == group_id],
    )
    DbManager.delete_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])
    log_info("group_disbanded_no_successor", group_id=group_id)


def get_promotable_members(group_id: int) -> list[schemas.WorkspaceGroupMember]:
    """Return members an owner may promote: active, local, and not already owners.

    Pending invitees and federated stub members are excluded.
    """
    return [
        member
        for member in get_active_members(group_id)
        if member.workspace_id and member.role != OWNER and _is_local_member(member)
    ]


def can_disband(group_id: int, workspace_id: int) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for *workspace_id* disbanding *group_id*.

    Two conditions must both hold:

    1. The acting workspace is the group's **only** owner, including any
       co-owner still in uninstall retention. Co-owners have equal standing
       and should not be dissolved out of a group by a peer. This does not
       deadlock, because self-demotion is allowed while another owner
       remains.
    2. The acting workspace is the group's **only publisher**. Otherwise a
       disband would destroy syncs another workspace authored.

    Condition 2 uses ``sync_channels.publishes`` (participation).
    """
    owners = get_active_owners(group_id)
    if not any(owner.workspace_id == workspace_id for owner in owners):
        return False, "not_owner"
    if len(owners) > 1:
        return False, "co_owner_exists"
    if any(row.workspace_id != workspace_id for row in get_retained_owners(group_id)):
        return False, "co_owner_exists"

    if get_other_publisher_workspace_ids(group_id, workspace_id):
        return False, "other_publishers"

    return True, ""


def get_other_publisher_workspace_ids(group_id: int, workspace_id: int) -> list[int]:
    """Return workspaces other than *workspace_id* that publish a sync into *group_id*."""
    syncs = DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group_id])
    if not syncs:
        return []
    sync_ids = [sync.id for sync in syncs]
    publishers = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.sync_id.in_(sync_ids),
            schemas.SyncChannel.publishes.is_(True),
            schemas.SyncChannel.deleted_at.is_(None),
            schemas.SyncChannel.workspace_id != workspace_id,
        ],
    )
    return sorted({row.workspace_id for row in publishers if row.workspace_id})


def get_owner_ids_blocking_allowlist_drop(instance_id: str, dropped_workspace_ids) -> list[int]:
    """Local workspace ids that still own a group with a stub of *instance_id*."""
    from helpers.workspace import get_workspace_by_id
    from helpers.workspace_kind import is_stub_workspace

    peer = (instance_id or "").strip()
    dropped: list[int] = []
    for raw in dropped_workspace_ids or []:
        try:
            dropped.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not peer or not dropped:
        return []
    blocked: list[int] = []
    for workspace_id in dropped:
        if workspace_id in blocked:
            continue
        owner_rows = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.workspace_id == workspace_id,
                schemas.WorkspaceGroupMember.role == OWNER,
                schemas.WorkspaceGroupMember.status == "active",
                schemas.WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )
        for owner in owner_rows:
            for member in get_active_members(owner.group_id):
                other = get_workspace_by_id(member.workspace_id) if member.workspace_id else None
                if is_stub_workspace(other) and getattr(other, "instance_id", None) == peer:
                    blocked.append(workspace_id)
                    break
            else:
                continue
            break
    return blocked


def get_owner_team_ids_blocking_stub_pause(peer_instance_id: str, stub_team_ids) -> list[str]:
    """Stub team ids that still own a mixed group with a local workspace."""
    from helpers.workspace import get_workspace_by_id
    from helpers.workspace_kind import is_local_workspace, is_stub_workspace

    peer = (peer_instance_id or "").strip()
    if not peer:
        return []
    blocked: list[str] = []
    for raw in stub_team_ids or []:
        team_id = str(raw or "").strip()
        if not team_id or team_id in blocked:
            continue
        workspace = DbManager.get_record(schemas.Workspace, id=team_id)
        if not workspace or not is_stub_workspace(workspace):
            continue
        if getattr(workspace, "instance_id", None) != peer:
            continue
        owner_rows = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.workspace_id == workspace.id,
                schemas.WorkspaceGroupMember.role == OWNER,
                schemas.WorkspaceGroupMember.status == "active",
                schemas.WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )
        for owner in owner_rows:
            for member in get_active_members(owner.group_id):
                other = get_workspace_by_id(member.workspace_id) if member.workspace_id else None
                if is_local_workspace(other):
                    blocked.append(team_id)
                    break
            else:
                continue
            break
    return blocked
