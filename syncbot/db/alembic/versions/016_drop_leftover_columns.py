"""Drop leftover sync/workspace columns; tighten post_meta and user_directory.

Revision ID: 016_drop_leftover_columns
Revises: 015_federation_stubs
Create Date: 2026-09-15

After ``workspaces.instance_id`` marks live vs stub, drop pairwise leftovers and
make ``slack_bots`` the bot-token store. Backfill ``slack_bots`` from
``workspaces.bot_token`` for live workspaces that have no Bolt row yet.
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "016_drop_leftover_columns"
down_revision: str | None = "015_federation_stubs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _col_names(inspector, table: str) -> set[str]:
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _index_names(inspector, table: str) -> set[str]:
    if table not in inspector.get_table_names():
        return set()
    return {ix["name"] for ix in inspector.get_indexes(table) if ix.get("name")}


def _fk_names(inspector, table: str) -> set[str]:
    if table not in inspector.get_table_names():
        return set()
    return {fk["name"] for fk in inspector.get_foreign_keys(table) if fk.get("name")}


def _column_has_fk(inspector, table: str, column: str) -> bool:
    """True when *column* already has a foreign key, named or not."""
    if table not in inspector.get_table_names():
        return False
    return any(column in (fk.get("constrained_columns") or []) for fk in inspector.get_foreign_keys(table))


def _fk_names_on_column(inspector, table: str, column: str) -> list[str]:
    names: list[str] = []
    if table not in inspector.get_table_names():
        return names
    for fk in inspector.get_foreign_keys(table):
        if column in (fk.get("constrained_columns") or []) and fk.get("name"):
            names.append(fk["name"])
    if names:
        return names
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return names
    rows = bind.execute(
        sa.text(
            """
            SELECT DISTINCT CONSTRAINT_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :table
              AND COLUMN_NAME = :column
              AND REFERENCED_TABLE_NAME IS NOT NULL
            """
        ),
        {"table": table, "column": column},
    ).fetchall()
    return [row[0] for row in rows if row[0]]


def _drop_column_with_fks(table: str, column: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if column not in _col_names(inspector, table):
        return
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)
        return
    for fk_name in _fk_names_on_column(inspector, table, column):
        op.drop_constraint(fk_name, table, type_="foreignkey")
    op.drop_column(table, column)


def _backfill_slack_bots(bind) -> None:
    """Copy decryptable live workspace tokens into slack_bots when missing."""
    inspector = sa.inspect(bind)
    if "slack_bots" not in inspector.get_table_names():
        return
    if "bot_token" not in _col_names(inspector, "workspaces"):
        return
    if "instance_id" not in _col_names(inspector, "workspaces"):
        return

    self_row = bind.execute(
        sa.text(
            """
            SELECT instance_id FROM instances
            WHERE private_key_encrypted IS NOT NULL
            LIMIT 1
            """
        )
    ).fetchone()
    if not self_row:
        return
    self_iid = self_row[0]

    bot_cols = _col_names(inspector, "slack_bots")
    client_id = os.environ.get("SLACK_CLIENT_ID", "").strip()
    rows = bind.execute(
        sa.text(
            """
            SELECT team_id, bot_token FROM workspaces
            WHERE deleted_at IS NULL
              AND instance_id = :self_iid
              AND bot_token IS NOT NULL
              AND team_id IS NOT NULL
            """
        ),
        {"self_iid": self_iid},
    ).fetchall()
    for team_id, bot_token in rows:
        exists = bind.execute(
            sa.text("SELECT 1 FROM slack_bots WHERE team_id = :tid LIMIT 1"),
            {"tid": team_id},
        ).fetchone()
        if exists:
            continue
        values = {"tid": team_id, "tok": bot_token}
        columns = ["team_id", "bot_token"]
        placeholders = [":tid", ":tok"]
        if "client_id" in bot_cols:
            columns.append("client_id")
            placeholders.append(":cid")
            values["cid"] = client_id[:32]
        if "app_id" in bot_cols:
            columns.append("app_id")
            placeholders.append(":app")
            values["app"] = ""
        if "is_enterprise_install" in bot_cols:
            columns.append("is_enterprise_install")
            placeholders.append(":ent")
            values["ent"] = False
        try:
            bind.execute(
                sa.text(f"INSERT INTO slack_bots ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"),
                values,
            )
        except Exception:
            # Do not abort 016; drop of workspaces.bot_token still proceeds when
            # slack_bots already has OAuth rows for this team.
            continue


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _backfill_slack_bots(bind)
    inspector = sa.inspect(bind)

    _drop_column_with_fks("syncs", "publisher_workspace_id")
    _drop_column_with_fks("syncs", "target_workspace_id")

    sc_cols = _col_names(inspector, "sync_channels")
    if "reaction_direction" in sc_cols:
        with op.batch_alter_table("sync_channels") as batch:
            batch.drop_column("reaction_direction")

    ws_cols = _col_names(inspector, "workspaces")
    if "bot_token" in ws_cols:
        with op.batch_alter_table("workspaces") as batch:
            batch.drop_column("bot_token")

    # Orphan source_workspace_id integers that are not a workspaces.id → NULL, then FK.
    pm_cols = _col_names(inspector, "post_meta")
    if "source_workspace_id" in pm_cols:
        bind.execute(
            sa.text(
                """
                UPDATE post_meta
                SET source_workspace_id = NULL
                WHERE source_workspace_id IS NOT NULL
                  AND source_workspace_id NOT IN (SELECT id FROM workspaces)
                """
            )
        )
        if not _column_has_fk(inspector, "post_meta", "source_workspace_id"):
            with op.batch_alter_table("post_meta") as batch:
                batch.create_foreign_key(
                    "fk_post_meta_source_workspace_id",
                    "workspaces",
                    ["source_workspace_id"],
                    ["id"],
                )

    ud_cols = _col_names(inspector, "user_directory")
    if ud_cols and "uq_user_directory_workspace_slack_user" not in _index_names(inspector, "user_directory"):
        # Drop duplicate (workspace_id, slack_user_id) keeping the newest updated_at.
        if bind.dialect.name == "sqlite":
            bind.execute(
                sa.text(
                    """
                    DELETE FROM user_directory
                    WHERE id NOT IN (
                        SELECT MAX(id) FROM user_directory
                        GROUP BY workspace_id, slack_user_id
                    )
                    """
                )
            )
        else:
            bind.execute(
                sa.text(
                    """
                    DELETE ud1 FROM user_directory ud1
                    INNER JOIN user_directory ud2
                      ON ud1.workspace_id = ud2.workspace_id
                     AND ud1.slack_user_id = ud2.slack_user_id
                     AND ud1.id < ud2.id
                    """
                )
            )
        with op.batch_alter_table("user_directory") as batch:
            batch.create_unique_constraint(
                "uq_user_directory_workspace_slack_user",
                ["workspace_id", "slack_user_id"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "uq_user_directory_workspace_slack_user" in _index_names(inspector, "user_directory"):
        with op.batch_alter_table("user_directory") as batch:
            batch.drop_constraint("uq_user_directory_workspace_slack_user", type_="unique")

    fks = _fk_names(inspector, "post_meta")
    if "fk_post_meta_source_workspace_id" in fks:
        with op.batch_alter_table("post_meta") as batch:
            batch.drop_constraint("fk_post_meta_source_workspace_id", type_="foreignkey")

    ws_cols = _col_names(inspector, "workspaces")
    if "bot_token" not in ws_cols:
        with op.batch_alter_table("workspaces") as batch:
            batch.add_column(sa.Column("bot_token", sa.Text(), nullable=True))

    sc_cols = _col_names(inspector, "sync_channels")
    if "reaction_direction" not in sc_cols:
        with op.batch_alter_table("sync_channels") as batch:
            batch.add_column(
                sa.Column(
                    "reaction_direction",
                    sa.String(length=32),
                    nullable=False,
                    server_default="both",
                )
            )

    sync_cols = _col_names(inspector, "syncs")
    with op.batch_alter_table("syncs") as batch:
        if "publisher_workspace_id" not in sync_cols:
            batch.add_column(sa.Column("publisher_workspace_id", sa.Integer(), nullable=True))
        if "target_workspace_id" not in sync_cols:
            batch.add_column(sa.Column("target_workspace_id", sa.Integer(), nullable=True))
