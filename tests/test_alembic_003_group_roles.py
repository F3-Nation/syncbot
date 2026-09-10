"""Migration tests for 003_group_roles.

Covers both shapes the migration has to survive: a legacy database that still
has ``workspace_groups.created_by_workspace_id`` and ``role='creator'`` rows,
and a fresh database built by ``001_baseline``'s ``metadata.create_all`` from the
current models, which never had the column at all.
"""

import os

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from unittest.mock import patch  # noqa: E402

from alembic import command  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402


@pytest.fixture
def legacy_db(tmp_path):
    """A database rewound to 002 with the pre-003 group schema restored."""
    import db as db_mod
    from db import get_engine, initialize_database

    url = f"sqlite:///{tmp_path / 'alembic003.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            engine = get_engine()
            with engine.begin() as conn:
                conn.execute(text("UPDATE alembic_version SET version_num = '002_processed_events'"))
                conn.execute(text("ALTER TABLE workspace_groups ADD COLUMN created_by_workspace_id INTEGER"))
            yield engine
        finally:
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema


@pytest.fixture
def fresh_db(tmp_path):
    """A database rewound to 002 without the legacy column, as create_all builds it."""
    import db as db_mod
    from db import get_engine, initialize_database

    url = f"sqlite:///{tmp_path / 'alembic003fresh.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            engine = get_engine()
            with engine.begin() as conn:
                conn.execute(text("UPDATE alembic_version SET version_num = '002_processed_events'"))
            yield engine
        finally:
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema


def _seed_group(conn, group_id, invite_code, members):
    """Insert one active group plus *members* as (id, workspace_id, role, status, deleted_at, joined_at)."""
    conn.execute(
        text(
            "INSERT INTO workspace_groups (id, name, invite_code, status, created_at) "
            "VALUES (:id, :name, :code, 'active', '2026-01-01 00:00:00')"
        ),
        {"id": group_id, "name": f"Group {group_id}", "code": invite_code},
    )
    for member_id, workspace_id, role, status, deleted_at, joined_at in members:
        # FKs are enforced in tests (conftest sets PRAGMA foreign_keys=ON), so the
        # referenced workspace has to exist.
        if workspace_id is not None:
            conn.execute(
                text("INSERT OR IGNORE INTO workspaces (id, team_id, workspace_name) VALUES (:id, :team, :name)"),
                {"id": workspace_id, "team": f"T{workspace_id}", "name": f"WS {workspace_id}"},
            )
        conn.execute(
            text(
                "INSERT INTO workspace_group_members "
                "(id, group_id, workspace_id, status, role, joined_at, deleted_at) "
                "VALUES (:id, :gid, :wid, :status, :role, :joined, :deleted)"
            ),
            {
                "id": member_id,
                "gid": group_id,
                "wid": workspace_id,
                "status": status,
                "role": role,
                "joined": joined_at,
                "deleted": deleted_at,
            },
        )


def _roles(engine, group_id):
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, role, status, deleted_at FROM workspace_group_members WHERE group_id = :gid ORDER BY id"),
            {"gid": group_id},
        ).fetchall()
    return rows


def _upgrade():
    from db import _alembic_config

    command.upgrade(_alembic_config(), "head")


class TestRoleRename:
    def test_creator_becomes_owner(self, legacy_db):
        with legacy_db.begin() as conn:
            _seed_group(
                conn,
                1,
                "AAA-111",
                [
                    (10, 100, "creator", "active", None, "2026-01-01 00:00:00"),
                    (11, 101, "member", "active", None, "2026-01-02 00:00:00"),
                ],
            )

        _upgrade()

        roles = {row[0]: row[1] for row in _roles(legacy_db, 1)}
        assert roles == {10: "owner", 11: "member"}

    def test_multiple_creators_all_become_owners(self, legacy_db):
        """Reachable through the restore paths; multiple owners is now a legal state."""
        with legacy_db.begin() as conn:
            _seed_group(
                conn,
                2,
                "BBB-222",
                [
                    (20, 200, "creator", "active", None, "2026-01-01 00:00:00"),
                    (21, 201, "creator", "active", None, "2026-01-02 00:00:00"),
                    (22, 202, "member", "active", None, "2026-01-03 00:00:00"),
                ],
            )

        _upgrade()

        roles = {row[0]: row[1] for row in _roles(legacy_db, 2)}
        assert roles == {20: "owner", 21: "owner", 22: "member"}


class TestOwnerBackfill:
    def test_group_with_no_active_owner_promotes_earliest_joined_member(self, legacy_db):
        """The creator left, so its membership is soft-deleted and the group has no owner."""
        with legacy_db.begin() as conn:
            _seed_group(
                conn,
                3,
                "CCC-333",
                [
                    (30, 300, "creator", "active", "2026-02-01 00:00:00", "2026-01-01 00:00:00"),
                    (31, 301, "member", "active", None, "2026-01-05 00:00:00"),
                    (32, 302, "member", "active", None, "2026-01-03 00:00:00"),
                ],
            )

        _upgrade()

        roles = {row[0]: row[1] for row in _roles(legacy_db, 3)}
        # 32 joined before 31, so it is the successor.
        assert roles[32] == "owner"
        assert roles[31] == "member"

    def test_group_with_an_active_owner_is_left_alone(self, legacy_db):
        with legacy_db.begin() as conn:
            _seed_group(
                conn,
                4,
                "DDD-444",
                [
                    (40, 400, "creator", "active", None, "2026-01-01 00:00:00"),
                    (41, 401, "member", "active", None, "2026-01-02 00:00:00"),
                ],
            )

        _upgrade()

        roles = {row[0]: row[1] for row in _roles(legacy_db, 4)}
        assert roles == {40: "owner", 41: "member"}

    def test_federated_only_group_has_nothing_to_promote(self, legacy_db):
        """No local member means no candidate; the succession ladder handles it at runtime."""
        with legacy_db.begin() as conn:
            _seed_group(
                conn,
                5,
                "EEE-555",
                [(50, None, "member", "active", None, "2026-01-01 00:00:00")],
            )

        _upgrade()

        roles = {row[0]: row[1] for row in _roles(legacy_db, 5)}
        assert roles == {50: "member"}

    def test_every_active_group_ends_with_at_least_one_owner(self, legacy_db):
        with legacy_db.begin() as conn:
            _seed_group(conn, 6, "FFF-666", [(60, 600, "member", "active", None, "2026-01-01 00:00:00")])
            _seed_group(conn, 7, "GGG-777", [(70, 700, "creator", "active", None, "2026-01-01 00:00:00")])

        _upgrade()

        for group_id in (6, 7):
            owners = [row for row in _roles(legacy_db, group_id) if row[1] == "owner"]
            assert owners, f"group {group_id} has no owner"


class TestColumnDrop:
    def test_created_by_workspace_id_is_dropped(self, legacy_db):
        columns = {col["name"] for col in inspect(legacy_db).get_columns("workspace_groups")}
        assert "created_by_workspace_id" in columns

        _upgrade()

        columns = {col["name"] for col in inspect(legacy_db).get_columns("workspace_groups")}
        assert "created_by_workspace_id" not in columns

    def test_migration_is_a_noop_on_a_fresh_database(self, fresh_db):
        """001_baseline builds fresh DBs from current models, so the column never existed."""
        columns = {col["name"] for col in inspect(fresh_db).get_columns("workspace_groups")}
        assert "created_by_workspace_id" not in columns

        _upgrade()

        columns = {col["name"] for col in inspect(fresh_db).get_columns("workspace_groups")}
        assert "created_by_workspace_id" not in columns

    def test_upgrade_is_idempotent(self, legacy_db):
        _upgrade()
        _upgrade()

        columns = {col["name"] for col in inspect(legacy_db).get_columns("workspace_groups")}
        assert "created_by_workspace_id" not in columns
