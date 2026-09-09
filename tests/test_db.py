"""Unit tests for ``syncbot/db`` connection pooling, retry logic, and backend parity (MySQL/SQLite)."""

import contextlib
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from db import _MAX_RETRIES, _is_retryable_db_error, _with_retry

# -----------------------------------------------------------------------
# _with_retry decorator
# -----------------------------------------------------------------------


class TestWithRetry:
    def test_success_no_retry(self):
        call_count = 0

        @_with_retry
        def fn():
            nonlocal call_count
            call_count += 1
            return "ok"

        assert fn() == "ok"
        assert call_count == 1

    def test_retries_on_operational_error(self):
        call_count = 0

        @_with_retry
        def fn():
            nonlocal call_count
            call_count += 1
            if call_count <= _MAX_RETRIES:
                raise OperationalError("statement", {}, Exception("connection lost"))
            return "recovered"

        assert fn() == "recovered"
        assert call_count == _MAX_RETRIES + 1

    def test_exhausts_retries_raises(self):
        @_with_retry
        def fn():
            raise OperationalError("statement", {}, Exception("connection lost"))

        with pytest.raises(OperationalError):
            fn()

    def test_non_operational_error_not_retried(self):
        call_count = 0

        @_with_retry
        def fn():
            nonlocal call_count
            call_count += 1
            raise ValueError("not a db error")

        with pytest.raises(ValueError):
            fn()
        assert call_count == 1

    def test_unknown_column_is_not_retried(self):
        call_count = 0

        class _UnknownColumn(Exception):
            args = (1054, "Unknown column 'sync_channels.reaction_direction' in 'field list'")

        @_with_retry
        def fn():
            nonlocal call_count
            call_count += 1
            raise OperationalError("SELECT …", {}, _UnknownColumn())

        with pytest.raises(OperationalError):
            fn()
        assert call_count == 1

    def test_connection_lost_is_still_retryable(self):
        assert _is_retryable_db_error(OperationalError("statement", {}, Exception("connection lost"))) is True
        assert _is_retryable_db_error(ProgrammingError("statement", {}, Exception("syntax error"))) is False

    def test_wrapped_programming_error_is_not_retryable(self):
        inner = ProgrammingError("SELECT … WHERE key = %(key)s", {}, Exception("syntax error"))
        outer = RuntimeError("alembic upgrade failed")
        outer.__cause__ = inner
        assert _is_retryable_db_error(outer) is False

    def test_wrapped_connection_error_is_retryable(self):
        inner = OperationalError("statement", {}, Exception("connection lost"))
        outer = RuntimeError("alembic upgrade failed")
        outer.__cause__ = inner
        assert _is_retryable_db_error(outer) is True


# -----------------------------------------------------------------------
# Engine creation uses QueuePool
# -----------------------------------------------------------------------


class TestEngineConfig:
    @patch.dict(
        os.environ,
        {
            "DATABASE_BACKEND": "mysql",
            "DATABASE_HOST": "localhost",
            "DATABASE_USER": "root",
            "DATABASE_PASSWORD": "test",
            "DATABASE_SCHEMA": "syncbot",
        },
        clear=False,
    )
    def test_engine_uses_queue_pool_mysql(self):
        from sqlalchemy.pool import QueuePool

        import db as db_mod
        from db import get_engine

        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        engine = None
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            engine = get_engine(schema="test_schema_unique")
            assert isinstance(engine.pool, QueuePool)
        finally:
            if engine:
                engine.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema

    @patch.dict(
        os.environ,
        {
            "DATABASE_BACKEND": "postgresql",
            "DATABASE_HOST": "localhost",
            "DATABASE_USER": "root",
            "DATABASE_PASSWORD": "test",
            "DATABASE_SCHEMA": "syncbot",
        },
        clear=False,
    )
    def test_engine_uses_queue_pool_postgresql(self):
        from sqlalchemy.pool import QueuePool

        import db as db_mod
        from db import get_engine

        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        engine = None
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            engine = get_engine(schema="test_schema_unique_pg")
            assert isinstance(engine.pool, QueuePool)
        finally:
            if engine:
                engine.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema

    @patch.dict(
        os.environ,
        {
            "DATABASE_BACKEND": "sqlite",
            "DATABASE_URL": "sqlite:///:memory:",
        },
        clear=False,
    )
    def test_engine_uses_null_pool_sqlite(self):
        from sqlalchemy.pool import NullPool

        import db as db_mod
        from db import get_engine

        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        engine = None
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            engine = get_engine()
            assert isinstance(engine.pool, NullPool)
        finally:
            if engine:
                engine.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema


# -----------------------------------------------------------------------
# Backend parity: SQLite bootstrap and required vars
# -----------------------------------------------------------------------


class TestBackendParity:
    @pytest.mark.parametrize("sqlite_url", ["sqlite:///test_bootstrap.db"])
    @patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite"}, clear=False)
    def test_sqlite_initialize_database_creates_tables(self, sqlite_url):
        import db as db_mod
        from db import get_engine, initialize_database

        os.environ["DATABASE_URL"] = sqlite_url
        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            engine = get_engine()
            insp = inspect(engine)
            assert insp.has_table("workspaces")
            assert insp.has_table("alembic_version")
            assert insp.has_table("slack_bots")
            assert insp.has_table("processed_events")
            assert insp.has_table("user_action_echoes")
        finally:
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema
            if "DATABASE_URL" in os.environ and "test_bootstrap" in os.environ["DATABASE_URL"]:
                with contextlib.suppress(Exception):
                    (__import__("pathlib").Path("test_bootstrap.db")).unlink(missing_ok=True)

    def test_alembic_002_creates_processed_events_when_missing(self, tmp_path):
        """Existing 001 installs do not get the table from create_all; 002 must create it."""
        from alembic import command

        import db as db_mod
        from db import _alembic_config, get_engine, initialize_database

        url = f"sqlite:///{tmp_path / 'alembic002.db'}"
        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
            try:
                db_mod.GLOBAL_ENGINE = None
                db_mod.GLOBAL_SCHEMA = None
                initialize_database()
                engine = get_engine()
                with engine.begin() as conn:
                    conn.execute(text("DROP TABLE IF EXISTS processed_events"))
                    conn.execute(text("UPDATE alembic_version SET version_num = '001_baseline'"))
                command.upgrade(_alembic_config(), "head")
                assert inspect(engine).has_table("processed_events")
                uniques = {u["name"] for u in inspect(engine).get_unique_constraints("processed_events")}
                assert "uq_processed_events_team_event" in uniques
            finally:
                if db_mod.GLOBAL_ENGINE:
                    db_mod.GLOBAL_ENGINE.dispose()
                db_mod.GLOBAL_ENGINE = old_engine
                db_mod.GLOBAL_SCHEMA = old_schema

    def test_alembic_002_skips_when_processed_events_already_exists(self, tmp_path):
        """Fresh DBs already have the table from 001 create_all; 002 must be a no-op."""
        from alembic import command

        import db as db_mod
        from db import _alembic_config, get_engine, initialize_database

        url = f"sqlite:///{tmp_path / 'alembic002skip.db'}"
        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
            try:
                db_mod.GLOBAL_ENGINE = None
                db_mod.GLOBAL_SCHEMA = None
                initialize_database()
                engine = get_engine()
                with engine.begin() as conn:
                    conn.execute(text("UPDATE alembic_version SET version_num = '001_baseline'"))
                command.upgrade(_alembic_config(), "head")
                assert inspect(engine).has_table("processed_events")
            finally:
                if db_mod.GLOBAL_ENGINE:
                    db_mod.GLOBAL_ENGINE.dispose()
                db_mod.GLOBAL_ENGINE = old_engine
                db_mod.GLOBAL_SCHEMA = old_schema

    def test_get_required_db_vars_mysql_without_url(self):
        with patch.dict(os.environ, {"DATABASE_BACKEND": "mysql"}, clear=False):
            if "DATABASE_URL" in os.environ:
                del os.environ["DATABASE_URL"]
            from constants import get_required_db_vars

            required = get_required_db_vars()
            assert "DATABASE_HOST" in required
            assert "DATABASE_USER" in required
            assert "DATABASE_PASSWORD" in required
            assert "DATABASE_SCHEMA" in required

    def test_get_required_db_vars_sqlite(self):
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite"}, clear=False):
            from constants import get_required_db_vars

            required = get_required_db_vars()
            assert required == ["DATABASE_URL"]

    def test_get_required_db_vars_postgresql_without_url(self):
        with patch.dict(
            os.environ,
            {"DATABASE_BACKEND": "postgresql"},
            clear=False,
        ):
            if "DATABASE_URL" in os.environ:
                del os.environ["DATABASE_URL"]
            from constants import get_required_db_vars

            required = get_required_db_vars()
            assert "DATABASE_HOST" in required
            assert "DATABASE_USER" in required
            assert "DATABASE_PASSWORD" in required
            assert "DATABASE_SCHEMA" in required

    def test_default_database_backend_is_mysql(self):
        import importlib

        import constants as c

        old = os.environ.pop("DATABASE_BACKEND", None)
        try:
            importlib.reload(c)
            assert c.get_database_backend() == "mysql"
        finally:
            if old is not None:
                os.environ["DATABASE_BACKEND"] = old
            else:
                os.environ.setdefault("DATABASE_BACKEND", "mysql")
            importlib.reload(c)


class TestInitializeDatabaseRetry:
    def test_syntax_error_is_not_retried(self):
        import db as db_mod

        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise ProgrammingError(
                "SELECT value FROM instance_settings WHERE key = %(key)s",
                {},
                Exception("You have an error in your SQL syntax"),
            )

        with (
            patch.object(db_mod, "_run_alembic_upgrade", boom),
            patch.object(db_mod, "_is_network_sql_backend", return_value=False),
            patch.object(db_mod.time, "sleep") as sleep,
            pytest.raises(ProgrammingError),
        ):
            db_mod.initialize_database()
        assert calls["n"] == 1
        sleep.assert_not_called()


class TestAlembic011MapMethod:
    def test_renames_match_method_when_rewound_to_010(self, tmp_path):
        from alembic import command

        import db as db_mod
        from db import _alembic_config, get_engine, initialize_database

        url = f"sqlite:///{tmp_path / 'alembic011.db'}"
        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
            try:
                db_mod.GLOBAL_ENGINE = None
                db_mod.GLOBAL_SCHEMA = None
                initialize_database()
                engine = get_engine()
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE user_mappings RENAME COLUMN map_method TO match_method"))
                    conn.execute(text("UPDATE alembic_version SET version_num = '010_instance_key_instance_id'"))
                command.upgrade(_alembic_config(), "head")
                names = {c["name"] for c in inspect(engine).get_columns("user_mappings")}
                assert "map_method" in names
                assert "match_method" not in names
            finally:
                if db_mod.GLOBAL_ENGINE:
                    db_mod.GLOBAL_ENGINE.dispose()
                db_mod.GLOBAL_ENGINE = old_engine
                db_mod.GLOBAL_SCHEMA = old_schema


class TestAlembic012FederationEndpoint:
    def test_appends_mount_path_to_bare_origin(self, tmp_path):
        from alembic import command

        import db as db_mod
        from db import _alembic_config, get_engine, initialize_database

        url = f"sqlite:///{tmp_path / 'alembic012.db'}"
        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
            try:
                db_mod.GLOBAL_ENGINE = None
                db_mod.GLOBAL_SCHEMA = None
                initialize_database()
                engine = get_engine()
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO federated_workspaces "
                            "(instance_id, webhook_url, public_key, status, created_at) VALUES "
                            "('bare', 'https://peer.example', 'PEM', 'active', '2026-01-01'),"
                            "('full', 'https://other.example/api/federation', 'PEM', 'active', '2026-01-01')"
                        )
                    )
                    conn.execute(text("UPDATE alembic_version SET version_num = '011_user_mapping_map_method'"))
                command.upgrade(_alembic_config(), "head")
                with engine.begin() as conn:
                    rows = dict(conn.execute(text("SELECT instance_id, webhook_url FROM federated_workspaces")).all())
                assert rows["bare"] == "https://peer.example/api/federation"
                assert rows["full"] == "https://other.example/api/federation"
            finally:
                if db_mod.GLOBAL_ENGINE:
                    db_mod.GLOBAL_ENGINE.dispose()
                db_mod.GLOBAL_ENGINE = old_engine
                db_mod.GLOBAL_SCHEMA = old_schema


class TestAlembic013MappedAt:
    def test_renames_matched_at_when_rewound_to_012(self, tmp_path):
        from alembic import command

        import db as db_mod
        from db import _alembic_config, get_engine, initialize_database

        url = f"sqlite:///{tmp_path / 'alembic013.db'}"
        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
            try:
                db_mod.GLOBAL_ENGINE = None
                db_mod.GLOBAL_SCHEMA = None
                initialize_database()
                engine = get_engine()
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE user_mappings RENAME COLUMN mapped_at TO matched_at"))
                    conn.execute(text("UPDATE alembic_version SET version_num = '012_federation_webhook_endpoint'"))
                command.upgrade(_alembic_config(), "head")
                names = {c["name"] for c in inspect(engine).get_columns("user_mappings")}
                assert "mapped_at" in names
                assert "matched_at" not in names
            finally:
                if db_mod.GLOBAL_ENGINE:
                    db_mod.GLOBAL_ENGINE.dispose()
                db_mod.GLOBAL_ENGINE = old_engine
                db_mod.GLOBAL_SCHEMA = old_schema


class TestAlembicRevisionIds:
    def test_revision_ids_fit_mysql_version_num(self):
        from alembic.script import ScriptDirectory

        from db import _alembic_config

        script = ScriptDirectory.from_config(_alembic_config())
        too_long = [rev.revision for rev in script.walk_revisions() if len(rev.revision) > 32]
        assert too_long == [], f"alembic_version.version_num is VARCHAR(32): {too_long}"


class TestAlembic014PostedAsAndPublishSubscribe:
    def test_adds_posted_as_and_participation_when_rewound_to_013(self, tmp_path):
        from alembic import command

        import db as db_mod
        from db import _alembic_config, get_engine, initialize_database

        url = f"sqlite:///{tmp_path / 'alembic014.db'}"
        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
            try:
                db_mod.GLOBAL_ENGINE = None
                db_mod.GLOBAL_SCHEMA = None
                initialize_database()
                engine = get_engine()
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE post_meta DROP COLUMN posted_as_user_id"))
                    conn.execute(text("ALTER TABLE sync_channels DROP COLUMN publishes"))
                    conn.execute(text("ALTER TABLE sync_channels DROP COLUMN subscribes"))
                    conn.execute(text("UPDATE alembic_version SET version_num = '013_user_mapping_mapped_at'"))
                command.upgrade(_alembic_config(), "head")
                post_cols = {c["name"] for c in inspect(engine).get_columns("post_meta")}
                sync_cols = {c["name"] for c in inspect(engine).get_columns("sync_channels")}
                assert "posted_as_user_id" in post_cols
                assert "publishes" in sync_cols
                assert "subscribes" in sync_cols
            finally:
                if db_mod.GLOBAL_ENGINE:
                    db_mod.GLOBAL_ENGINE.dispose()
                db_mod.GLOBAL_ENGINE = old_engine
                db_mod.GLOBAL_SCHEMA = old_schema


class TestAlembic009WidenTokens:
    def test_widens_varchar_token_column_when_rewound_to_008(self, tmp_path):
        from alembic import command

        import db as db_mod
        from db import _alembic_config, get_engine, initialize_database

        url = f"sqlite:///{tmp_path / 'alembic009.db'}"
        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
            try:
                db_mod.GLOBAL_ENGINE = None
                db_mod.GLOBAL_SCHEMA = None
                initialize_database()
                engine = get_engine()
                with engine.begin() as conn:
                    conn.execute(text("DROP TABLE slack_bots"))
                    conn.execute(
                        text(
                            "CREATE TABLE slack_bots ("
                            "id INTEGER PRIMARY KEY, "
                            "bot_token VARCHAR(200), "
                            "bot_refresh_token VARCHAR(200))"
                        )
                    )
                    conn.execute(text("UPDATE alembic_version SET version_num = '008_post_meta_reaction_notices'"))
                command.upgrade(_alembic_config(), "head")
                types = {c["name"]: str(c["type"]).lower() for c in inspect(engine).get_columns("slack_bots")}
                assert "text" in types["bot_token"]
                assert "text" in types["bot_refresh_token"]
            finally:
                if db_mod.GLOBAL_ENGINE:
                    db_mod.GLOBAL_ENGINE.dispose()
                db_mod.GLOBAL_ENGINE = old_engine
                db_mod.GLOBAL_SCHEMA = old_schema
