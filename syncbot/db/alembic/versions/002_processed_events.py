"""Add processed_events for Slack event_id idempotency.

Revision ID: 002_processed_events
Revises: 001_baseline
Create Date: 2026-08-27

Existing installs already at 001 will not pick up a new ORM table from
``001``'s ``create_all``. This revision uses ``op.create_table``. Fresh
databases may already have the table from ``001`` ``create_all`` of current
metadata; skip create in that case.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_processed_events"
down_revision: str | None = "001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "processed_events" in inspector.get_table_names():
        return
    op.create_table(
        "processed_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("team_id", sa.String(length=100), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "event_id", name="uq_processed_events_team_event"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "processed_events" in inspector.get_table_names():
        op.drop_table("processed_events")
