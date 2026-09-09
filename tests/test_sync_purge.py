"""Real-database tests for the ordered purge helpers.

These run against a real SQLite file with ``PRAGMA foreign_keys=ON`` (set in
``conftest.py``), which is what makes the MySQL error-1451 failure reproducible
off MySQL. The handler tests elsewhere mock ``DbManager`` and so cannot catch
delete-ordering bugs at all.
"""

import os
from datetime import UTC, datetime

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from unittest.mock import patch  # noqa: E402

from sqlalchemy.exc import IntegrityError  # noqa: E402

from db import DbManager, schemas  # noqa: E402


@pytest.fixture
def real_db(tmp_path):
    """A real SQLite database built from the current models, with FKs enforced."""
    import db as db_mod
    from db import initialize_database

    url = f"sqlite:///{tmp_path / 'purge.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            yield
        finally:
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def _build_sync(*, with_soft_deleted_channel=True):
    """Create a workspace, a sync, its channels, and a post_meta row.

    The soft-deleted channel is the detail that broke the naive fix: handlers
    filter on ``deleted_at IS NULL``, but the row still references ``syncs``.
    """
    workspace = DbManager.create_record(
        schemas.Workspace(team_id="T_PURGE", workspace_name="Purge WS", bot_token="tok")
    )
    sync = DbManager.create_record(
        schemas.Sync(title="Purge Sync", sync_mode="group", publisher_workspace_id=workspace.id)
    )

    active = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=workspace.id,
            channel_id="C_ACTIVE",
            status="active",
            created_at=_now(),
        )
    )
    channels = [active]
    if with_soft_deleted_channel:
        channels.append(
            DbManager.create_record(
                schemas.SyncChannel(
                    sync_id=sync.id,
                    workspace_id=workspace.id,
                    channel_id="C_SOFT_DELETED",
                    status="active",
                    created_at=_now(),
                    deleted_at=_now(),
                )
            )
        )

    DbManager.create_record(schemas.PostMeta(post_id="P1", sync_channel_id=active.id, ts=1.0))

    return workspace, sync, channels


class TestPurgeSync:
    def test_deleting_the_sync_directly_violates_foreign_keys(self, real_db):
        """Documents the original bug: this is what the button used to do."""
        _, sync, _ = _build_sync()

        with pytest.raises(IntegrityError):
            DbManager.delete_records(schemas.Sync, [schemas.Sync.id == sync.id])

    def test_purge_removes_children_and_the_sync(self, real_db):
        import helpers

        _, sync, _ = _build_sync()

        helpers.purge_sync(sync.id)

        assert DbManager.find_records(schemas.Sync, [schemas.Sync.id == sync.id]) == []
        assert DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.sync_id == sync.id]) == []
        assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "P1"]) == []

    def test_purge_removes_soft_deleted_channels(self, real_db):
        """deleted_at only hides a row from the UI; it still blocks the parent delete."""
        import helpers

        _, sync, _ = _build_sync()

        helpers.purge_sync(sync.id)

        remaining = DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.channel_id == "C_SOFT_DELETED"])
        assert remaining == []

    def test_purge_is_idempotent(self, real_db):
        """A retry after a partial failure must converge, not raise."""
        import helpers

        _, sync, _ = _build_sync()

        helpers.purge_sync(sync.id)
        helpers.purge_sync(sync.id)

        assert DbManager.find_records(schemas.Sync, [schemas.Sync.id == sync.id]) == []

    def test_purge_of_unknown_sync_is_a_noop(self, real_db):
        import helpers

        helpers.purge_sync(999999)
        helpers.purge_sync(None)

    def test_purge_invalidates_the_channel_memberships_cache(self, real_db):
        """Without invalidation, fan-out keeps targeting deleted rows for up to 60s."""
        import helpers
        from helpers._cache import _cache_get

        _, sync, _ = _build_sync()

        assert helpers.find_channel_memberships("C_ACTIVE")
        assert _cache_get("channel_memberships:C_ACTIVE:active") is not None

        helpers.purge_sync(sync.id)

        assert _cache_get("channel_memberships:C_ACTIVE:active") is None
        assert helpers.find_channel_memberships("C_ACTIVE") == []


class TestPurgeSyncChannels:
    def test_removes_channels_and_post_meta_but_keeps_the_sync(self, real_db):
        import helpers

        _, sync, channels = _build_sync()

        helpers.purge_sync_channels([channels[0]])

        assert DbManager.find_records(schemas.Sync, [schemas.Sync.id == sync.id])
        assert DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.channel_id == "C_ACTIVE"]) == []
        assert DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "P1"]) == []

    def test_invalidates_the_cache(self, real_db):
        import helpers
        from helpers._cache import _cache_get

        _, _, channels = _build_sync()

        helpers.find_channel_memberships("C_ACTIVE")
        helpers.purge_sync_channels([channels[0]])

        assert _cache_get("channel_memberships:C_ACTIVE:active") is None


class TestPurgeWorkspace:
    def test_deleting_the_workspace_directly_violates_foreign_keys(self, real_db):
        """Documents the retention-purge bug that purge_workspace fixes."""
        workspace, _, _ = _build_sync()

        with pytest.raises(IntegrityError):
            DbManager.delete_records(schemas.Workspace, [schemas.Workspace.id == workspace.id])

    def test_purge_removes_the_workspace_and_its_published_syncs(self, real_db):
        import helpers

        workspace, sync, _ = _build_sync()

        helpers.purge_workspace(workspace.id)

        assert DbManager.find_records(schemas.Workspace, [schemas.Workspace.id == workspace.id]) == []
        assert DbManager.find_records(schemas.Sync, [schemas.Sync.id == sync.id]) == []
        assert DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.sync_id == sync.id]) == []

    def test_purge_is_idempotent(self, real_db):
        import helpers

        workspace, _, _ = _build_sync()

        helpers.purge_workspace(workspace.id)
        helpers.purge_workspace(workspace.id)

        assert DbManager.find_records(schemas.Workspace, [schemas.Workspace.id == workspace.id]) == []

    def test_purge_keeps_other_workspaces_memberships_and_nulls_the_inviter(self, real_db):
        """The inviter link is nulled; the other workspace's membership survives.

        workspace_group_members references workspaces.id through two columns, so
        deleting by invited_by_workspace_id would evict a legitimate member.
        """
        import helpers

        inviter = DbManager.create_record(schemas.Workspace(team_id="T_INVITER", workspace_name="Inviter"))
        invitee = DbManager.create_record(schemas.Workspace(team_id="T_INVITEE", workspace_name="Invitee"))
        group = DbManager.create_record(
            schemas.WorkspaceGroup(
                name="G",
                invite_code="ABC-123",
                status="active",
                created_at=_now(),
            )
        )
        own_membership = DbManager.create_record(
            schemas.WorkspaceGroupMember(
                group_id=group.id, workspace_id=inviter.id, status="active", role="owner", joined_at=_now()
            )
        )
        other_membership = DbManager.create_record(
            schemas.WorkspaceGroupMember(
                group_id=group.id,
                workspace_id=invitee.id,
                status="active",
                role="member",
                joined_at=_now(),
                invited_by_workspace_id=inviter.id,
            )
        )

        helpers.purge_workspace(inviter.id)

        assert DbManager.find_records(schemas.Workspace, [schemas.Workspace.id == inviter.id]) == []
        assert (
            DbManager.find_records(schemas.WorkspaceGroupMember, [schemas.WorkspaceGroupMember.id == own_membership.id])
            == []
        )
        # The group itself survives the purge of the workspace that created it.
        assert DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group.id])

        survived = DbManager.find_records(
            schemas.WorkspaceGroupMember, [schemas.WorkspaceGroupMember.id == other_membership.id]
        )
        assert len(survived) == 1
        assert survived[0].workspace_id == invitee.id
        assert survived[0].invited_by_workspace_id is None

    def test_purge_nulls_direct_sync_targets(self, real_db):
        import helpers

        publisher = DbManager.create_record(schemas.Workspace(team_id="T_PUB", workspace_name="Pub"))
        target = DbManager.create_record(schemas.Workspace(team_id="T_TGT", workspace_name="Target"))
        sync = DbManager.create_record(
            schemas.Sync(
                title="Direct",
                sync_mode="direct",
                publisher_workspace_id=publisher.id,
                target_workspace_id=target.id,
            )
        )

        helpers.purge_workspace(target.id)

        remaining = DbManager.find_records(schemas.Sync, [schemas.Sync.id == sync.id])
        assert len(remaining) == 1
        assert remaining[0].target_workspace_id is None
