"""Add user_action_echoes for user-token write echo suppression.

Revision ID: 007_user_action_echoes
Revises: 006_sync_channel_reactions
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007_user_action_echoes"
down_revision: str | None = "006_sync_channel_reactions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_action_echoes" in inspector.get_table_names():
        return
    op.create_table(
        "user_action_echoes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("team_id", sa.String(length=100), nullable=False),
        sa.Column("user_id", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "team_id",
            "user_id",
            "kind",
            "fingerprint",
            name="uq_user_action_echoes_team_user_kind_fp",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_action_echoes" in inspector.get_table_names():
        op.drop_table("user_action_echoes")
