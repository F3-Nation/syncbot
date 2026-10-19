"""Focused unit tests for backup/restore and migration handler validation."""

import json
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from handlers.export_import import (  # noqa: E402
    handle_backup_restore,
    handle_backup_restore_submit_ack,
)
from slack import actions  # noqa: E402


class TestBackupRestoreSubmitValidation:
    def test_returns_error_when_file_missing(self):
        client = MagicMock()
        body = {"user": {"id": "U1"}, "team": {"id": "TTEST"}, "view": {"state": {"values": {}}}}

        with (
            patch.dict(os.environ, {"PRIMARY_WORKSPACE": "TTEST"}),
            patch("handlers.export_import._is_admin", return_value=True),
        ):
            resp = handle_backup_restore_submit_ack(body, client, context={})

        assert resp["response_action"] == "errors"
        assert actions.CONFIG_BACKUP_RESTORE_JSON_INPUT in resp["errors"]

    def test_returns_error_when_uploaded_file_has_no_url(self):
        client = MagicMock()
        body = {
            "user": {"id": "U1"},
            "team": {"id": "TTEST"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: {
                            actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: {
                                "files": [{"id": "F123"}],
                            }
                        }
                    }
                }
            },
        }

        with (
            patch.dict(os.environ, {"PRIMARY_WORKSPACE": "TTEST"}),
            patch("handlers.export_import._is_admin", return_value=True),
        ):
            resp = handle_backup_restore_submit_ack(body, client, context={})

        assert resp["response_action"] == "errors"
        assert "Could not retrieve the uploaded file." in resp["errors"][actions.CONFIG_BACKUP_RESTORE_JSON_INPUT]

    def test_rejects_wrong_dump_format_version(self):
        client = MagicMock()
        body = {
            "user": {"id": "U1"},
            "team": {"id": "TTEST"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: {
                            actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: {
                                "files": [{"url_private_download": "https://files.example/b.json"}],
                            }
                        }
                    }
                }
            },
        }
        with (
            patch.dict(os.environ, {"PRIMARY_WORKSPACE": "TTEST"}),
            patch("handlers.export_import._is_admin", return_value=True),
            patch(
                "handlers.export_import._download_uploaded_file",
                return_value=('{"version": 2, "syncbot_version": "1.7.0"}', None),
            ),
        ):
            resp = handle_backup_restore_submit_ack(body, client, context={})

        assert resp["response_action"] == "errors"
        assert "Unsupported backup version" in resp["errors"][actions.CONFIG_BACKUP_RESTORE_JSON_INPUT]

    def test_syncbot_version_label_is_not_a_gate(self):
        from handlers._common import _STAY_WAIT_TEXT

        client = MagicMock()
        body = {
            "user": {"id": "U1"},
            "team": {"id": "TTEST"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: {
                            actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: {
                                "files": [{"url_private_download": "https://files.example/b.json"}],
                            }
                        }
                    }
                }
            },
        }
        with (
            patch.dict(os.environ, {"PRIMARY_WORKSPACE": "TTEST"}),
            patch("handlers.export_import._is_admin", return_value=True),
            patch(
                "handlers.export_import._download_uploaded_file",
                return_value=('{"version": 1, "syncbot_version": "0.0.0"}', None),
            ),
            patch("handlers.export_import.ei.verify_backup_hmac", return_value=True),
            patch("handlers.export_import.ei.verify_backup_encryption_key", return_value=True),
        ):
            resp = handle_backup_restore_submit_ack(body, client, context={})

        assert resp["response_action"] == "update"
        assert resp["view"]["title"]["text"] == "Backup / Restore"
        assert resp["view"]["close"]["text"] == "Close"
        assert "submit" not in resp["view"]
        assert resp["view"]["blocks"][0]["text"]["text"] == _STAY_WAIT_TEXT


class TestHandleBackupRestorePrimaryWorkspace:
    def test_returns_early_when_primary_mismatch(self):
        client = MagicMock()
        body = {
            "user": {"id": "U1"},
            "team": {"id": "T_WRONG"},
            "trigger_id": "trig",
        }
        with (
            patch.dict(os.environ, {"PRIMARY_WORKSPACE": "T_PRIMARY"}),
            patch("handlers.export_import._is_admin", return_value=True),
        ):
            handle_backup_restore(body, client, MagicMock(), {})

        client.views_update.assert_not_called()


class TestDataMigrationModal:
    def test_export_buttons_and_copy(self):
        from handlers.export_import import handle_data_migration

        client = MagicMock()
        body = {"user": {"id": "U1"}, "team": {"id": "T1"}, "trigger_id": "tr"}
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("handlers.export_import.helpers.federation_enabled", return_value=True),
            patch("handlers.export_import._primary_workspace_code_tick", return_value="`Workspace A`"),
        ):
            handle_data_migration(body, client, MagicMock(), {})
        view = client.views_update.call_args.kwargs["view"]
        text = str(view)
        assert ":outbox_tray: Export" in text
        assert ":link: Export and Request Connection" in text
        assert "admins of `Workspace A`" in text
        assert "already connected" in text
        assert "not connected yet" in text
        assert "Export only" not in text
        assert "Home tab" not in text

    def test_export_confirms_in_modal_after_dm(self):
        from types import SimpleNamespace

        from handlers._common import _DM_WAIT_TEXT
        from handlers.export_import import handle_data_migration_export

        client = MagicMock()
        body = {"user": {"id": "U1"}, "team": {"id": "T1"}, "view": {"id": "V1"}}
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A", deleted_at=None)
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("handlers.export_import.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.export_import.ei.build_migration_export", return_value={"workspace": {"team_id": "T1"}}),
            patch("handlers.export_import._open_dm_channel", return_value="D1"),
            patch("handlers.export_import.helpers.resolve_workspace_name", return_value="Workspace A"),
        ):
            handle_data_migration_export(body, client, MagicMock(), {})
        assert [name for name, _a, _k in client.method_calls[:2]] == ["views_update", "files_upload_v2"]
        view = client.views_update.call_args.kwargs["view"]
        assert view["close"]["text"] == "Close"
        assert view["blocks"][0]["text"]["text"] == _DM_WAIT_TEXT
        assert view.get("submit") is None


class TestDataMigrationRequest:
    def test_confirms_in_modal_and_names_requester_without_mention(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_request

        client = MagicMock()
        body = {
            "user": {"id": "U_REQ"},
            "team": {"id": "T_SRC"},
            "view": {"id": "V1"},
        }
        workspace = SimpleNamespace(id=1, team_id="T_SRC", workspace_name="Workspace A", deleted_at=None)
        primary = SimpleNamespace(id=2, team_id="T_PRIMARY", deleted_at=None)
        request = SimpleNamespace(id=9, subject_team_id="T_SRC", requested_by_user_id="U_REQ")
        with (
            patch.dict(os.environ, {"PRIMARY_WORKSPACE": "T_PRIMARY"}),
            patch("handlers.export_import.helpers.federation_enabled", return_value=True),
            patch("handlers.export_import._is_admin", return_value=True),
            patch("handlers.export_import.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.export_import.DbManager.find_records", return_value=[]),
            patch("handlers.export_import.DbManager.create_record", return_value=request),
            patch("handlers.export_import.DbManager.get_record", return_value=primary),
            patch("handlers.export_import.helpers.get_bot_token", return_value="xoxb-primary"),
            patch("handlers.export_import.helpers.get_user_info", return_value=("Ada Lovelace", None)),
            patch("handlers.export_import.helpers.resolve_workspace_name", return_value="Workspace A"),
            patch("handlers.export_import._primary_workspace_code_tick", return_value="`Workspace A`"),
            patch("handlers.export_import.helpers.notify_admins_dm") as notify,
            patch("handlers.export_import._dm_migration_requester") as requester_dm,
            patch("handlers.export_import.WebClient"),
        ):
            handle_data_migration_request(body, client, MagicMock(), {})

        text = notify.call_args.args[1]
        assert "<@U_REQ>" not in text
        assert "`Ada Lovelace (Workspace A)`" in text
        assert "requested to export data and create a remote connection." in text
        assert ":globe_with_meridians: Approve and Create" in str(notify.call_args)
        assert requester_dm.call_args.args[1] == (
            ":outbox_tray: Your request to export data and create a remote connection has been sent to admins in the `Workspace A` workspace."
        )
        view = client.views_update.call_args.kwargs["view"]
        assert view["close"]["text"] == "Close"
        assert "A DM was sent to admins in `Workspace A`" in view["blocks"][0]["text"]["text"]

    def test_already_waiting_names_primary(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_request

        client = MagicMock()
        body = {"user": {"id": "U_REQ"}, "team": {"id": "T_SRC"}, "view": {"id": "V1"}}
        pending = SimpleNamespace(id=9, subject_team_id="T_SRC", requested_by_user_id="U_REQ")
        with (
            patch("handlers.export_import.helpers.federation_enabled", return_value=True),
            patch("handlers.export_import._is_admin", return_value=True),
            patch("handlers.export_import.helpers.get_workspace_record", return_value=SimpleNamespace()),
            patch("handlers.export_import.DbManager.find_records", return_value=[pending]),
            patch("handlers.export_import._primary_workspace_code_tick", return_value="`Workspace A`"),
            patch("handlers.export_import._dm_migration_requester") as requester_dm,
        ):
            handle_data_migration_request(body, client, MagicMock(), {})

        assert requester_dm.call_args.args[1] == (
            ":outbox_tray: A request to export data and create a remote connection is already waiting "
            "for admins in `Workspace A`."
        )
        view = client.views_update.call_args.kwargs["view"]
        assert "already waiting for admins in `Workspace A`" in view["blocks"][0]["text"]["text"]

    def test_decline_names_primary(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_pairing_request_decline

        request = SimpleNamespace(id=9, status="pending")
        with (
            patch("handlers.export_import.helpers.is_primary_workspace", return_value=True),
            patch("handlers.export_import.helpers.is_workspace_admin", return_value=True),
            patch("handlers.export_import.DbManager.get_record", return_value=request),
            patch("handlers.export_import.DbManager.update_records"),
            patch("handlers.export_import._primary_workspace_code_tick", return_value="`Workspace A`"),
            patch("handlers.export_import._dm_migration_requester") as requester_dm,
        ):
            handle_pairing_request_decline(
                {"user": {"id": "U_ADMIN"}, "team": {"id": "T_PRIMARY"}, "actions": [{"value": "9"}]},
                MagicMock(),
                MagicMock(),
                {},
            )

        text = requester_dm.call_args.args[1]
        assert "Admins in `Workspace A` declined your request to export data and create a remote connection." in text
        assert "You can still use Export for the file only." in text

    def test_approve_opens_create_modal(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_pairing_request_approve

        request = SimpleNamespace(id=9, status="pending", subject_team_id="T_SRC")
        source = SimpleNamespace(id=10, team_id="T_SRC", workspace_name="Workspace B")

        def _get_record(model, id=None, **kwargs):
            name = getattr(model, "__name__", "")
            if name == "FederationPairingRequest":
                return request
            if name == "Workspace":
                return source
            return None

        with (
            patch("handlers.export_import.helpers.is_primary_workspace", return_value=True),
            patch("handlers.export_import.helpers.is_workspace_admin", return_value=True),
            patch("handlers.export_import.DbManager.get_record", side_effect=_get_record),
            patch("handlers.export_import.helpers.resolve_workspace_name", return_value="Workspace B"),
            patch("handlers.federation_cmds.open_create_external_connection_modal") as open_modal,
        ):
            handle_pairing_request_approve(
                {
                    "user": {"id": "U_ADMIN"},
                    "team": {"id": "T_PRIMARY"},
                    "trigger_id": "tr",
                    "actions": [{"value": "9"}],
                },
                MagicMock(),
                MagicMock(),
                {},
            )

        open_modal.assert_called_once()
        assert open_modal.call_args.kwargs["request_id"] == 9
        assert open_modal.call_args.kwargs["exclude_workspace_id"] == 10
        assert open_modal.call_args.kwargs["initial_name"] == "Workspace B"
        assert open_modal.call_args.kwargs["leaving_workspace_name"] == "Workspace B"


class TestDataMigrationImportConnect:
    def test_prepare_does_not_pair(self):
        from types import SimpleNamespace

        from handlers.export_import import _data_migration_prepare

        payload = {
            "version": 1,
            "workspace": {"team_id": "T1"},
            "source_instance": {
                "webhook_url": "https://ws-a.example/api/federation",
                "instance_id": "a" * 64,
                "public_key": "pem",
                "connection_code": "ENCODED_BLOB",
            },
        }
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        body = {
            "user": {"id": "U1"},
            "team": {"id": "T1"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
                            actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
                                "files": [{"url_private_download": "https://files.example/m.json"}],
                            }
                        }
                    }
                }
            },
        }
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch(
                "handlers.export_import._download_uploaded_file",
                return_value=(json.dumps(payload), None),
            ),
            patch("handlers.export_import.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.export_import.DbManager.find_records", return_value=[]),
            patch("federation.core.initiate_federation_connect") as connect,
        ):
            err, data, *_rest = _data_migration_prepare(body, MagicMock(token="xoxb"), {})

        assert err is None
        assert data is not None
        connect.assert_not_called()

    def test_submit_ack_reviews_groups_and_connection(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_submit_ack

        payload = {
            "version": 1,
            "workspace": {"team_id": "T1"},
            "groups": [{"uid": "g1", "name": "Workspace Group", "role": "member"}],
            "syncs": [{"uid": "s1", "group_uid": "g1", "title": "Announcements"}],
            "sync_channels": [{"sync_uid": "s1", "channel_id": "C1"}],
            "user_mappings": [{"source_user_id": "U1"}],
            "post_meta": {"s1:C1": [{"kind": "message", "post_id": "p1"}]},
            "source_instance": {
                "webhook_url": "https://ws-a.example/api/federation",
                "instance_id": "a" * 64,
                "public_key": "pem",
                "connection_code": "ENCODED_BLOB",
            },
        }
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        parsed = {
            "code": "FED-AABBCCDD",
            "webhook_url": "https://ws-a.example/api/federation",
            "instance_id": "a" * 64,
            "public_key": "pem",
            "label": "Partner Org",
            "primary_team_id": "TPRIMARY",
            "primary_workspace_name": "Workspace A",
        }
        body = {
            "user": {"id": "U1"},
            "team": {"id": "T1"},
            "view": {
                "state": {
                    "values": {
                        actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
                            actions.CONFIG_DATA_MIGRATION_JSON_INPUT: {
                                "files": [{"url_private_download": "https://files.example/m.json"}],
                            }
                        }
                    }
                }
            },
        }
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch(
                "handlers.export_import._download_uploaded_file",
                return_value=(json.dumps(payload), None),
            ),
            patch("handlers.export_import.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.export_import.DbManager.find_records", return_value=[]),
            patch("handlers.export_import.ei.verify_migration_signature", return_value=True),
            patch("federation.core.parse_federation_code", return_value=parsed),
            patch("federation.core.initiate_federation_connect") as connect,
            patch("helpers._cache._cache_set") as cache_set,
        ):
            resp = handle_data_migration_submit_ack(body, MagicMock(token="xoxb"), {})

        connect.assert_not_called()
        cache_set.assert_called_once()
        assert resp["response_action"] == "update"
        view = resp["view"]
        assert view["callback_id"] == actions.CONFIG_DATA_MIGRATION_REVIEW
        assert view["submit"]["text"] == "Import"
        text = str(view)
        assert "Partner Org" in text
        assert "Workspace A" in text
        assert "Workspace Group" in text
        assert "Synced Channels" in text
        assert "Mapped Users" in text
        assert "Synced Messages" in text
        assert "Announcements" in text

    def test_ensure_skips_pair_when_peer_exists(self):
        from types import SimpleNamespace

        from handlers.export_import import _ensure_migration_connection

        payload = {
            "source_instance": {
                "webhook_url": "https://ws-a.example/api/federation",
                "instance_id": "a" * 64,
                "public_key": "pem",
                "connection_code": "ENCODED_BLOB",
            }
        }
        workspace = SimpleNamespace(id=1, team_id="T1", workspace_name="Workspace A")
        parsed = {
            "code": "FED-AABBCCDD",
            "webhook_url": "https://ws-a.example/api/federation",
            "instance_id": "a" * 64,
            "public_key": "pem",
            "label": "Partner Org",
        }
        peer = SimpleNamespace(instance_id="a" * 64)

        def find_records(model, _filters):
            name = getattr(model, "__name__", "")
            if name == "Instance":
                return [peer]
            return [object()]

        with (
            patch("federation.core.parse_federation_code", return_value=parsed),
            patch("federation.core.initiate_federation_connect") as connect,
            patch("federation.core.get_or_create_instance", return_value=peer),
            patch("handlers.export_import.DbManager.find_records", side_effect=find_records),
            patch("handlers.export_import.invalidate_fed_ws_for_sync_cache"),
        ):
            _ensure_migration_connection(payload, workspace, {})

        connect.assert_not_called()

    def test_review_ack_keeps_wait_modal(self):
        from handlers._common import _DM_WAIT_TEXT
        from handlers.export_import import handle_data_migration_review_ack

        result = handle_data_migration_review_ack({}, MagicMock(), {})
        assert result["response_action"] == "update"
        assert result["view"]["title"]["text"] == "Confirm Import"
        assert result["view"]["close"]["text"] == "Close"
        assert "submit" not in result["view"]
        assert result["view"]["blocks"][0]["text"]["text"] == _DM_WAIT_TEXT

    def test_review_import_dms_when_finished(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_review

        workspace = SimpleNamespace(id=1, team_id="T1")
        body = {"user": {"id": "U1"}, "view": {"id": "V1"}}
        client = MagicMock()
        meta = {
            "data": {"version": 1},
            "group_id": 7,
            "workspace_id": 1,
            "team_id_to_workspace_id": {},
        }
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("helpers._cache._cache_get", return_value=meta),
            patch("handlers.export_import.helpers.get_workspace_by_id", return_value=workspace),
            patch("handlers.export_import._ensure_migration_connection") as ensure,
            patch("handlers.export_import._run_migration_import") as run_import,
            patch("handlers.export_import.builders.refresh_home_tab_for_workspace"),
            patch("handlers.export_import.helpers.notify_user_dm") as dm,
        ):
            handle_data_migration_review(body, client, MagicMock(), {})
        ensure.assert_called_once()
        run_import.assert_called_once()
        dm.assert_called_once()
        assert "Import finished" in dm.call_args.args[2]
        client.views_update.assert_called_once()

    def test_review_import_dms_when_expired(self):
        from handlers.export_import import handle_data_migration_review

        body = {"user": {"id": "U1"}, "view": {"id": "V1"}}
        client = MagicMock()
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("helpers._cache._cache_get", return_value=None),
            patch("handlers.export_import.helpers.notify_user_dm") as dm,
        ):
            handle_data_migration_review(body, client, MagicMock(), {})
        dm.assert_called_once()
        assert "import expired" in dm.call_args.args[2]

    def test_review_import_dms_when_failed(self):
        from types import SimpleNamespace

        from handlers.export_import import handle_data_migration_review

        workspace = SimpleNamespace(id=1, team_id="T1")
        body = {"user": {"id": "U1"}, "view": {"id": "V1"}}
        client = MagicMock()
        meta = {
            "data": {"version": 1},
            "group_id": 7,
            "workspace_id": 1,
            "team_id_to_workspace_id": {},
        }
        with (
            patch("handlers.export_import._is_admin", return_value=True),
            patch("helpers._cache._cache_get", return_value=meta),
            patch("handlers.export_import.helpers.get_workspace_by_id", return_value=workspace),
            patch("handlers.export_import._ensure_migration_connection"),
            patch("handlers.export_import._run_migration_import", side_effect=RuntimeError("boom")),
            patch("handlers.export_import.helpers.notify_user_dm") as dm,
        ):
            handle_data_migration_review(body, client, MagicMock(), {})
        dm.assert_called_once()
        assert "Import failed" in dm.call_args.args[2]
