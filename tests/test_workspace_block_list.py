"""Workspace Block List parse, Settings ack, OAuth save, and heal."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

import constants  # noqa: E402
from slack import actions  # noqa: E402


class TestParseWorkspaceBlockList:
    def test_splits_comma_semicolon_and_whitespace(self):
        from helpers.settings import format_workspace_block_list, parse_workspace_block_list

        ids, err = parse_workspace_block_list("t0123456789, T9876543210; t0123456789\nTAAA")
        assert err is None
        assert ids == ["T0123456789", "T9876543210", "TAAA"]
        assert format_workspace_block_list(ids) == "T0123456789, T9876543210, TAAA"

    def test_rejects_malformed_id(self):
        from helpers.settings import parse_workspace_block_list

        ids, err = parse_workspace_block_list("C0123456789")
        assert ids == []
        assert err is not None
        assert "C0123456789" in err


class TestSettingsBlockListAck:
    def test_ack_rejects_primary_workspace(self):
        from handlers.settings import handle_settings_submit_ack

        form = MagicMock()
        form.get_selected_values.return_value = {
            actions.CONFIG_SETTINGS_WORKSPACE_BLOCK_LIST: "T0123456789",
        }
        with (
            patch.dict(os.environ, {constants.PRIMARY_WORKSPACE: "T0123456789"}),
            patch("handlers.settings.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.settings.helpers.get_team_id_from_body", return_value="T0123456789"),
            patch("handlers.settings.helpers.is_workspace_admin", return_value=True),
            patch("handlers.settings.helpers.is_primary_workspace", return_value=True),
            patch("handlers.settings._build_settings_form", return_value=form),
        ):
            result = handle_settings_submit_ack({}, MagicMock(), {})
        assert result["response_action"] == "errors"
        assert actions.CONFIG_SETTINGS_WORKSPACE_BLOCK_LIST in result["errors"]

    def test_ack_rejects_group_owner(self):
        from handlers.settings import handle_settings_submit_ack

        form = MagicMock()
        form.get_selected_values.return_value = {
            actions.CONFIG_SETTINGS_WORKSPACE_BLOCK_LIST: "T0987654321",
        }
        owner = SimpleNamespace(id=2, team_id="T0987654321", deleted_at=None)
        group = SimpleNamespace(id=9)
        with (
            patch.dict(os.environ, {constants.PRIMARY_WORKSPACE: "T0123456789"}),
            patch("handlers.settings.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.settings.helpers.get_team_id_from_body", return_value="T0123456789"),
            patch("handlers.settings.helpers.is_workspace_admin", return_value=True),
            patch("handlers.settings.helpers.is_primary_workspace", return_value=True),
            patch("handlers.settings._build_settings_form", return_value=form),
            patch("handlers.settings.DbManager.get_record", return_value=owner),
            patch("handlers.settings.helpers.get_groups_for_workspace", return_value=[group]),
            patch("handlers.settings.helpers.is_workspace_owner", return_value=True),
            patch("handlers.settings.helpers.resolve_workspace_name", return_value="Workspace A"),
        ):
            result = handle_settings_submit_ack({}, MagicMock(), {})
        assert result["response_action"] == "errors"
        assert "Owner" in result["errors"][actions.CONFIG_SETTINGS_WORKSPACE_BLOCK_LIST]


class TestSettingsBlockListWork:
    def test_newly_added_id_calls_uninstall_helper(self):
        from handlers.settings import handle_settings_submit

        form = MagicMock()
        form.get_selected_values.return_value = {
            actions.CONFIG_SETTINGS_WORKSPACE_BLOCK_LIST: "T0987654321",
        }
        primary = SimpleNamespace(id=1, team_id="T0123456789")
        installed = SimpleNamespace(id=2, team_id="T0987654321", deleted_at=None)
        with (
            patch.dict(os.environ, {constants.PRIMARY_WORKSPACE: "T0123456789"}),
            patch("handlers.settings.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.settings.helpers.get_team_id_from_body", return_value="T0123456789"),
            patch("handlers.settings.helpers.is_workspace_admin", return_value=True),
            patch("handlers.settings.helpers.get_workspace_record", return_value=primary),
            patch("handlers.settings.helpers.is_primary_workspace", return_value=True),
            patch("handlers.settings._build_settings_form", return_value=form),
            patch("handlers.settings.helpers.set_workspace_setting"),
            patch("handlers.settings.helpers.extra_manager_user_ids", return_value=[]),
            patch("handlers.settings.helpers.allow_private_channels", return_value=False),
            patch("handlers.settings.helpers.workspace_block_list", return_value=[]),
            patch("handlers.settings.helpers.parse_workspace_block_list", return_value=(["T0987654321"], None)),
            patch("handlers.settings.helpers.format_workspace_block_list", return_value="T0987654321"),
            patch("handlers.settings._block_list_field_error", return_value=None),
            patch("handlers.settings.helpers.set_setting") as set_setting,
            patch("handlers.settings.DbManager.get_record", return_value=installed),
            patch("handlers.settings.helpers.get_bot_token", return_value="xoxb-blocked"),
            patch("handlers.settings.helpers.slack_apps_uninstall") as apps_uninstall,
            patch("handlers.settings.helpers.uninstall_workspace") as uninstall,
            patch("handlers.settings.builders.refresh_home_tab_for_workspace"),
        ):
            handle_settings_submit({}, MagicMock(), MagicMock(), context={})
        set_setting.assert_any_call(constants.SETTING_WORKSPACE_BLOCK_LIST, "T0987654321")
        apps_uninstall.assert_called_once_with("xoxb-blocked")
        uninstall.assert_called_once_with("T0987654321")


class TestInstallationStoreBlock:
    def test_save_does_not_persist_blocked_team(self):
        from slack_sdk.oauth.installation_store.models import Installation

        from helpers.encryption_installation_store import EncryptedSQLAlchemyInstallationStore, WorkspaceBlockedError

        store = EncryptedSQLAlchemyInstallationStore.__new__(EncryptedSQLAlchemyInstallationStore)
        installation = Installation(
            app_id="A1",
            enterprise_id=None,
            team_id="T0123456789",
            user_id="U1",
            bot_token="xoxb-blocked",
        )
        with (
            patch("helpers.settings.team_id_is_blocked", return_value=True),
            patch("helpers.workspace.slack_apps_uninstall") as uninstall,
            patch(
                "slack_sdk.oauth.installation_store.sqlalchemy.SQLAlchemyInstallationStore.save",
            ) as save,
            pytest.raises(WorkspaceBlockedError),
        ):
            store.save(installation)
        uninstall.assert_called_once_with("xoxb-blocked")
        save.assert_not_called()

    def test_save_bot_does_not_persist_blocked_team(self):
        from helpers.encryption_installation_store import EncryptedSQLAlchemyInstallationStore, WorkspaceBlockedError

        store = EncryptedSQLAlchemyInstallationStore.__new__(EncryptedSQLAlchemyInstallationStore)
        bot = SimpleNamespace(team_id="T0123456789", bot_token="xoxb-blocked", bot_refresh_token=None)
        with (
            patch("helpers.settings.team_id_is_blocked", return_value=True),
            patch("helpers.workspace.slack_apps_uninstall") as uninstall,
            patch(
                "slack_sdk.oauth.installation_store.sqlalchemy.SQLAlchemyInstallationStore.save_bot",
            ) as save_bot,
            pytest.raises(WorkspaceBlockedError),
        ):
            store.save_bot(bot)
        uninstall.assert_called_once_with("xoxb-blocked")
        save_bot.assert_not_called()


class TestHealBlocked:
    def test_heal_workspace_to_local_refused_when_blocked(self):
        from helpers.workspace_kind import heal_workspace_to_local

        with patch("helpers.settings.team_id_is_blocked", return_value=True):
            assert heal_workspace_to_local("T0123456789", source="import") == "blocked"


class TestOAuthFailureBlocked:
    def test_blocked_workspace_returns_custom_html(self):
        from slack_bolt.error import BoltError

        from helpers.encryption_installation_store import WorkspaceBlockedError
        from helpers.oauth import _oauth_failure

        err = WorkspaceBlockedError("T0123456789")
        assert isinstance(err, BoltError)
        args = SimpleNamespace(
            reason="storage_error",
            error=err,
            default=SimpleNamespace(failure=MagicMock(return_value="default")),
        )
        resp = _oauth_failure(args)
        assert resp.status == 403
        assert "cannot install SyncBot" in resp.body
        assert "block list" in resp.body
        args.default.failure.assert_not_called()


class TestGetWorkspaceRecordBlocked:
    def test_does_not_create_or_announce_a_blocked_team(self):
        from helpers.workspace import get_workspace_record

        with (
            patch("helpers.settings.team_id_is_blocked", return_value=True),
            patch("helpers.workspace.DbManager.get_record", return_value=None),
            patch("helpers.workspace.DbManager.create_record") as create,
            patch("helpers.workspace._push_live_team_to_trusted_peers") as push,
        ):
            assert get_workspace_record("T0123456789", {}, {}, MagicMock()) is None
        create.assert_not_called()
        push.assert_not_called()

    def test_does_not_restore_or_announce_a_blocked_stub(self):
        from helpers.workspace import get_workspace_record

        stub = SimpleNamespace(team_id="T0123456789", deleted_at=None)
        with (
            patch("helpers.settings.team_id_is_blocked", return_value=True),
            patch("helpers.workspace.DbManager.get_record", return_value=stub),
            patch("helpers.workspace._is_stub_workspace", return_value=True),
            patch("helpers.workspace._restore_workspace") as restore,
            patch("helpers.workspace._push_live_team_to_trusted_peers") as push,
        ):
            assert get_workspace_record("T0123456789", {}, {}, MagicMock()) is stub
        restore.assert_not_called()
        push.assert_not_called()

    def test_uninstalls_a_live_workspace_that_is_still_listed(self):
        from helpers.workspace import get_workspace_record

        live = SimpleNamespace(team_id="T0123456789", deleted_at=None)
        paused = SimpleNamespace(team_id="T0123456789", deleted_at="now")
        with (
            patch("helpers.settings.team_id_is_blocked", return_value=True),
            patch("helpers.workspace.DbManager.get_record", side_effect=[live, paused]),
            patch("helpers.workspace._is_stub_workspace", return_value=False),
            patch("helpers.workspace.get_bot_token", return_value="xoxb-1"),
            patch("helpers.workspace.slack_apps_uninstall") as apps,
            patch("helpers.workspace.uninstall_workspace") as uninstall,
            patch("helpers.workspace._push_live_team_to_trusted_peers") as push,
        ):
            got = get_workspace_record("T0123456789", {}, {}, MagicMock())
        apps.assert_called_once_with("xoxb-1")
        uninstall.assert_called_once_with("T0123456789")
        push.assert_not_called()
        assert got is paused
