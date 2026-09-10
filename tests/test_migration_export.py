"""Data migration export looks up Workspaces by integer primary key."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from helpers.export_import import build_migration_export, import_migration_data


class TestBuildMigrationExportWorkspaceLookup:
    def test_finds_workspace_by_integer_pk(self):
        workspace = SimpleNamespace(
            id=42,
            team_id="T1",
            workspace_name="Alpha",
            deleted_at=None,
        )
        with (
            patch("helpers.export_import.get_workspace_by_id", return_value=workspace) as get_ws,
            patch("helpers.export_import.DbManager.find_records", return_value=[]),
            patch("helpers.export_import.os.environ.get", return_value=""),
        ):
            payload = build_migration_export(42, include_source_instance=False)

        get_ws.assert_called_with(42)
        assert payload["workspace"] == {"team_id": "T1", "workspace_name": "Alpha"}
        assert payload["syncs"] == []
        assert payload["groups"] == []

    def test_raises_when_workspace_missing(self):
        with (
            patch("helpers.export_import.get_workspace_by_id", return_value=None),
            pytest.raises(ValueError, match="Workspace not found"),
        ):
            build_migration_export(99)

    def test_exports_channel_participation_and_posting_identity(self):
        workspace = SimpleNamespace(id=42, team_id="T1", workspace_name="Alpha", deleted_at=None)
        sync = SimpleNamespace(
            id=9,
            title="Announcements",
            sync_mode="group",
            publisher_workspace_id=42,
            target_workspace_id=None,
        )
        sync_channel = SimpleNamespace(
            id=7,
            sync_id=9,
            channel_id="C1",
            status="active",
            publishes=True,
            subscribes=False,
            reaction_style="direct_only",
            reaction_direction="send",
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
            patch("helpers.export_import.DbManager.get_record", return_value=sync),
        ):
            payload = build_migration_export(42, include_source_instance=False)

        assert payload["sync_channels"] == [
            {
                "sync_title": "Announcements",
                "channel_id": "C1",
                "status": "active",
                "publishes": True,
                "subscribes": False,
                "reaction_style": "direct_only",
                "reaction_direction": "send",
            }
        ]
        assert payload["post_meta"]["Announcements:C1"][0]["posted_as_user_id"] == "U2"


def test_import_defaults_legacy_channel_participation_and_posting_identity():
    data = {
        "workspace": {"team_id": "T1"},
        "syncs": [{"title": "S1", "publisher_team_id": "T1"}],
        "sync_channels": [
            {"sync_title": "S1", "channel_id": "C1"},
            {
                "sync_title": "S1",
                "channel_id": "C2",
                "publishes": False,
                "subscribes": True,
                "reaction_style": "direct_only",
                "reaction_direction": "receive",
            },
        ],
        "post_meta": {"S1:C1": [{"post_id": "post-1", "ts": 100.0}]},
    }
    created = []

    def capture(record):
        if type(record).__name__ == "Sync":
            record.id = 1
        elif type(record).__name__ == "SyncChannel":
            record.id = 2
        created.append(record)
        return record

    with (
        patch("helpers.export_import.DbManager.find_records", return_value=[]),
        patch("helpers.export_import.DbManager.create_record", side_effect=capture),
        patch("helpers.export_import.DbManager.delete_records"),
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
    assert configured_channel.reaction_direction == "receive"
    assert post_meta.posted_as_user_id is None
