"""Group ownership rules.

``role`` on ``workspace_group_members`` is descriptive except for ``owner``,
which is load-bearing: owners may promote another workspace, may leave only
while another active owner remains, and may disband a group they solely own and
solely publish into.

The invariant is **every active group has at least one active owner** — not
exactly one. Multiple owners is a normal, legal state. It is a per-group
aggregate, not a row property, so no database constraint can express it; it
lives here plus the backfill in migration ``003_group_roles``.

Ownership is lost only by choice. Uninstall, token revocation, and every other
offline event leave it intact — only a voluntary departure or the permanent
retention purge can move it.

These are workspace-level gates that layer *on top of*
``helpers.is_workspace_manager``. Both must pass; neither replaces the other.

Imports submodules only, per the import-direction constraint in
``helpers/sync_cleanup.py``.
"""

import logging
import os
from datetime import UTC, datetime

import constants
from db import DbManager, schemas

_logger = logging.getLogger(__name__)

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
        if member.workspace_id and member.workspace_id != exclude_workspace_id and member.role != OWNER
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


def ensure_group_has_owner(group_id: int) -> schemas.WorkspaceGroupMember | None:
    """Self-heal a group that has no owner at all. Returns the promoted member, if any.

    Belt-and-suspenders behind migration ``003_group_roles``, which is the real
    fix. At runtime, ownership can only reach zero through paths that are already
    guarded — voluntary departure requires another owner, and purge runs the
    succession ladder — so this only fires on legacy pre-migration data.

    It runs in the Home-tab load path, so it is a write-on-read and must be
    tightly constrained:

    * Fires only when there are **zero active-or-retained** owners, so a
      soft-deleted (uninstalled) owner blocks it.
    * Idempotent, so a double-execution across warm Lambda containers
      re-promotes the same row rather than creating two owners.
    """
    if not group_id:
        return None

    if get_retained_owners(group_id):
        return None

    members = get_active_members(group_id)
    candidate = _promotion_candidate(members)
    if not candidate:
        # Federated-only group; nothing local to promote. The succession ladder
        # handles this when it is reached through a purge.
        return None

    # Re-check under the same read: if any owner appeared, do nothing. Two
    # containers racing here both select the same earliest-joined row, so the
    # duplicate write is the same write.
    if get_retained_owners(group_id):
        return None

    _set_role(candidate.id, OWNER)
    _logger.info(
        "group_owner_self_healed",
        extra={"group_id": group_id, "member_id": candidate.id, "workspace_id": candidate.workspace_id},
    )
    return candidate


def can_workspace_leave(group_id: int, workspace_id: int) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for *workspace_id* leaving *group_id*.

    Plain members may always leave. An owner may leave only while at least one
    other active owner remains — except when it is also the last member, in
    which case leaving disbands the group and there is nobody to promote.
    """
    if not is_workspace_owner(group_id, workspace_id):
        return True, ""

    members = get_active_members(group_id)
    others = [member for member in members if member.workspace_id != workspace_id]

    if not others:
        # Sole owner and sole member: leaving deletes the group.
        return True, ""

    other_owners = [member for member in others if member.role == OWNER]
    if other_owners:
        return True, ""

    local_others = [member for member in others if member.workspace_id]
    if not local_others:
        # Only federated members remain; destroying a working cross-instance
        # sync to enforce local ownership would be worse. Succession handles it.
        return True, ""

    return False, "sole_owner"


def succeed_ownership(group_id: int, departing_workspace_id: int | None = None) -> schemas.WorkspaceGroupMember | None:
    """Apply the succession ladder after the last owner is permanently removed.

    Only called when ownership is lost involuntarily and permanently — the
    retention purge — or when a departure leaves no local member to promote.

    1. The earliest-joined active local member, excluding the departing owner.
    2. Otherwise the workspace named by ``PRIMARY_WORKSPACE``, if set and
       installed. This is the instance operator, already privileged for
       backup/restore and DB reset.
    3. Otherwise disband the group.

    Rung 2 is an authority escalation: the primary workspace is usually not a
    member, so taking ownership creates a membership row granting it visibility
    into syncs and channel names it never joined. It is logged for that reason.
    """
    if not group_id:
        return None

    if get_active_owners(group_id):
        return None

    members = get_active_members(group_id)
    candidate = _promotion_candidate(members, exclude_workspace_id=departing_workspace_id)
    if candidate:
        _set_role(candidate.id, OWNER)
        _logger.info(
            "group_owner_succeeded",
            extra={
                "group_id": group_id,
                "member_id": candidate.id,
                "workspace_id": candidate.workspace_id,
                "reason": "earliest_active_local_member",
            },
        )
        return candidate

    primary = _primary_workspace_record()
    if primary:
        existing = [member for member in members if member.workspace_id == primary.id]
        if existing:
            _set_role(existing[0].id, OWNER)
            promoted = existing[0]
        else:
            promoted = DbManager.create_record(
                schemas.WorkspaceGroupMember(
                    group_id=group_id,
                    workspace_id=primary.id,
                    status="active",
                    role=OWNER,
                    joined_at=datetime.now(UTC),
                )
            )
        _logger.info(
            "group_owner_succeeded",
            extra={
                "group_id": group_id,
                "member_id": promoted.id,
                "workspace_id": primary.id,
                "reason": "primary_workspace",
            },
        )
        return promoted

    # Rung 3. PRIMARY_WORKSPACE is optional and frequently unset, so this is a
    # real outcome rather than a theoretical one.
    _logger.info("group_disbanded_no_successor", extra={"group_id": group_id})
    DbManager.delete_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group_id])
    return None


def _primary_workspace_record() -> schemas.Workspace | None:
    """Return the installed PRIMARY_WORKSPACE, or None if unset or not installed."""
    team_id = (os.environ.get(constants.PRIMARY_WORKSPACE) or "").strip()
    if not team_id:
        return None

    matches = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.team_id == team_id,
            schemas.Workspace.deleted_at.is_(None),
        ],
    )
    for workspace in matches:
        if workspace.bot_token:
            return workspace
    return None


def get_promotable_members(group_id: int) -> list[schemas.WorkspaceGroupMember]:
    """Return members an owner may promote: active, local, and not already owners.

    Pending invitees and federated members are excluded.
    """
    return [member for member in get_active_members(group_id) if member.workspace_id and member.role != OWNER]


def can_disband(group_id: int, workspace_id: int) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for *workspace_id* disbanding *group_id*.

    Two conditions must both hold:

    1. The acting workspace is the group's **only** owner. Co-owners have equal
       standing and should not be dissolved out of a group by a peer. This does
       not deadlock, because self-demotion is allowed while another owner
       remains.
    2. The acting workspace is the group's **only publisher**. Otherwise a
       disband would destroy syncs another workspace authored.

    Condition 2 is deliberately **publisher-based, not participation-based**. A
    subscribe-only channel describes its role inside one sync and says
    nothing about whether it published a sync of its own into the same group.
    """
    owners = get_active_owners(group_id)
    if not any(owner.workspace_id == workspace_id for owner in owners):
        return False, "not_owner"
    if len(owners) > 1:
        return False, "co_owner_exists"

    syncs = DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group_id])
    other_publishers = {
        sync.publisher_workspace_id
        for sync in syncs
        if sync.publisher_workspace_id and sync.publisher_workspace_id != workspace_id
    }
    if other_publishers:
        return False, "other_publishers"

    return True, ""


def get_other_publisher_workspace_ids(group_id: int, workspace_id: int) -> list[int]:
    """Return workspaces other than *workspace_id* that publish a sync into *group_id*."""
    syncs = DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group_id])
    return sorted(
        {
            sync.publisher_workspace_id
            for sync in syncs
            if sync.publisher_workspace_id and sync.publisher_workspace_id != workspace_id
        }
    )
