"""Add post_meta.posted_as_user_id and sync_channels publishes/subscribes.

Revision ID: 014_posted_as_pub_sub
Revises: 013_user_mapping_mapped_at
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# alembic_version.version_num is VARCHAR(32) on MySQL/TiDB.
revision: str = "014_posted_as_pub_sub"
down_revision: str | None = "013_user_mapping_mapped_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# TINYINT/BOOL default must be 1, not `true` — TiDB rejects DEFAULT true.
_BOOL_TRUE = sa.text("1")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    post_meta_cols = {col["name"] for col in inspector.get_columns("post_meta")}
    if "posted_as_user_id" not in post_meta_cols:
        op.add_column(
            "post_meta",
            sa.Column("posted_as_user_id", sa.String(length=100), nullable=True),
        )

    sync_cols = {col["name"] for col in inspector.get_columns("sync_channels")}
    if "publishes" not in sync_cols:
        op.add_column(
            "sync_channels",
            sa.Column("publishes", sa.Boolean(), nullable=False, server_default=_BOOL_TRUE),
        )
    if "subscribes" not in sync_cols:
        op.add_column(
            "sync_channels",
            sa.Column("subscribes", sa.Boolean(), nullable=False, server_default=_BOOL_TRUE),
        )

    bind.execute(sa.text("UPDATE sync_channels SET publishes = 1 WHERE publishes IS NULL"))
    bind.execute(sa.text("UPDATE sync_channels SET subscribes = 1 WHERE subscribes IS NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    sync_cols = {col["name"] for col in inspector.get_columns("sync_channels")}
    if "subscribes" in sync_cols:
        op.drop_column("sync_channels", "subscribes")
    if "publishes" in sync_cols:
        op.drop_column("sync_channels", "publishes")

    post_meta_cols = {col["name"] for col in inspector.get_columns("post_meta")}
    if "posted_as_user_id" in post_meta_cols:
        op.drop_column("post_meta", "posted_as_user_id")
