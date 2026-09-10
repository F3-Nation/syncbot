"""Add Text columns for encrypted Slack OAuth tokens.

Revision ID: 009_encrypt_oauth_tokens
Revises: 008_post_meta_reaction_notices
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009_encrypt_oauth_tokens"
down_revision: str | None = "008_post_meta_reaction_notices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOKEN_COLUMNS: dict[str, tuple[str, ...]] = {
    "slack_installations": ("bot_token", "bot_refresh_token", "user_token", "user_refresh_token"),
    "slack_bots": ("bot_token", "bot_refresh_token"),
    "workspaces": ("bot_token",),
}

_SLACK_PREFIXES = ("xoxb-", "xoxp-", "xoxe-", "xoxa-")


def _column_type_name(col: dict) -> str:
    col_type = col["type"]
    return col_type.__class__.__name__.lower()


def _widen_to_text(table: str, column: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"]: col for col in inspector.get_columns(table)}
    if column not in columns:
        return
    if _column_type_name(columns[column]) == "text":
        return
    col = columns[column]
    with op.batch_alter_table(table) as batch:
        batch.alter_column(
            column,
            type_=sa.Text(),
            existing_type=col["type"],
            existing_nullable=col["nullable"],
        )


def _encrypt_plaintext_tokens(table: str, column: str) -> None:
    from helpers.encryption import encrypt_bot_token, encryption_active_for_migration

    if not encryption_active_for_migration():
        return

    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")).fetchall()
    for row_id, value in rows:
        if not value or not isinstance(value, str):
            continue
        if value.startswith("gAAAAA"):
            continue
        if not value.startswith(_SLACK_PREFIXES):
            continue
        encrypted = encrypt_bot_token(value)
        bind.execute(
            sa.text(f"UPDATE {table} SET {column} = :enc WHERE id = :id"),
            {"enc": encrypted, "id": row_id},
        )


def upgrade() -> None:
    for table, columns in _TOKEN_COLUMNS.items():
        bind = op.get_bind()
        inspector = sa.inspect(bind)
        if table not in inspector.get_table_names():
            continue
        for column in columns:
            _widen_to_text(table, column)
            _encrypt_plaintext_tokens(table, column)


def downgrade() -> None:
    pass
