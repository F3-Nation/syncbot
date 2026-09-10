"""Tests for group ownership rules: the invariant, promotion, departure, succession, disband."""

import os
from datetime import UTC, datetime

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from unittest.mock import patch  # noqa: E402

from db import DbManager, schemas  # noqa: E402


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database

    url = f"sqlite:///{tmp_path / 'roles.db'}"
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


def _workspace(team_id, *, installed=True):
    return DbManager.create_record(
        schemas.Workspace(
            team_id=team_id,
            workspace_name=f"WS {team_id}",
            bot_token="tok" if installed else None,
        )
    )


def _group(code="ZZZ-999"):
    return DbManager.create_record(
        schemas.WorkspaceGroup(name="Group", invite_code=code, status="active", created_at=_now())
    )


def _member(group, workspace, role="member", *, joined_offset=0, status="active", deleted=False, federated_id=None):
    return DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=workspace.id if workspace else None,
            federated_workspace_id=federated_id,
            status=status,
            role=role,
            joined_at=datetime(2026, 1, 1 + joined_offset, tzinfo=UTC).replace(tzinfo=None),
            deleted_at=_now() if deleted else None,
        )
    )


class TestOwnerInvariant:
    def test_self_heal_promotes_earliest_joined_member_when_no_owner_exists(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T1"), role="member", joined_offset=5)
        early = _member(group, _workspace("T2"), role="member", joined_offset=1)

        promoted = helpers.ensure_group_has_owner(group.id)

        assert promoted is not None
        assert promoted.id == early.id
        assert len(helpers.get_active_owners(group.id)) == 1

    def test_self_heal_is_a_noop_when_an_owner_exists(self, real_db):
        import helpers

        group = _group()
        owner = _member(group, _workspace("T1"), role="owner")
        _member(group, _workspace("T2"), role="member")

        assert helpers.ensure_group_has_owner(group.id) is None
        owners = helpers.get_active_owners(group.id)
        assert [o.id for o in owners] == [owner.id]

    def test_a_soft_deleted_owner_is_retained_not_missing(self, real_db):
        """An uninstalled owner must not silently lose the group during retention."""
        import helpers

        group = _group()
        _member(group, _workspace("T1"), role="owner", deleted=True)
        _member(group, _workspace("T2"), role="member")

        assert helpers.ensure_group_has_owner(group.id) is None
        assert helpers.get_active_owners(group.id) == []

    def test_self_heal_is_idempotent_across_containers(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T1"), role="member", joined_offset=1)
        _member(group, _workspace("T2"), role="member", joined_offset=2)

        helpers.ensure_group_has_owner(group.id)
        helpers.ensure_group_has_owner(group.id)

        assert len(helpers.get_active_owners(group.id)) == 1

    def test_federated_only_group_has_no_candidate(self, real_db):
        import helpers

        group = _group()
        fed = DbManager.create_record(
            schemas.FederatedWorkspace(
                instance_id="abc",
                name="Remote",
                webhook_url="https://example.test/hook",
                public_key="key",
                created_at=_now(),
            )
        )
        _member(group, None, role="member", federated_id=fed.id)

        assert helpers.ensure_group_has_owner(group.id) is None


class TestDepartureRules:
    def test_plain_member_may_always_leave(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T1"), role="owner")
        member_ws = _workspace("T2")
        _member(group, member_ws, role="member")

        assert helpers.can_workspace_leave(group.id, member_ws.id) == (True, "")

    def test_sole_owner_with_other_members_is_rejected(self, real_db):
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")
        _member(group, _workspace("T2"), role="member")

        allowed, reason = helpers.can_workspace_leave(group.id, owner_ws.id)
        assert allowed is False
        assert reason == "sole_owner"

    def test_owner_may_leave_when_a_second_owner_remains(self, real_db):
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")
        _member(group, _workspace("T2"), role="owner")

        assert helpers.can_workspace_leave(group.id, owner_ws.id)[0] is True

    def test_sole_owner_who_is_sole_member_may_leave(self, real_db):
        """Leaving disbands the group, and there is nobody to promote."""
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")

        assert helpers.can_workspace_leave(group.id, owner_ws.id)[0] is True

    def test_sole_owner_with_only_federated_members_may_leave(self, real_db):
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")
        fed = DbManager.create_record(
            schemas.FederatedWorkspace(
                instance_id="abc",
                name="Remote",
                webhook_url="https://example.test/hook",
                public_key="key",
                created_at=_now(),
            )
        )
        _member(group, None, role="member", federated_id=fed.id)

        assert helpers.can_workspace_leave(group.id, owner_ws.id)[0] is True


class TestPromotionEligibility:
    def test_pending_and_federated_members_are_not_promotable(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T1"), role="owner")
        local = _member(group, _workspace("T2"), role="member")
        _member(group, _workspace("T3"), role="member", status="pending")
        fed = DbManager.create_record(
            schemas.FederatedWorkspace(
                instance_id="abc",
                name="Remote",
                webhook_url="https://example.test/hook",
                public_key="key",
                created_at=_now(),
            )
        )
        _member(group, None, role="member", federated_id=fed.id)

        promotable = helpers.get_promotable_members(group.id)
        assert [m.id for m in promotable] == [local.id]

    def test_existing_owners_are_not_promotable(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T1"), role="owner")

        assert helpers.get_promotable_members(group.id) == []


class TestSuccessionLadder:
    def test_rung_one_promotes_earliest_active_local_member(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T2"), role="member", joined_offset=5)
        early = _member(group, _workspace("T3"), role="member", joined_offset=1)

        promoted = helpers.succeed_ownership(group.id, departing_workspace_id=999)

        assert promoted.id == early.id

    def test_rung_two_falls_back_to_the_primary_workspace(self, real_db):
        """An authority escalation: the primary workspace is usually not a member."""
        import helpers

        group = _group()
        primary = _workspace("T_PRIMARY")
        fed = DbManager.create_record(
            schemas.FederatedWorkspace(
                instance_id="abc",
                name="Remote",
                webhook_url="https://example.test/hook",
                public_key="key",
                created_at=_now(),
            )
        )
        _member(group, None, role="member", federated_id=fed.id)

        with patch.dict(os.environ, {"PRIMARY_WORKSPACE": "T_PRIMARY"}):
            promoted = helpers.succeed_ownership(group.id)

        assert promoted is not None
        assert promoted.workspace_id == primary.id
        assert DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group.id])

    def test_rung_two_is_skipped_when_the_primary_workspace_is_not_installed(self, real_db):
        import helpers

        group = _group()
        _workspace("T_PRIMARY", installed=False)

        with patch.dict(os.environ, {"PRIMARY_WORKSPACE": "T_PRIMARY"}):
            assert helpers.succeed_ownership(group.id) is None

        assert DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group.id]) == []

    def test_rung_three_disbands_when_primary_workspace_is_unset(self, real_db):
        """PRIMARY_WORKSPACE is optional and often unset, so this is a real outcome."""
        import helpers

        group = _group()

        with patch.dict(os.environ, {"PRIMARY_WORKSPACE": ""}):
            assert helpers.succeed_ownership(group.id) is None

        assert DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group.id]) == []

    def test_succession_is_a_noop_when_an_owner_still_exists(self, real_db):
        import helpers

        group = _group()
        owner = _member(group, _workspace("T1"), role="owner")

        assert helpers.succeed_ownership(group.id) is None
        assert [o.id for o in helpers.get_active_owners(group.id)] == [owner.id]


class TestPurgeRetainsOrTransfersOwnership:
    def test_purging_the_last_owner_promotes_a_successor(self, real_db):
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")
        successor_ws = _workspace("T2")
        successor = _member(group, successor_ws, role="member")

        helpers.purge_workspace(owner_ws.id)

        owners = helpers.get_active_owners(group.id)
        assert [o.id for o in owners] == [successor.id]

    def test_purging_a_plain_member_does_not_change_ownership(self, real_db):
        import helpers

        group = _group()
        owner = _member(group, _workspace("T1"), role="owner")
        member_ws = _workspace("T2")
        _member(group, member_ws, role="member")

        helpers.purge_workspace(member_ws.id)

        assert [o.id for o in helpers.get_active_owners(group.id)] == [owner.id]


class TestDisbandGates:
    def _sync(self, group, publisher):
        return DbManager.create_record(
            schemas.Sync(title="S", sync_mode="group", group_id=group.id, publisher_workspace_id=publisher.id)
        )

    def test_sole_owner_and_sole_publisher_may_disband(self, real_db):
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")
        _member(group, _workspace("T2"), role="member")
        self._sync(group, owner_ws)

        assert helpers.can_disband(group.id, owner_ws.id) == (True, "")

    def test_a_co_owner_blocks_disband(self, real_db):
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")
        _member(group, _workspace("T2"), role="owner")

        allowed, reason = helpers.can_disband(group.id, owner_ws.id)
        assert allowed is False
        assert reason == "co_owner_exists"

    def test_another_workspaces_published_sync_blocks_disband(self, real_db):
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")
        other_ws = _workspace("T2")
        _member(group, other_ws, role="member")
        self._sync(group, owner_ws)
        self._sync(group, other_ws)

        allowed, reason = helpers.can_disband(group.id, owner_ws.id)
        assert allowed is False
        assert reason == "other_publishers"

    def test_a_receive_only_subscriber_that_publishes_its_own_sync_still_blocks(self, real_db):
        """The case a direction-based check would wrongly allow."""
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")
        subscriber_ws = _workspace("T2")
        _member(group, subscriber_ws, role="member")

        broadcast = self._sync(group, owner_ws)
        DbManager.create_record(
            schemas.SyncChannel(
                sync_id=broadcast.id,
                workspace_id=subscriber_ws.id,
                channel_id="C_RECEIVE_ONLY",
                status="active",
                created_at=_now(),
            )
        )
        # The subscriber also publishes a sync of its own into the same group.
        self._sync(group, subscriber_ws)

        assert helpers.can_disband(group.id, owner_ws.id) == (False, "other_publishers")

    def test_a_non_owner_may_not_disband(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T1"), role="owner")
        member_ws = _workspace("T2")
        _member(group, member_ws, role="member")

        allowed, reason = helpers.can_disband(group.id, member_ws.id)
        assert allowed is False
        assert reason == "not_owner"
