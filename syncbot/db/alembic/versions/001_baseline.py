"""Baseline schema (all app tables + Slack OAuth tables). Supports MySQL and SQLite.

Revision ID: 001_baseline
Revises:
Create Date: Baseline from ORM models + OAuth tables

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from db.schemas import BaseClass

revision: str = "001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    BaseClass.metadata.create_all(bind)

    # Slack SDK OAuth tables (not in our ORM; dialect-neutral schema)
    op.create_table(
        "slack_bots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("client_id", sa.String(32), nullable=False),
        sa.Column("app_id", sa.String(32), nullable=False),
        sa.Column("enterprise_id", sa.String(32), nullable=True),
        sa.Column("enterprise_name", sa.String(200), nullable=True),
        sa.Column("team_id", sa.String(32), nullable=True),
        sa.Column("team_name", sa.String(200), nullable=True),
        sa.Column("bot_token", sa.Text(), nullable=True),
        sa.Column("bot_id", sa.String(32), nullable=True),
        sa.Column("bot_user_id", sa.String(32), nullable=True),
        sa.Column("bot_scopes", sa.String(1000), nullable=True),
        sa.Column("bot_refresh_token", sa.Text(), nullable=True),
        sa.Column("bot_token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("is_enterprise_install", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("installed_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "slack_installations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("client_id", sa.String(32), nullable=False),
        sa.Column("app_id", sa.String(32), nullable=False),
        sa.Column("enterprise_id", sa.String(32), nullable=True),
        sa.Column("enterprise_name", sa.String(200), nullable=True),
        sa.Column("enterprise_url", sa.String(200), nullable=True),
        sa.Column("team_id", sa.String(32), nullable=True),
        sa.Column("team_name", sa.String(200), nullable=True),
        sa.Column("bot_token", sa.Text(), nullable=True),
        sa.Column("bot_id", sa.String(32), nullable=True),
        sa.Column("bot_user_id", sa.String(32), nullable=True),
        sa.Column("bot_scopes", sa.String(1000), nullable=True),
        sa.Column("bot_refresh_token", sa.Text(), nullable=True),
        sa.Column("bot_token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("user_token", sa.Text(), nullable=True),
        sa.Column("user_scopes", sa.String(1000), nullable=True),
        sa.Column("user_refresh_token", sa.Text(), nullable=True),
        sa.Column("user_token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("incoming_webhook_url", sa.String(200), nullable=True),
        sa.Column("incoming_webhook_channel", sa.String(200), nullable=True),
        sa.Column("incoming_webhook_channel_id", sa.String(200), nullable=True),
        sa.Column("incoming_webhook_configuration_url", sa.String(200), nullable=True),
        sa.Column("is_enterprise_install", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("token_type", sa.String(32), nullable=True),
        sa.Column("installed_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "slack_oauth_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("state", sa.String(200), nullable=False),
        sa.Column("expire_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("slack_oauth_states")
    op.drop_table("slack_installations")
    op.drop_table("slack_bots")
    bind = op.get_bind()
    BaseClass.metadata.drop_all(bind)
