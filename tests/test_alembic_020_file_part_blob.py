"""Alembic 020: file-part MEDIUMBLOB, team_id length, hot-path indexes."""

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
from sqlalchemy.dialects import mysql  # noqa: E402

_HOT_INDEXES = (
    "ix_post_meta_post_channel",
    "ix_post_meta_channel_ts",
    "ix_post_meta_ts",
    "ix_sync_channels_channel_id",
)


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database

    url = f"sqlite:///{tmp_path / 'alembic020.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            yield
        finally:
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema


def test_020_upgrades_sqlite_from_019_and_indexes(real_db):
    from db import _alembic_config, get_engine, schemas

    engine = get_engine()
    with engine.begin() as conn:
        for name in _HOT_INDEXES:
            conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
        conn.execute(text("UPDATE alembic_version SET version_num = '019_sync_channel_name'"))
    command.upgrade(_alembic_config(), "020_file_part_blob_indexes")
    inspector = inspect(engine)
    post_indexes = {idx["name"] for idx in inspector.get_indexes("post_meta")}
    channel_indexes = {idx["name"] for idx in inspector.get_indexes("sync_channels")}
    assert "ix_post_meta_post_channel" in post_indexes
    assert "ix_post_meta_channel_ts" in post_indexes
    assert "ix_post_meta_ts" in post_indexes
    assert "ix_sync_channels_channel_id" in channel_indexes
    assert schemas.Workspace.team_id.type.length == 32
    assert schemas.Instance.primary_team_id.type.length == 32
    compiled = str(schemas.FederationFilePart.payload.type.compile(dialect=mysql.dialect()))
    assert "MEDIUMBLOB" in compiled.upper()


def test_file_part_larger_than_64kib_stores(real_db):
    from db import DbManager, schemas
    from federation.files import upsert_file_part

    payload = b"x" * 70_000
    status, body = upsert_file_part(
        sha256="a" * 64,
        part_index=0,
        total=1,
        size=len(payload),
        payload=payload,
    )
    assert status == 200
    assert body.get("ok") is True
    rows = DbManager.find_records(
        schemas.FederationFilePart,
        [schemas.FederationFilePart.sha256 == "a" * 64],
    )
    assert len(rows) == 1
    assert len(bytes(rows[0].payload or b"")) > 64_000
