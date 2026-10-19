"""Tests for group ownership rules: the invariant, promotion, departure, succession, disband."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from unittest.mock import MagicMock, patch  # noqa: E402

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
            from federation import core as federation_core

            federation_core._INSTANCE_ID = None
            federation_core._cached_private_key = None
            federation_core._cached_public_pem = None
            federation_core.get_or_create_instance_keypair()
            yield
        finally:
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def _workspace(team_id, *, installed=True):
    from federation.core import get_instance_id

    return DbManager.create_record(
        schemas.Workspace(
            team_id=team_id,
            workspace_name=f"WS {team_id}",
            instance_id=get_instance_id(),
            deleted_at=None if installed else _now(),
        )
    )


def _group(code="ZZZ-999"):
    return DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Group",
            invite_code=code,
            status="active",
            created_at=_now(),
            uid=str(uuid4()),
        )
    )


def _member(group, workspace, role="member", *, joined_offset=0, status="active", deleted=False):
    return DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group.id,
            workspace_id=workspace.id if workspace else None,
            status=status,
            role=role,
            joined_at=datetime(2026, 1, 1 + joined_offset, tzinfo=UTC).replace(tzinfo=None),
            deleted_at=_now() if deleted else None,
        )
    )


def _federated_peer():
    return DbManager.create_record(
        schemas.Instance(
            instance_id="abc",
            name="Remote",
            webhook_url="https://example.test/hook",
            public_key="key",
            created_at=_now(),
        )
    )


def _stub_workspace(team_id, fed):
    return DbManager.create_record(
        schemas.Workspace(
            team_id=team_id,
            workspace_name=f"Stub {team_id}",
            instance_id=fed.instance_id,
        )
    )


class TestOwnerInvariant:
    def test_a_soft_deleted_owner_is_retained_not_missing(self, real_db):
        """An uninstalled owner must not silently lose the group during retention."""
        import helpers

        group = _group()
        owner = _member(group, _workspace("T1"), role="owner", deleted=True)
        _member(group, _workspace("T2"), role="member")

        assert helpers.get_active_owners(group.id) == []
        assert [row.id for row in helpers.get_retained_owners(group.id)] == [owner.id]

    def test_succession_waits_while_an_uninstalled_owner_is_retained(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T1"), role="owner", deleted=True)
        _member(group, _workspace("T2"), role="member")
        outsider = _workspace("T_PRIMARY")

        with patch.dict(os.environ, {"PRIMARY_WORKSPACE": "T_PRIMARY"}):
            assert helpers.succeed_ownership(group.id) is None

        assert helpers.get_active_owners(group.id) == []
        assert [row.role for row in helpers.get_active_members(group.id)] == ["member"]
        assert (
            DbManager.find_records(
                schemas.WorkspaceGroupMember,
                [
                    schemas.WorkspaceGroupMember.group_id == group.id,
                    schemas.WorkspaceGroupMember.workspace_id == outsider.id,
                ],
            )
            == []
        )


class TestDepartureRules:
    def test_plain_member_may_always_leave(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T1"), role="owner")
        member_ws = _workspace("T2")
        _member(group, member_ws, role="member")

        assert helpers.can_workspace_leave(group.id, member_ws.id) == (True, "")

    def test_remaining_member_may_leave_while_an_uninstalled_owner_is_retained(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T1"), role="owner", deleted=True)
        member_ws = _workspace("T2")
        _member(group, member_ws, role="member")

        assert helpers.can_workspace_leave(group.id, member_ws.id) == (True, "")

    def test_remaining_member_leave_confirm_keeps_group_during_owner_retention(self, real_db):
        import json

        import helpers
        from handlers.group_manage import handle_leave_group_confirm

        group = _group()
        owner = _member(group, _workspace("T1"), role="owner", deleted=True)
        member_ws = _workspace("T2")
        _member(group, member_ws, role="member")

        body = {
            "view": {
                "team_id": member_ws.team_id,
                "private_metadata": json.dumps({"group_id": group.id}),
            },
            "user": {"id": "U1", "team_id": member_ws.team_id},
            "team": {"id": member_ws.team_id},
        }
        with (
            patch("handlers.group_manage.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
            patch("handlers.group_manage.helpers.get_workspace_record", return_value=member_ws),
            patch("handlers.group_manage.helpers.format_admin_label", return_value=("Ada", "Ada from Workspace B")),
            patch("handlers.group_manage.helpers.resolve_workspace_name", return_value="Workspace B"),
            patch("handlers.group_manage.builders.refresh_home_tab_for_workspace"),
            patch("handlers._common._close_modal_done"),
            patch("federation.replicate.replicate_group_leave"),
        ):
            handle_leave_group_confirm(body, MagicMock(), MagicMock(), context={})

        assert helpers.get_active_members(group.id) == []
        assert DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group.id])
        assert [row.id for row in helpers.get_retained_owners(group.id)] == [owner.id]

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

    def test_sole_owner_who_is_sole_member_may_not_leave(self, real_db):
        """Home shows Disband Group instead of Leave Group."""
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")

        allowed, reason = helpers.can_workspace_leave(group.id, owner_ws.id)
        assert allowed is False
        assert reason == "sole_owner"

    def test_sole_owner_with_only_federated_members_may_not_leave(self, real_db):
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")
        fed = _federated_peer()
        _member(group, _stub_workspace("T_REMOTE", fed), role="member")

        allowed, reason = helpers.can_workspace_leave(group.id, owner_ws.id)
        assert allowed is False
        assert reason == "sole_owner"


class TestPromotionEligibility:
    def test_pending_and_federated_members_are_not_promotable(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T1"), role="owner")
        local = _member(group, _workspace("T2"), role="member")
        _member(group, _workspace("T3"), role="member", status="pending")
        fed = _federated_peer()
        _member(group, _stub_workspace("T_REMOTE", fed), role="member")

        promotable = helpers.get_promotable_members(group.id)
        assert [m.id for m in promotable] == [local.id]

    def test_existing_owners_are_not_promotable(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T1"), role="owner")

        assert helpers.get_promotable_members(group.id) == []


class TestSuccessionLadder:
    def test_promotes_the_earliest_joined_remaining_local_member(self, real_db):
        import helpers

        group = _group()
        _member(group, _workspace("T2"), role="member", joined_offset=5)
        early = _member(group, _workspace("T3"), role="member", joined_offset=1)

        promoted = helpers.succeed_ownership(group.id, departing_workspace_id=999)

        assert promoted.id == early.id

    def test_primary_workspace_is_not_added_when_it_is_not_a_member(self, real_db):
        import helpers

        group = _group()
        primary = _workspace("T_PRIMARY")
        fed = _federated_peer()
        _member(group, _stub_workspace("T_REMOTE", fed), role="member")

        with patch.dict(os.environ, {"PRIMARY_WORKSPACE": "T_PRIMARY"}):
            assert helpers.succeed_ownership(group.id) is None

        assert DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group.id]) == []
        assert (
            DbManager.find_records(
                schemas.WorkspaceGroupMember,
                [schemas.WorkspaceGroupMember.workspace_id == primary.id],
            )
            == []
        )

    def test_disbands_when_no_local_member_remains(self, real_db):
        import helpers

        group = _group()

        assert helpers.succeed_ownership(group.id) is None
        assert DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group.id]) == []

    def test_primary_member_is_promoted_only_when_it_joined_earliest(self, real_db):
        import helpers

        group = _group()
        primary = _member(group, _workspace("T_PRIMARY"), role="member", joined_offset=5)
        early = _member(group, _workspace("T_EARLY"), role="member", joined_offset=1)

        with patch.dict(os.environ, {"PRIMARY_WORKSPACE": "T_PRIMARY"}):
            promoted = helpers.succeed_ownership(group.id)

        assert promoted.id == early.id
        assert promoted.id != primary.id

    def test_longest_standing_member_may_already_be_primary(self, real_db):
        import helpers

        group = _group()
        primary = _member(group, _workspace("T_PRIMARY"), role="member", joined_offset=1)
        _member(group, _workspace("T_LATE"), role="member", joined_offset=5)

        with patch.dict(os.environ, {"PRIMARY_WORKSPACE": "T_PRIMARY"}):
            promoted = helpers.succeed_ownership(group.id)

        assert promoted.id == primary.id

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

    def test_purging_the_last_workspace_disbands_the_group(self, real_db):
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")
        outsider = _workspace("T_PRIMARY")

        with patch.dict(os.environ, {"PRIMARY_WORKSPACE": "T_PRIMARY"}):
            helpers.purge_workspace(owner_ws.id)

        assert DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.id == group.id]) == []
        assert (
            DbManager.find_records(
                schemas.WorkspaceGroupMember,
                [schemas.WorkspaceGroupMember.workspace_id == outsider.id],
            )
            == []
        )


class TestDisbandGates:
    def _sync(self, group, publisher):
        sync = DbManager.create_record(
            schemas.Sync(
                title="S",
                sync_mode="group",
                group_id=group.id,
                uid=str(uuid4()),
            )
        )
        DbManager.create_record(
            schemas.SyncChannel(
                sync_id=sync.id,
                workspace_id=publisher.id,
                channel_id=f"C_{publisher.team_id}",
                status="active",
                publishes=True,
                subscribes=True,
                created_at=_now(),
            )
        )
        return sync

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

    def test_a_retained_uninstalled_co_owner_blocks_disband(self, real_db):
        import helpers

        group = _group()
        owner_ws = _workspace("T1")
        _member(group, owner_ws, role="owner")
        _member(group, _workspace("T2"), role="owner", deleted=True)

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


class TestConnectionOwnerGate:
    def test_drop_blocked_when_owner_of_group_with_peer_stub(self, real_db):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        import helpers
        from db import DbManager, schemas
        from federation.core import public_key_fingerprint

        private = Ed25519PrivateKey.generate()
        public = (
            private.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode()
        )
        peer = DbManager.create_record(
            schemas.Instance(
                instance_id=public_key_fingerprint(public),
                public_key=public,
                webhook_url="https://peer.example/api/federation",
                status="active",
                trust_status="trusted",
                created_at=_now(),
            )
        )
        local = _workspace("T1")
        stub = DbManager.create_record(
            schemas.Workspace(team_id="T2", workspace_name="Remote", instance_id=peer.instance_id)
        )
        group = _group("AAA-111")
        _member(group, local, role="owner")
        _member(group, stub, role="member")

        blocked = helpers.get_owner_ids_blocking_allowlist_drop(peer.instance_id, [local.id])
        assert blocked == [local.id]

        other = _workspace("T3")
        local_only = _group("BBB-222")
        _member(local_only, local, role="owner")
        _member(local_only, other, role="member")
        assert helpers.get_owner_ids_blocking_allowlist_drop(peer.instance_id, [other.id]) == []

    def test_stub_pause_blocked_when_owner_of_mixed_group(self, real_db):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        import helpers
        from db import DbManager, schemas
        from federation.core import public_key_fingerprint

        private = Ed25519PrivateKey.generate()
        public = (
            private.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode()
        )
        peer = DbManager.create_record(
            schemas.Instance(
                instance_id=public_key_fingerprint(public),
                public_key=public,
                webhook_url="https://peer.example/api/federation",
                status="active",
                trust_status="trusted",
                created_at=_now(),
            )
        )
        local = _workspace("T1")
        stub = DbManager.create_record(
            schemas.Workspace(team_id="T2", workspace_name="Remote", instance_id=peer.instance_id)
        )
        group = _group("CCC-333")
        _member(group, stub, role="owner")
        _member(group, local, role="member")

        blocked = helpers.get_owner_team_ids_blocking_stub_pause(peer.instance_id, [stub.team_id])
        assert blocked == [stub.team_id]
