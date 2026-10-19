"""Add workspace_settings for per-workspace policy.

Revision ID: 005_workspace_settings
Revises: 004_instance_settings
Create Date: 2026-09-01

Copies the legacy instance-wide ``allow_private_channels`` value onto every
existing workspace so upgrades do not silently revert to public-only.

Raw ``WHERE key = …`` is a syntax error on MySQL/TiDB (``KEY`` is reserved).
Identifier quoting is forced on the ``key`` column so the copy statements
compile correctly on every dialect this project ships.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import column, func, insert, select, table
from sqlalchemy.sql import quoted_name

revision: str = "005_workspace_settings"
down_revision: str | None = "004_instance_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workspace_settings"
_KEY_ALLOW_PRIVATE = "allow_private_channels"


def _key_column():
    """``KEY`` is reserved in MySQL/TiDB; always quote this identifier."""
    return column(quoted_name("key", True))


def _instance_settings():
    return table("instance_settings", _key_column(), column("value"))


def _workspace_settings():
    return table(
        "workspace_settings",
        column("workspace_id"),
        _key_column(),
        column("value"),
        column("updated_at"),
    )


def _workspaces():
    return table("workspaces", column("id"), column("deleted_at"))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        op.create_table(
            _TABLE,
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("value", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("workspace_id", "key"),
        )

    inst = _instance_settings()
    ws_settings = _workspace_settings()
    ws = _workspaces()

    legacy = bind.execute(select(inst.c.value).where(inst.c.key == _KEY_ALLOW_PRIVATE)).fetchone()
    if not legacy or legacy[0] is None:
        return
    legacy_val = str(legacy[0]).strip()
    if legacy_val.lower() not in ("true", "false", "1", "0", "yes", "no"):
        return

    workspaces = bind.execute(select(ws.c.id).where(ws.c.deleted_at.is_(None))).fetchall()
    for (workspace_id,) in workspaces:
        exists = bind.execute(
            select(ws_settings.c.workspace_id)
            .where(
                ws_settings.c.workspace_id == workspace_id,
                ws_settings.c.key == _KEY_ALLOW_PRIVATE,
            )
            .limit(1)
        ).fetchone()
        if exists:
            continue
        bind.execute(
            insert(ws_settings).values(
                workspace_id=workspace_id,
                key=_KEY_ALLOW_PRIVATE,
                value=legacy_val.lower(),
                updated_at=func.current_timestamp(),
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in inspector.get_table_names():
        op.drop_table(_TABLE)
