"""Rename user_mappings.match_method to map_method.

Revision ID: 011_user_mapping_map_method
Revises: 010_instance_key_instance_id
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "011_user_mapping_map_method"
down_revision: str | None = "010_instance_key_instance_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "user_mappings"
_OLD = "match_method"
_NEW = "map_method"


def _rename_column(old: str, new: str) -> None:
    """Rename *old* to *new* when present. TiDB/MySQL CHANGE needs the type."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {col["name"]: col for col in inspector.get_columns(_TABLE)}
    if new in columns or old not in columns:
        return
    col = columns[old]
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            old,
            new_column_name=new,
            existing_type=col["type"],
            existing_nullable=col["nullable"],
        )


def upgrade() -> None:
    _rename_column(_OLD, _NEW)


def downgrade() -> None:
    _rename_column(_NEW, _OLD)
