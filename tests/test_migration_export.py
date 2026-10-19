"""Data migration export looks up Workspaces by integer primary key."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from helpers.export_import import build_migration_export, import_migration_data


class TestBuildMigrationExportWorkspaceLookup:
    def test_finds_workspace_by_integer_pk(self, caplog):
        import logging

        workspace = SimpleNamespace(
            id=42,
            team_id="T1",
            workspace_name="Workspace A",
            deleted_at=None,
        )
        with (
            patch("helpers.export_import.get_workspace_by_id", return_value=workspace) as get_ws,
            patch("helpers.export_import.DbManager.find_records", return_value=[]),
            patch("helpers.export_import.os.environ.get", return_value=""),
            caplog.at_level(logging.DEBUG, logger="syncbot"),
        ):
            payload = build_migration_export(42, include_source_instance=False)

        get_ws.assert_called_with(42)
        assert payload["workspace"] == {"team_id": "T1", "workspace_name": "Workspace A"}
        assert payload["syncs"] == []
        assert payload["groups"] == []
        assert payload["version"] == 1
        assert payload["syncbot_version"]
        assert "federation_file_parts" not in payload
        assert any(
            record.message == "migration_export" and record.__dict__.get("team_id") == "T1" for record in caplog.records
        )

    def test_raises_when_workspace_missing(self):
        with (
            patch("helpers.export_import.get_workspace_by_id", return_value=None),
            pytest.raises(ValueError, match="Workspace not found"),
        ):
            build_migration_export(99)

    def test_exports_channel_participation_and_posting_identity(self):
        workspace = SimpleNamespace(id=42, team_id="T1", workspace_name="Workspace A", deleted_at=None)
        sync = SimpleNamespace(
            id=9,
            uid="sync-uid",
            group_id=3,
            title="Announcements",
            sync_mode="group",
        )
        group = SimpleNamespace(id=3, uid="group-uid")
        sync_channel = SimpleNamespace(
            id=7,
            sync_id=9,
            workspace_id=42,
            channel_id="C1",
            status="active",
            publishes=True,
            subscribes=False,
            reaction_style="direct_only",
        )
        post_meta = SimpleNamespace(
            post_id="post-1",
            ts=123.0,
            kind="message",
            parent_post_id=None,
            reaction=None,
            source_user_id="U1",
            source_workspace_id=42,
            posted_as_user_id="U2",
        )

        def find_records(model, _filters):
            return {
                "WorkspaceGroupMember": [],
                "SyncChannel": [sync_channel],
                "PostMeta": [post_meta],
                "UserDirectory": [],
                "UserMapping": [],
            }.get(model.__name__, [])

        with (
            patch("helpers.export_import.get_workspace_by_id", return_value=workspace),
            patch("helpers.export_import.DbManager.find_records", side_effect=find_records),
            patch(
                "helpers.export_import.DbManager.get_record",
                side_effect=lambda model, *args, **kwargs: group if model.__name__ == "WorkspaceGroup" else sync,
            ),
        ):
            payload = build_migration_export(42, include_source_instance=False)

        assert payload["sync_channels"] == [
            {
                "sync_uid": "sync-uid",
                "team_id": "T1",
                "channel_id": "C1",
                "channel_name": None,
                "status": "active",
                "publishes": True,
                "subscribes": False,
                "reaction_style": "direct_only",
            }
        ]
        assert payload["post_meta"]["sync-uid:C1"][0]["posted_as_user_id"] == "U2"

    def test_exports_post_meta_for_peer_stub_channels(self):
        local = SimpleNamespace(id=42, team_id="T1", workspace_name="Workspace A", deleted_at=None)
        peer = SimpleNamespace(id=99, team_id="T-PEER", workspace_name="Workspace B", deleted_at=None)
        sync = SimpleNamespace(id=9, uid="sync-uid", group_id=3, title="Announcements", sync_mode="group")
        group = SimpleNamespace(id=3, uid="group-uid")
        local_sc = SimpleNamespace(
            id=7,
            sync_id=9,
            workspace_id=42,
            channel_id="C1",
            channel_name="announcements",
            status="active",
            publishes=True,
            subscribes=True,
            reaction_style="hybrid",
        )
        peer_sc = SimpleNamespace(
            id=8,
            sync_id=9,
            workspace_id=99,
            channel_id="C-PEER",
            channel_name="general",
            status="active",
            publishes=True,
            subscribes=True,
            reaction_style="hybrid",
        )
        local_pm = SimpleNamespace(
            post_id="post-1",
            ts="10.000001",
            kind="message",
            parent_post_id=None,
            reaction=None,
            source_user_id="U1",
            source_workspace_id=42,
            posted_as_user_id=None,
        )
        peer_pm = SimpleNamespace(
            post_id="post-1",
            ts="20.000002",
            kind="message",
            parent_post_id=None,
            reaction=None,
            source_user_id="U1",
            source_workspace_id=42,
            posted_as_user_id="U-PEER",
        )

        def find_records(model, filters):
            name = model.__name__
            if name == "SyncChannel":
                return [local_sc, peer_sc]
            if name == "PostMeta":
                clause = filters[0]
                sync_channel_id = clause.right.value
                return [local_pm] if sync_channel_id == 7 else [peer_pm]
            return []

        def get_ws(pk):
            return local if pk == 42 else peer

        with (
            patch("helpers.export_import.get_workspace_by_id", side_effect=get_ws),
            patch("helpers.export_import.DbManager.find_records", side_effect=find_records),
            patch(
                "helpers.export_import.DbManager.get_record",
                side_effect=lambda model, *args, **kwargs: group if model.__name__ == "WorkspaceGroup" else sync,
            ),
        ):
            payload = build_migration_export(42, include_source_instance=False)

        channels = {(row["team_id"], row["channel_id"]) for row in payload["sync_channels"]}
        assert channels == {("T1", "C1"), ("T-PEER", "C-PEER")}
        by_channel = {row["channel_id"]: row for row in payload["sync_channels"]}
        assert by_channel["C1"]["channel_name"] == "announcements"
        assert by_channel["C-PEER"]["channel_name"] == "general"
        assert payload["post_meta"]["sync-uid:C1"][0]["ts"] == "10.000001"
        assert payload["post_meta"]["sync-uid:C-PEER"][0]["ts"] == "20.000002"
        assert payload["post_meta"]["sync-uid:C-PEER"][0]["posted_as_user_id"] == "U-PEER"


def test_import_defaults_legacy_channel_participation_and_posting_identity():
    data = {
        "workspace": {"team_id": "T1"},
        "groups": [{"uid": "group-uid", "name": "Group", "role": "owner"}],
        "syncs": [{"uid": "sync-uid", "group_uid": "group-uid", "title": "S1"}],
        "sync_channels": [
            {"sync_uid": "sync-uid", "channel_id": "C1"},
            {
                "sync_uid": "sync-uid",
                "channel_id": "C2",
                "publishes": False,
                "subscribes": True,
                "reaction_style": "direct_only",
                "reaction_direction": "receive",
            },
        ],
        "post_meta": {"sync-uid:C1": [{"post_id": "post-1", "ts": 100.0}]},
    }
    created = []

    def capture(record):
        if type(record).__name__ == "WorkspaceGroup":
            record.id = 3
        elif type(record).__name__ == "Sync":
            record.id = 1
        elif type(record).__name__ == "SyncChannel":
            record.id = 2
        created.append(record)
        return record

    with (
        patch("helpers.export_import.DbManager.find_records", return_value=[]),
        patch("helpers.export_import.DbManager.create_record", side_effect=capture),
        patch("helpers.export_import.DbManager.delete_records"),
        patch("helpers.export_import.get_workspace_by_id", return_value=None),
    ):
        import_migration_data(data, 42, 3, team_id_to_workspace_id={"T1": 42})

    sync_channels = [row for row in created if type(row).__name__ == "SyncChannel"]
    sync_channel = next(row for row in sync_channels if row.channel_id == "C1")
    configured_channel = next(row for row in sync_channels if row.channel_id == "C2")
    post_meta = next(row for row in created if type(row).__name__ == "PostMeta")
    assert sync_channel.publishes is True
    assert sync_channel.subscribes is True
    assert configured_channel.publishes is False
    assert configured_channel.subscribes is True
    assert configured_channel.reaction_style == "direct_only"
    assert post_meta.posted_as_user_id is None


def test_import_skips_unknown_peer_group_members():
    data = {
        "source_instance": {"instance_id": "peer-fp"},
        "workspace": {"team_id": "T1"},
        "groups": [
            {
                "uid": "group-uid",
                "name": "Group",
                "role": "owner",
                "member_team_ids": ["T-PEER"],
                "member_workspaces": [{"team_id": "T-PEER", "workspace_name": "Workspace B"}],
            }
        ],
        "syncs": [{"uid": "sync-uid", "group_uid": "group-uid", "title": "S1"}],
        "sync_channels": [
            {"sync_uid": "sync-uid", "team_id": "T1", "channel_id": "C1"},
            {"sync_uid": "sync-uid", "team_id": "T-PEER", "channel_id": "C-PEER"},
        ],
        "post_meta": {},
    }
    created = []

    def find_records(model, _filters):
        if model.__name__ == "Instance":
            return [SimpleNamespace(instance_id="peer-fp", private_key_encrypted=None)]
        return []

    def capture(record):
        if type(record).__name__ == "WorkspaceGroup":
            record.id = 3
        elif type(record).__name__ == "Sync":
            record.id = 1
        elif type(record).__name__ == "SyncChannel":
            record.id = 2
        elif type(record).__name__ == "WorkspaceGroupMember":
            record.id = 8
        created.append(record)
        return record

    with (
        patch("helpers.export_import.DbManager.find_records", side_effect=find_records),
        patch("helpers.export_import.DbManager.create_record", side_effect=capture),
        patch("helpers.export_import.DbManager.delete_records"),
        patch("helpers.workspace_kind.ensure_stub_workspace") as ensure_stub,
        patch("helpers.export_import.get_workspace_by_id", return_value=None),
    ):
        import_migration_data(data, 42, 3, team_id_to_workspace_id={"T1": 42})

    ensure_stub.assert_not_called()
    members = [row for row in created if type(row).__name__ == "WorkspaceGroupMember"]
    assert {row.workspace_id for row in members} == {42}
    channels = [row for row in created if type(row).__name__ == "SyncChannel"]
    assert {row.workspace_id for row in channels} == {42}


def test_import_without_source_instance_skips_unknown_peer_channels():
    data = {
        "workspace": {"team_id": "T1"},
        "groups": [
            {
                "uid": "group-uid",
                "name": "Group",
                "role": "owner",
                "member_team_ids": ["T-PEER"],
                "member_workspaces": [{"team_id": "T-PEER", "workspace_name": "Workspace B"}],
            }
        ],
        "syncs": [{"uid": "sync-uid", "group_uid": "group-uid", "title": "S1"}],
        "sync_channels": [
            {"sync_uid": "sync-uid", "team_id": "T1", "channel_id": "C1"},
            {"sync_uid": "sync-uid", "team_id": "T-PEER", "channel_id": "C-PEER"},
        ],
        "post_meta": {
            "sync-uid:C-PEER": [{"post_id": "PARENT", "ts": "10.000001"}],
        },
    }
    created = []

    def find_records(model, _filters):
        if model.__name__ == "Instance":
            return [
                SimpleNamespace(
                    instance_id="peer-fp",
                    private_key_encrypted=None,
                    status="active",
                    trust_status="trusted",
                )
            ]
        return []

    def capture(record):
        name = type(record).__name__
        if name == "WorkspaceGroup":
            record.id = 3
        elif name == "Sync":
            record.id = 1
        elif name == "SyncChannel":
            record.id = 2
        elif name == "WorkspaceGroupMember":
            record.id = 8
        created.append(record)
        return record

    with (
        patch("helpers.export_import.DbManager.find_records", side_effect=find_records),
        patch("helpers.export_import.DbManager.create_record", side_effect=capture),
        patch("helpers.export_import.DbManager.delete_records"),
        patch("helpers.workspace_kind.ensure_stub_workspace") as ensure_stub,
        patch("helpers.export_import.get_workspace_by_id", return_value=None),
    ):
        import_migration_data(data, 42, 3, team_id_to_workspace_id={"T1": 42})

    ensure_stub.assert_not_called()
    posts = [row for row in created if type(row).__name__ == "PostMeta"]
    assert posts == []


def test_import_unknown_source_instance_does_not_guess_sole_trusted_peer():
    from helpers.export_import import _peer_instance_id_from_migration

    with (
        patch("helpers.export_import.DbManager.find_records", return_value=[]),
        patch("helpers.export_import._sole_trusted_peer_instance_id") as sole,
    ):
        assert _peer_instance_id_from_migration({"source_instance": {"instance_id": "other-fp"}}) is None
        sole.assert_not_called()


def test_import_post_meta_onto_peer_stub_channels():
    data = {
        "source_instance": {"instance_id": "peer-fp"},
        "workspace": {"team_id": "T1"},
        "groups": [
            {
                "uid": "group-uid",
                "name": "Group",
                "role": "owner",
                "member_team_ids": ["T-PEER"],
                "member_workspaces": [{"team_id": "T-PEER", "workspace_name": "Workspace B"}],
            }
        ],
        "syncs": [{"uid": "sync-uid", "group_uid": "group-uid", "title": "S1"}],
        "sync_channels": [
            {"sync_uid": "sync-uid", "team_id": "T1", "channel_id": "C1", "channel_name": "announcements"},
            {"sync_uid": "sync-uid", "team_id": "T-PEER", "channel_id": "C-PEER", "channel_name": "general"},
        ],
        "post_meta": {
            "sync-uid:C1": [{"post_id": "post-1", "ts": "10.000001"}],
            "sync-uid:C-PEER": [{"post_id": "post-1", "ts": "20.000002", "posted_as_user_id": "U-PEER"}],
        },
    }
    created = []
    stub = SimpleNamespace(id=99, team_id="T-PEER")
    next_id = {"WorkspaceGroup": 3, "Sync": 1, "SyncChannel": 10, "WorkspaceGroupMember": 8}

    def find_records(model, _filters):
        if model.__name__ == "Instance":
            return [SimpleNamespace(instance_id="peer-fp", private_key_encrypted=None)]
        if model.__name__ == "Workspace":
            return [stub]
        return []

    def capture(record):
        name = type(record).__name__
        if name in next_id:
            record.id = next_id[name]
            next_id[name] += 1
        created.append(record)
        return record

    with (
        patch("helpers.export_import.DbManager.find_records", side_effect=find_records),
        patch("helpers.export_import.DbManager.create_record", side_effect=capture),
        patch("helpers.export_import.DbManager.delete_records"),
        patch("helpers.workspace_kind.ensure_stub_workspace") as ensure_stub,
        patch("helpers.export_import.get_workspace_by_id", return_value=None),
    ):
        import_migration_data(data, 42, 3, team_id_to_workspace_id={"T1": 42})
    ensure_stub.assert_not_called()

    channels = {row.channel_id: row for row in created if type(row).__name__ == "SyncChannel"}
    post_metas = [row for row in created if type(row).__name__ == "PostMeta"]
    assert channels["C1"].channel_name == "announcements"
    assert channels["C-PEER"].channel_name == "general"
    assert {row.sync_channel_id for row in post_metas} == {channels["C1"].id, channels["C-PEER"].id}
    remote = next(row for row in post_metas if row.sync_channel_id == channels["C-PEER"].id)
    assert remote.post_id == "post-1"
    assert str(remote.ts) == "20.000002"
    assert remote.posted_as_user_id == "U-PEER"
