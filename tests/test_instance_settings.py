"""Tests for database-backed instance settings and the operator Settings gate."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from sqlalchemy import inspect  # noqa: E402

import constants  # noqa: E402
from db import DbManager, schemas  # noqa: E402


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database
    from helpers._cache import clear_all_caches

    url = f"sqlite:///{tmp_path / 'settings.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            clear_all_caches()
            yield
        finally:
            clear_all_caches()
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema


class TestMigration004:
    def test_instance_settings_table_exists(self, real_db):
        from db import get_engine

        assert inspect(get_engine()).has_table("instance_settings")

    def test_migration_is_a_noop_when_the_table_already_exists(self, real_db, tmp_path):
        """Fresh databases get the table from 001's create_all, so 004 must skip."""
        from alembic import command
        from sqlalchemy import text

        from db import _alembic_config, get_engine

        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '003_group_roles'"))

        command.upgrade(_alembic_config(), "head")

        assert inspect(engine).has_table("instance_settings")


class TestResolutionPrecedence:
    def test_set_setting_updates_an_existing_row_rather_than_duplicating(self, real_db):
        import helpers

        helpers.set_setting("sample_key", "true")
        helpers.set_setting("sample_key", "false")

        rows = DbManager.find_records(
            schemas.InstanceSetting,
            [schemas.InstanceSetting.key == "sample_key"],
        )
        assert len(rows) == 1
        assert rows[0].value == "false"


class TestTypedParsing:
    def test_bool_accepts_the_usual_spellings(self, real_db):
        import helpers

        for raw, expected in (("true", True), ("1", True), ("yes", True), ("false", False), ("0", False)):
            helpers.set_setting("t_bool", raw)
            assert helpers.get_bool_setting("t_bool", False) is expected

    def test_bool_falls_back_to_the_default_when_unparseable(self, real_db):
        import helpers

        helpers.set_setting("t_bool_bad", "perhaps")
        assert helpers.get_bool_setting("t_bool_bad", True) is True

    def test_int_parses_and_falls_back(self, real_db):
        import helpers

        helpers.set_setting("t_int", "45")
        assert helpers.get_int_setting("t_int", 30) == 45

        helpers.set_setting("t_int_bad", "many")
        assert helpers.get_int_setting("t_int_bad", 30) == 30

    def test_retention_days_reads_the_setting(self, real_db):
        import helpers

        helpers.set_setting(constants.SETTING_SOFT_DELETE_RETENTION_DAYS, "90")
        assert helpers.soft_delete_retention_days() == 90


class TestFederationEnabled:
    def test_defaults_off_with_no_row_and_no_env(self, real_db):
        import helpers
        from helpers import settings as settings_mod

        settings_mod._IGNORED_ENV_WARNED.clear()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(constants.SYNCBOT_FEDERATION_ENABLED, None)
            assert helpers.federation_enabled() is False

    def test_leftover_env_true_seeds_once(self, real_db):
        import helpers
        from helpers import settings as settings_mod

        settings_mod._IGNORED_ENV_WARNED.clear()
        with patch.dict(os.environ, {constants.SYNCBOT_FEDERATION_ENABLED: "true"}):
            assert helpers.federation_enabled() is True
        assert constants.SYNCBOT_FEDERATION_ENABLED in settings_mod._IGNORED_ENV_WARNED
        rows = DbManager.find_records(
            schemas.InstanceSetting,
            [schemas.InstanceSetting.key == constants.SETTING_FEDERATION_ENABLED],
        )
        assert len(rows) == 1
        assert rows[0].value == "true"

    def test_leftover_env_false_stays_off(self, real_db):
        import helpers
        from helpers import settings as settings_mod

        settings_mod._IGNORED_ENV_WARNED.clear()
        with patch.dict(os.environ, {constants.SYNCBOT_FEDERATION_ENABLED: "false"}):
            assert helpers.federation_enabled() is False
        rows = DbManager.find_records(
            schemas.InstanceSetting,
            [schemas.InstanceSetting.key == constants.SETTING_FEDERATION_ENABLED],
        )
        assert rows == []

    def test_database_value_wins_after_seed(self, real_db):
        import helpers
        from helpers import settings as settings_mod

        helpers.set_setting(constants.SETTING_FEDERATION_ENABLED, "false")
        settings_mod._IGNORED_ENV_WARNED.clear()
        with patch.dict(os.environ, {constants.SYNCBOT_FEDERATION_ENABLED: "true"}):
            assert helpers.federation_enabled() is False


class TestSettingsVisibilityGate:
    def test_visible_for_any_installed_workspace_team_id(self):
        import helpers

        assert helpers.is_settings_visible_for_workspace("T1") is True
        assert helpers.is_settings_visible_for_workspace("T_OTHER") is True

    def test_hidden_when_team_id_is_missing(self):
        import helpers

        assert helpers.is_settings_visible_for_workspace(None) is False
        assert helpers.is_settings_visible_for_workspace("") is False


class TestSettingsHandlerGate:
    WORKSPACE = SimpleNamespace(id=1, team_id="T1")

    def test_submit_rejected_for_non_admin(self):
        from handlers.settings import handle_settings_submit

        body = {"view": {"team_id": "T1", "state": {"values": {}}, "blocks": []}, "user": {"id": "U1"}}

        with (
            patch("handlers.settings.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.settings.helpers.is_workspace_admin", return_value=False),
            patch("handlers.settings.helpers.set_workspace_setting") as set_ws,
        ):
            handle_settings_submit(body, MagicMock(), MagicMock(), context={})

        assert not set_ws.called

    def test_non_primary_cannot_write_instance_settings(self):
        from handlers.settings import handle_settings_submit

        body = {
            "view": {
                "team_id": "T_OTHER",
                "state": {"values": {}},
                "blocks": [],
            },
            "user": {"id": "U1"},
        }
        ws = SimpleNamespace(id=2, team_id="T_OTHER")

        with (
            patch.dict(os.environ, {constants.PRIMARY_WORKSPACE: "T_PRIMARY"}),
            patch("handlers.settings.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.settings.helpers.is_workspace_admin", return_value=True),
            patch("handlers.settings.helpers.get_workspace_record", return_value=ws),
            patch("handlers.settings.helpers.set_setting") as set_instance,
            patch("handlers.settings.helpers.set_workspace_setting"),
            patch("handlers.settings.helpers.extra_manager_user_ids", return_value=[]),
            patch("handlers.settings.helpers.allow_private_channels", return_value=False),
            patch("handlers.settings.helpers.federation_enabled", return_value=False),
            patch("handlers.settings.helpers.soft_delete_retention_days", return_value=30),
            patch("handlers.settings.builders.refresh_home_tab_for_workspace"),
            patch("handlers.settings._primary_workspace_label", return_value="Workspace A"),
        ):
            handle_settings_submit(body, MagicMock(), MagicMock(), context={})

        assert not set_instance.called

    def test_open_rejected_for_non_admin(self):
        from handlers.settings import handle_open_settings

        body = {"team": {"id": "T1"}, "user": {"id": "U1"}, "trigger_id": "tr"}

        with (
            patch("handlers.settings.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.settings.helpers.is_workspace_admin", return_value=False),
            patch("handlers.settings._build_settings_form") as build_form,
        ):
            handle_open_settings(body, MagicMock(), MagicMock(), context={})

        assert not build_form.called

    def test_extra_manager_cannot_open_settings(self):
        from handlers.settings import handle_open_settings

        body = {"team": {"id": "T1"}, "user": {"id": "U_EXTRA"}, "trigger_id": "tr"}

        with (
            patch("handlers.settings.helpers.get_user_id_from_body", return_value="U_EXTRA"),
            patch("handlers.settings.helpers.is_workspace_admin", return_value=False),
            patch("handlers.settings.helpers.is_workspace_manager", return_value=True),
            patch("handlers.settings._build_settings_form") as build_form,
        ):
            handle_open_settings(body, MagicMock(), MagicMock(), context={})

        assert not build_form.called


class TestSettingsFormRenders:
    """Build the real form (no _build_settings_form patch) so a missing orm
    element or bad field cannot slip through the way it would when the form is
    always mocked out."""

    def test_form_serializes_to_valid_slack_blocks(self):
        from handlers.settings import _build_settings_form

        with (
            patch.dict(os.environ, {constants.PRIMARY_WORKSPACE: "T_PRIMARY"}),
            patch("handlers.settings.helpers.allow_private_channels", return_value=True),
            patch("handlers.settings.helpers.extra_manager_user_ids", return_value=["U_EXTRA"]),
            patch("handlers.settings.helpers.federation_enabled", return_value=False),
            patch("handlers.settings.helpers.soft_delete_retention_days", return_value=30),
            patch("handlers.settings.helpers.workspace_block_list", return_value=[]),
            patch("handlers.settings._primary_workspace_label", return_value="Workspace A"),
            patch("handlers.settings._instance_fingerprint", return_value="aabb" + "c" * 60),
        ):
            blocks = _build_settings_form("T_PRIMARY").as_form_field()

        element_types = [b["element"]["type"] for b in blocks if b.get("type") == "input" and b.get("element")]
        assert "multi_users_select" in element_types
        assert "radio_buttons" in element_types
        assert "number_input" in element_types
        assert "plain_text_input" in element_types
        assert "multi_static_select" not in element_types
        from slack import actions

        action_ids = [b["element"]["action_id"] for b in blocks if b.get("element", {}).get("action_id")]
        assert actions.CONFIG_SETTINGS_WORKSPACE_BLOCK_LIST in action_ids
        labels = [b.get("label", {}).get("text") for b in blocks if b.get("type") == "input"]
        assert "Workspace Block List" in labels

    def test_non_primary_form_omits_instance_fields(self):
        from handlers.settings import _build_settings_form

        with (
            patch.dict(os.environ, {constants.PRIMARY_WORKSPACE: "T_PRIMARY"}),
            patch("handlers.settings.helpers.allow_private_channels", return_value=False),
            patch("handlers.settings.helpers.extra_manager_user_ids", return_value=[]),
            patch("handlers.settings._primary_workspace_label", return_value="Workspace A"),
        ):
            blocks = _build_settings_form("T_OTHER").as_form_field()

        action_ids = [b["element"]["action_id"] for b in blocks if b.get("element", {}).get("action_id")]
        from slack import actions

        assert actions.CONFIG_SETTINGS_EXTRA_MANAGERS in action_ids
        assert actions.CONFIG_SETTINGS_ALLOW_PRIVATE_CHANNELS in action_ids
        assert actions.CONFIG_SETTINGS_FEDERATION_ENABLED not in action_ids
        assert actions.CONFIG_SETTINGS_RETENTION_DAYS not in action_ids
        assert actions.CONFIG_SETTINGS_WORKSPACE_BLOCK_LIST not in action_ids

    def test_information_is_last_for_primary_and_non_primary(self):
        from handlers.settings import _build_settings_form

        with (
            patch.dict(
                os.environ,
                {constants.PRIMARY_WORKSPACE: "T_PRIMARY", "LOG_LEVEL": "DEBUG", "DATABASE_BACKEND": "sqlite"},
            ),
            patch("handlers.settings.helpers.allow_private_channels", return_value=False),
            patch("handlers.settings.helpers.extra_manager_user_ids", return_value=[]),
            patch("handlers.settings.helpers.federation_enabled", return_value=False),
            patch("handlers.settings.helpers.soft_delete_retention_days", return_value=30),
            patch("handlers.settings.helpers.workspace_block_list", return_value=[]),
            patch("handlers.settings._app_version", return_value="1.7.0"),
            patch("handlers.settings._primary_workspace_label", return_value="Workspace A"),
            patch("handlers.settings._instance_fingerprint", return_value="aabb" + "c" * 60),
            patch("handlers.settings._public_url_label", return_value="https://ws-a.example"),
        ):
            primary = _build_settings_form("T_PRIMARY").as_form_field()
            other = _build_settings_form("T_OTHER").as_form_field()
        fingerprint = "aabb" + "c" * 60
        assert primary[-1]["text"]["text"] == (
            "Version: `1.7.0`\n"
            "Primary Workspace: `Workspace A`\n"
            "Log level: `DEBUG`\n"
            f"Fingerprint: `{fingerprint}`\n"
            "Public URL: `https://ws-a.example`\n"
            "Database: `sqlite`"
        )
        assert other[-1]["text"]["text"] == "Version: `1.7.0`\nPrimary Workspace: `Workspace A`"
        assert "Log level:" not in other[-1]["text"]["text"]
        assert "Fingerprint:" not in other[-1]["text"]["text"]
        assert "Public URL:" not in other[-1]["text"]["text"]
        assert "Database:" not in other[-1]["text"]["text"]
        assert [b["text"]["text"] for b in primary if b.get("type") == "header"][-1] == "Information"

    def test_open_posts_a_modal_for_a_workspace_admin(self):
        from handlers.settings import handle_open_settings

        body = {"team": {"id": "T1"}, "user": {"id": "U1"}, "trigger_id": "tr"}
        client = MagicMock()

        with (
            patch("handlers.settings.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.settings.helpers.is_workspace_admin", return_value=True),
            patch("handlers.settings._build_settings_form") as build_form,
        ):
            mock_form = MagicMock()
            build_form.return_value = mock_form
            handle_open_settings(body, client, MagicMock(), context={})

        build_form.assert_called_once_with("T1", {})
        mock_form.post_modal.assert_called_once()
