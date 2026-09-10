"""Add instance_settings for operator-managed policy.

Revision ID: 004_instance_settings
Revises: 003_group_roles
Create Date: 2026-08-30

Key/value on purpose, so adding a setting later never needs another migration.

Existing installs already past ``001`` will not pick up a new ORM table from
``001``'s ``create_all``, so this revision creates it explicitly. Fresh
databases already have it from ``create_all`` of the current metadata, so the
create is skipped in that case — the same defensive shape as
``002_processed_events``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "004_instance_settings"
down_revision: str | None = "003_group_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "instance_settings"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in inspector.get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in inspector.get_table_names():
        op.drop_table(_TABLE)
