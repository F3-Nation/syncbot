"""Store Create External Connection allowlist on the pairing code.

Revision ID: 018_pairing_allowlist
Revises: 017_file_part_total
Create Date: 2026-09-16

The peer instance does not exist until Join, so the creator's allowed
workspace ids live on the pairing row and are applied in handle_pair.

Also recreates ``federation_pairing_requests.pairing_code_id`` with
``ON DELETE SET NULL`` when 015 created it without that rule (TiDB ``fk_1``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "018_pairing_allowlist"
down_revision: str | None = "017_file_part_total"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PAIRING_REQUEST_CODE_FK = "fk_federation_pairing_requests_pairing_code_id"


def _pairing_code_fk_rules(bind, inspector) -> list[tuple[str | None, str]]:
    table = "federation_pairing_requests"
    if table not in inspector.get_table_names():
        return []
    if bind.dialect.name == "mysql":
        rows = bind.execute(
            sa.text(
                """
                SELECT rc.CONSTRAINT_NAME, rc.DELETE_RULE
                FROM information_schema.REFERENTIAL_CONSTRAINTS rc
                INNER JOIN information_schema.KEY_COLUMN_USAGE kcu
                  ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
                 AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                 AND rc.TABLE_NAME = kcu.TABLE_NAME
                WHERE rc.CONSTRAINT_SCHEMA = DATABASE()
                  AND kcu.TABLE_NAME = 'federation_pairing_requests'
                  AND kcu.COLUMN_NAME = 'pairing_code_id'
                  AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
                """
            )
        ).fetchall()
        return [(row[0], (row[1] or "").upper()) for row in rows if row[0]]
    rules: list[tuple[str | None, str]] = []
    for fk in inspector.get_foreign_keys(table):
        if "pairing_code_id" not in (fk.get("constrained_columns") or []):
            continue
        rule = ((fk.get("options") or {}).get("ondelete") or "").upper()
        rules.append((fk.get("name"), rule))
    return rules


def _ensure_pairing_request_code_ondelete_set_null() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "federation_pairing_requests" not in tables or "federation_pairing_codes" not in tables:
        return
    rules = _pairing_code_fk_rules(bind, inspector)
    if rules and all(rule.replace("_", " ") == "SET NULL" for _, rule in rules):
        return
    names = [name for name, _ in rules if name]
    if not names and bind.dialect.name == "mysql":
        rows = bind.execute(
            sa.text(
                """
                SELECT DISTINCT CONSTRAINT_NAME
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'federation_pairing_requests'
                  AND COLUMN_NAME = 'pairing_code_id'
                  AND REFERENCED_TABLE_NAME IS NOT NULL
                """
            )
        ).fetchall()
        names = [row[0] for row in rows if row[0]]
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("federation_pairing_requests") as batch:
            for name in names:
                batch.drop_constraint(name, type_="foreignkey")
            batch.create_foreign_key(
                _PAIRING_REQUEST_CODE_FK,
                "federation_pairing_codes",
                ["pairing_code_id"],
                ["id"],
                ondelete="SET NULL",
            )
        return
    for name in names:
        op.drop_constraint(name, "federation_pairing_requests", type_="foreignkey")
    op.create_foreign_key(
        _PAIRING_REQUEST_CODE_FK,
        "federation_pairing_requests",
        "federation_pairing_codes",
        ["pairing_code_id"],
        ["id"],
        ondelete="SET NULL",
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "federation_pairing_codes" in tables:
        cols = {c["name"] for c in inspector.get_columns("federation_pairing_codes")}
        if "allowed_workspace_ids" not in cols:
            op.add_column(
                "federation_pairing_codes",
                sa.Column("allowed_workspace_ids", sa.Text(), nullable=True),
            )
    _ensure_pairing_request_code_ondelete_set_null()


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "federation_pairing_codes" not in tables:
        return
    cols = {c["name"] for c in inspector.get_columns("federation_pairing_codes")}
    if "allowed_workspace_ids" in cols:
        op.drop_column("federation_pairing_codes", "allowed_workspace_ids")
