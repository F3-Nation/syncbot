"""Store Slack Channel display names on sync_channels for stub Home and notices.

Revision ID: 019_sync_channel_name
Revises: 018_pairing_allowlist
Create Date: 2026-09-16

Federated stubs have no bot token, so conversations.info cannot resolve
``#name``. Replicate and migration carry the name the live side already knows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "019_sync_channel_name"
down_revision: str | None = "018_pairing_allowlist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "sync_channels" not in tables:
        return
    cols = {c["name"] for c in inspector.get_columns("sync_channels")}
    if "channel_name" not in cols:
        op.add_column(
            "sync_channels",
            sa.Column("channel_name", sa.String(length=100), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "sync_channels" not in tables:
        return
    cols = {c["name"] for c in inspector.get_columns("sync_channels")}
    if "channel_name" in cols:
        op.drop_column("sync_channels", "channel_name")
