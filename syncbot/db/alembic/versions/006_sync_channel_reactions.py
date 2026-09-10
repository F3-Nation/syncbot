"""Add per-channel reaction direction and style.

Revision ID: 006_sync_channel_reactions
Revises: 005_workspace_settings
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006_sync_channel_reactions"
down_revision: str | None = "005_workspace_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "sync_channels"
_COL_DIRECTION = "reaction_direction"
_COL_STYLE = "reaction_style"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COL_DIRECTION not in columns:
        op.add_column(
            _TABLE,
            sa.Column(_COL_DIRECTION, sa.String(length=32), nullable=False, server_default="both"),
        )
    if _COL_STYLE not in columns:
        op.add_column(
            _TABLE,
            sa.Column(_COL_STYLE, sa.String(length=32), nullable=True),
        )
    bind.execute(
        sa.text(
            f"UPDATE {_TABLE} SET {_COL_DIRECTION} = 'both' "
            f"WHERE {_COL_DIRECTION} IS NULL OR TRIM({_COL_DIRECTION}) = ''"
        )
    )
    bind.execute(
        sa.text(
            f"UPDATE {_TABLE} SET {_COL_STYLE} = 'threaded_and_direct' "
            f"WHERE {_COL_STYLE} IS NULL OR TRIM({_COL_STYLE}) = ''"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COL_STYLE in columns:
        op.drop_column(_TABLE, _COL_STYLE)
    if _COL_DIRECTION in columns:
        op.drop_column(_TABLE, _COL_DIRECTION)
