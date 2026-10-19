"""Add instance_id on instance_keys for the public-key fingerprint cache.

The column stores this instance's federation id (SHA-256 hex of the raw
Ed25519 public key). App code derives the value from ``public_key``; this
revision only adds the column.

Revision ID: 010_instance_key_instance_id
Revises: 009_encrypt_oauth_tokens
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010_instance_key_instance_id"
down_revision: str | None = "009_encrypt_oauth_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "instance_keys"
_COLUMN = "instance_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
