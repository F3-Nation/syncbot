"""Tests for per-workspace settings and migration 005."""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from sqlalchemy import inspect, text  # noqa: E402

import constants  # noqa: E402
from db import DbManager, schemas  # noqa: E402


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database
    from helpers._cache import clear_all_caches

    url = f"sqlite:///{tmp_path / 'workspace_settings.db'}"
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


def _create_workspace(team_id: str = "T1") -> schemas.Workspace:
    return DbManager.create_record(
        schemas.Workspace(
            team_id=team_id,
            workspace_name="Test",
            bot_token="enc:xoxb-test",
        )
    )


class TestMigration005:
    def test_workspace_settings_table_exists(self, real_db):
        from db import get_engine

        assert inspect(get_engine()).has_table("workspace_settings")

    def test_copies_legacy_instance_private_flag(self, real_db):
        from alembic import command

        from db import _alembic_config, get_engine

        ws1 = _create_workspace("T1")
        _create_workspace("T2")
        DbManager.create_record(
            schemas.InstanceSetting(
                key=constants.SETTING_ALLOW_PRIVATE_CHANNELS,
                value="true",
            )
        )

        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '004_instance_settings'"))

        command.upgrade(_alembic_config(), "head")

        rows = DbManager.find_records(
            schemas.WorkspaceSetting,
            [
                schemas.WorkspaceSetting.workspace_id == ws1.id,
                schemas.WorkspaceSetting.key == constants.SETTING_ALLOW_PRIVATE_CHANNELS,
            ],
        )
        assert len(rows) == 1
        assert rows[0].value == "true"

    def test_mysql_quotes_reserved_key_column(self):
        import importlib.util
        from pathlib import Path

        from sqlalchemy import insert, select
        from sqlalchemy.dialects.mysql.base import MySQLDialect

        path = Path(__file__).resolve().parents[1] / "syncbot/db/alembic/versions/005_workspace_settings.py"
        spec = importlib.util.spec_from_file_location("alembic_005_workspace_settings", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        dialect = MySQLDialect()
        inst = mod._instance_settings()
        ws_settings = mod._workspace_settings()
        select_sql = str(select(inst.c.value).where(inst.c.key == "allow_private_channels").compile(dialect=dialect))
        insert_sql = str(
            insert(ws_settings)
            .values(workspace_id=1, key="allow_private_channels", value="true")
            .compile(dialect=dialect)
        )
        assert "`key`" in select_sql
        assert "`key`" in insert_sql
        assert "WHERE key =" not in select_sql
        assert "WHERE instance_settings.key" not in select_sql


class TestAllowPrivateChannelsPerWorkspace:
    def test_default_off_without_row(self, real_db):
        import helpers

        _create_workspace("T1")
        assert helpers.allow_private_channels("T1") is False

    def test_leftover_env_is_warned_and_ignored(self, real_db):
        import helpers
        from helpers import workspace_settings as ws_mod

        _create_workspace("T1")
        ws_mod._ALLOW_PRIVATE_ENV_WARNED = False
        with patch.dict(os.environ, {constants.ALLOW_PRIVATE_CHANNELS: "true"}):
            assert helpers.allow_private_channels("T1") is False
        assert ws_mod._ALLOW_PRIVATE_ENV_WARNED is True

    def test_workspace_value_is_used(self, real_db):
        import helpers

        ws = _create_workspace("T1")
        helpers.set_workspace_setting(ws.id, constants.SETTING_ALLOW_PRIVATE_CHANNELS, "true", team_id="T1")
        assert helpers.allow_private_channels("T1") is True
        assert helpers.allow_private_channels("T_MISSING") is False


class TestExtraManagers:
    def test_round_trip(self, real_db):
        import helpers

        _create_workspace("T1")
        helpers.set_extra_manager_user_ids("T1", ["U_EXTRA", "B_BOT", "U2"])
        assert helpers.extra_manager_user_ids("T1") == ["U2", "U_EXTRA"]

    def test_manager_includes_admin_and_extra(self, real_db):
        import helpers

        _create_workspace("T1")
        helpers.set_extra_manager_user_ids("T1", ["U_EXTRA"])
        client = MagicMock()

        with patch("helpers.slack_api._users_info", return_value={"user": {"is_admin": False, "is_owner": False}}):
            assert helpers.is_workspace_manager(client, "U_EXTRA", "T1") is True
            assert helpers.is_workspace_manager(client, "U_MEMBER", "T1") is False

        with patch("helpers.slack_api._users_info", return_value={"user": {"is_admin": True, "is_owner": False}}):
            assert helpers.is_workspace_manager(client, "U_ADMIN", "T1") is True


class TestMigration006:
    def test_existing_rows_get_hybrid_style(self, real_db):
        from datetime import UTC, datetime

        from alembic import command

        from db import _alembic_config, get_engine

        ws = _create_workspace("T_REACT")
        sync = DbManager.create_record(schemas.Sync(title="S", sync_mode="group", publisher_workspace_id=ws.id))
        channel = DbManager.create_record(
            schemas.SyncChannel(
                sync_id=sync.id,
                workspace_id=ws.id,
                channel_id="C_EXISTING",
                created_at=datetime.now(UTC),
            )
        )

        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE sync_channels SET reaction_style = NULL WHERE id = :id"),
                {"id": channel.id},
            )
            conn.execute(text("UPDATE alembic_version SET version_num = '005_workspace_settings'"))

        command.upgrade(_alembic_config(), "head")

        rows = DbManager.find_records(schemas.SyncChannel, [schemas.SyncChannel.id == channel.id])
        assert rows[0].reaction_direction == "both"
        assert rows[0].reaction_style == "threaded_and_direct"
