"""MEDIUMBLOB file parts, Slack team_id length 32, hot-path indexes.

Revision ID: 020_file_part_blob_indexes
Revises: 019_sync_channel_name
Create Date: 2026-09-17

Do not edit applied 001–019. SQLite ignores VARCHAR length and already stores
BLOB without a 64 KiB cap. MySQL InnoDB already indexes FK columns — skip a
named index when those leading columns exist.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import MEDIUMBLOB

revision: str = "020_file_part_blob_indexes"
down_revision: str | None = "019_sync_channel_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TEAM_ID_TABLES = (
    ("workspaces", "team_id", True),
    ("instances", "primary_team_id", True),
    ("processed_events", "team_id", False),
    ("user_action_echoes", "team_id", False),
)

_INDEXES = (
    ("post_meta", "ix_post_meta_post_channel", ("post_id", "sync_channel_id")),
    ("post_meta", "ix_post_meta_channel_ts", ("sync_channel_id", "ts")),
    ("post_meta", "ix_post_meta_ts", ("ts",)),
    ("sync_channels", "ix_sync_channels_channel_id", ("channel_id",)),
    (
        "user_mappings",
        "ix_user_mappings_source_target",
        ("source_workspace_id", "source_user_id", "target_workspace_id"),
    ),
    ("sync_channels", "ix_sync_channels_sync_id", ("sync_id",)),
    ("sync_channels", "ix_sync_channels_workspace_id", ("workspace_id",)),
    ("workspace_group_members", "ix_workspace_group_members_group_id", ("group_id",)),
    ("workspace_group_members", "ix_workspace_group_members_workspace_id", ("workspace_id",)),
    ("workspaces", "ix_workspaces_instance_id", ("instance_id",)),
    ("syncs", "ix_syncs_group_id", ("group_id",)),
)


def _table_names(inspector) -> set[str]:
    return set(inspector.get_table_names())


def _has_index(inspector, table: str, name: str, leading: tuple[str, ...]) -> bool:
    if table not in _table_names(inspector):
        return True
    for idx in inspector.get_indexes(table):
        if idx.get("name") == name:
            return True
        cols = list(idx.get("column_names") or [])
        if list(cols[: len(leading)]) == list(leading):
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    if dialect == "mysql" and "federation_file_parts" in _table_names(inspector):
        op.alter_column(
            "federation_file_parts",
            "payload",
            existing_type=sa.LargeBinary(),
            type_=MEDIUMBLOB(),
            existing_nullable=False,
        )

    if dialect != "sqlite":
        for table, column, nullable in _TEAM_ID_TABLES:
            if table not in _table_names(inspector):
                continue
            cols = {col["name"]: col for col in inspector.get_columns(table)}
            col = cols.get(column)
            if not col:
                continue
            length = getattr(col.get("type"), "length", None)
            if length == 32:
                continue
            op.alter_column(
                table,
                column,
                existing_type=sa.String(length=length or 100),
                type_=sa.String(length=32),
                existing_nullable=nullable if col.get("nullable") is None else col.get("nullable"),
            )

    for table, name, leading in _INDEXES:
        if _has_index(inspector, table, name, leading):
            continue
        op.create_index(name, table, list(leading))
        inspector = sa.inspect(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, name, _leading in reversed(_INDEXES):
        if table not in _table_names(inspector):
            continue
        names = {idx.get("name") for idx in inspector.get_indexes(table)}
        if name in names:
            op.drop_index(name, table_name=table)
        inspector = sa.inspect(bind)
    if bind.dialect.name == "mysql" and "federation_file_parts" in _table_names(inspector):
        op.alter_column(
            "federation_file_parts",
            "payload",
            existing_type=MEDIUMBLOB(),
            type_=sa.LargeBinary(),
            existing_nullable=False,
        )
