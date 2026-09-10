"""Full-instance restore and migration import accept pre-1.5.3 User Mapping keys."""

from datetime import UTC, datetime
from unittest.mock import patch

from db import schemas
from helpers.export_import import import_migration_data, restore_full_backup


class TestRestoreLegacyUserMappingKeys:
    def test_remaps_matched_at_and_last_auto_match(self):
        merged: list = []

        data = {
            "user_mappings": [
                {
                    "id": 1,
                    "source_workspace_id": 1,
                    "source_user_id": "U1",
                    "target_workspace_id": 2,
                    "target_user_id": "U2",
                    "map_method": "email",
                    "matched_at": "2026-01-02T03:04:05Z",
                }
            ],
            "workspace_settings": [
                {
                    "id": 1,
                    "workspace_id": 2,
                    "key": "last_auto_match",
                    "value": '{"at":"2026-01-02T03:04:05Z","newly_matched":1,"still_unmatched":0}',
                    "updated_at": "2026-01-02T03:04:05Z",
                }
            ],
        }
        with (
            patch("helpers.export_import._restore_raw_table"),
            patch("helpers.export_import.DbManager.merge_record", side_effect=lambda r: merged.append(r)),
        ):
            restore_full_backup(data)

        mapping = next(r for r in merged if isinstance(r, schemas.UserMapping))
        assert mapping.mapped_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

        setting = next(r for r in merged if isinstance(r, schemas.WorkspaceSetting))
        assert setting.key == "last_auto_map"


class TestImportLegacyMatchMethod:
    def test_import_accepts_match_method_key(self):
        data = {
            "workspace": {"team_id": "T1"},
            "syncs": [],
            "sync_channels": [],
            "post_meta": {},
            "user_directory": [],
            "user_mappings": [
                {
                    "source_team_id": "T1",
                    "target_team_id": "T2",
                    "source_user_id": "U1",
                    "target_user_id": "U2",
                    "match_method": "email",
                }
            ],
        }
        created: list = []

        with (
            patch("helpers.export_import.DbManager.find_records", return_value=[]),
            patch("helpers.export_import.DbManager.create_record", side_effect=lambda r: created.append(r)),
            patch("helpers.export_import.DbManager.delete_records"),
        ):
            import_migration_data(data, workspace_id=1, group_id=1, team_id_to_workspace_id={"T1": 1, "T2": 2})

        mapping = next(r for r in created if isinstance(r, schemas.UserMapping))
        assert mapping.map_method == "email"
        assert mapping.target_user_id == "U2"
        assert mapping.mapped_at is not None
