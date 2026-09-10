"""Map group role 'creator' to 'owner' and drop workspace_groups.created_by_workspace_id.

Revision ID: 003_group_roles
Revises: 002_processed_events
Create Date: 2026-08-30

``created_by_workspace_id`` was vestigial: every write site also created a
``workspace_group_members`` row with ``role='creator'`` for the same workspace,
and the only reader was a redundant self-join guard. Because the column is
``NOT NULL`` it blocked the retention purge from ever deleting a workspace that
had created a group.

The role rename makes ``role`` load-bearing (owners are the workspaces that may
promote, leave under a guard, and disband) using Slack's own vocabulary.

Also backfills the ownership invariant: every active group ends with at least
one active owner. Groups whose creator already left are repaired by promoting
the earliest-joined active local member.

Defensive throughout: ``001_baseline`` builds fresh databases from the current
models via ``metadata.create_all``, so a fresh database never has the dropped
column and every step here must detect that and no-op.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003_group_roles"
down_revision: str | None = "002_processed_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workspace_groups"
_COLUMN = "created_by_workspace_id"


def _has_column(inspector, table: str, column: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return any(col["name"] == column for col in inspector.get_columns(table))


def _workspace_fk_names(inspector) -> list[str]:
    """Find FKs on workspace_groups.created_by_workspace_id.

    ``create_all`` auto-generates the constraint name, so it must be located by
    referred table and constrained columns rather than by name.
    """
    names = []
    for fk in inspector.get_foreign_keys(_TABLE):
        if (
            fk.get("referred_table") == "workspaces"
            and _COLUMN in (fk.get("constrained_columns") or [])
            and fk.get("name")
        ):
            names.append(fk["name"])
    return names


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "workspace_group_members" in inspector.get_table_names():
        op.execute(sa.text("UPDATE workspace_group_members SET role = 'owner' WHERE role = 'creator'"))
        _backfill_group_owners(bind)

    if not _has_column(inspector, _TABLE, _COLUMN):
        return

    if bind.dialect.name == "sqlite":
        # SQLite cannot drop a column carrying a REFERENCES clause without a
        # table rebuild, and that rebuild issues DROP TABLE workspace_groups,
        # which workspace_group_members references. Defer FK checks to the end
        # of the transaction, by which point the table and its rows are back.
        # Unlike `foreign_keys`, `defer_foreign_keys` is honored inside a
        # transaction, which is where migrations run.
        op.execute(sa.text("PRAGMA defer_foreign_keys=ON"))
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.drop_column(_COLUMN)
        return

    for fk_name in _workspace_fk_names(inspector):
        op.drop_constraint(fk_name, _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, _COLUMN)


def _backfill_group_owners(bind) -> None:
    """Ensure every active group has at least one active owner.

    A group whose creator left has an inactive ``owner`` row, because leaving
    soft-deletes the membership. Promote the earliest-joined active local member
    so the ownership invariant holds before the application starts enforcing it.
    """
    groups = bind.execute(
        sa.text(
            """
            SELECT g.id
            FROM workspace_groups g
            WHERE g.status = 'active'
              AND NOT EXISTS (
                SELECT 1 FROM workspace_group_members m
                WHERE m.group_id = g.id
                  AND m.role = 'owner'
                  AND m.status = 'active'
                  AND m.deleted_at IS NULL
              )
            """
        )
    ).fetchall()

    for (group_id,) in groups:
        candidate = bind.execute(
            sa.text(
                """
                SELECT id FROM workspace_group_members
                WHERE group_id = :group_id
                  AND status = 'active'
                  AND deleted_at IS NULL
                  AND workspace_id IS NOT NULL
                ORDER BY joined_at IS NULL, joined_at ASC, id ASC
                LIMIT 1
                """
            ),
            {"group_id": group_id},
        ).fetchone()

        # No local member to promote (federated-only group). The succession
        # ladder handles that at runtime; there is nothing to do here.
        if candidate is None:
            continue

        bind.execute(
            sa.text("UPDATE workspace_group_members SET role = 'owner' WHERE id = :member_id"),
            {"member_id": candidate[0]},
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, _TABLE, _COLUMN):
        # Re-added as nullable: the original values are gone, so NOT NULL cannot
        # be restored without inventing data.
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))

    if "workspace_group_members" in inspector.get_table_names():
        op.execute(sa.text("UPDATE workspace_group_members SET role = 'creator' WHERE role = 'owner'"))
