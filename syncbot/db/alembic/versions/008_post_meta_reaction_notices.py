"""Add PostMeta fields for Hybrid reaction notices.

Revision ID: 008_post_meta_reaction_notices
Revises: 007_user_action_echoes
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "008_post_meta_reaction_notices"
down_revision: str | None = "007_user_action_echoes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "post_meta"
_INDEX = "ix_post_meta_notice_lookup"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns(_TABLE)}
    indexes = {idx["name"] for idx in inspector.get_indexes(_TABLE)}

    if "kind" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("kind", sa.String(length=32), nullable=False, server_default="message"),
        )
    if "parent_post_id" not in columns:
        op.add_column(_TABLE, sa.Column("parent_post_id", sa.String(length=100), nullable=True))
    if "reaction" not in columns:
        op.add_column(_TABLE, sa.Column("reaction", sa.String(length=100), nullable=True))
    if "source_user_id" not in columns:
        op.add_column(_TABLE, sa.Column("source_user_id", sa.String(length=100), nullable=True))
    if "source_workspace_id" not in columns:
        op.add_column(_TABLE, sa.Column("source_workspace_id", sa.Integer(), nullable=True))

    bind.execute(sa.text(f"UPDATE {_TABLE} SET kind = 'message' WHERE kind IS NULL OR TRIM(kind) = ''"))

    if _INDEX not in indexes:
        op.create_index(
            _INDEX,
            _TABLE,
            ["parent_post_id", "reaction", "source_user_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    columns = {col["name"] for col in inspector.get_columns(_TABLE)}

    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)
    for col in ("source_workspace_id", "source_user_id", "reaction", "parent_post_id", "kind"):
        if col in columns:
            op.drop_column(_TABLE, col)
