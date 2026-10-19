"""Add part_total on federation_file_parts so offer_have is not fooled by a prefix.

Revision ID: 017_file_part_total
Revises: 016_drop_leftover_columns
Create Date: 2026-09-15

Ciphertext length is not plaintext size, so completeness must use the
sender's part count. Inferring total from max(part_index)+1 treats part 0 of
N as a complete one-part file.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "017_file_part_total"
down_revision: str | None = "016_drop_leftover_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "federation_file_parts" not in tables:
        return
    cols = {c["name"] for c in inspector.get_columns("federation_file_parts")}
    if "part_total" not in cols:
        op.add_column(
            "federation_file_parts",
            sa.Column("part_total", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "federation_file_parts" not in tables:
        return
    cols = {c["name"] for c in inspector.get_columns("federation_file_parts")}
    if "part_total" in cols:
        op.drop_column("federation_file_parts", "part_total")
