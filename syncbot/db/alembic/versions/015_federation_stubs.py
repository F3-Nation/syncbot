"""Instances fingerprint PK, workspace.instance_id, uid, pairing/pending stubs.

Revision ID: 015_federation_stubs
Revises: 014_posted_as_pub_sub
Create Date: 2026-09-15

Replaces federated_workspaces + instance_keys with instances (PK = instance_id).
Live vs stub is workspaces.instance_id vs this install's fingerprint.

NOTE: SQL in this file deliberately uses the *pre-015* column name
``federated_workspace_id`` when reading 014-era tables. Do not rename those
string literals to ``instance_id``.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

revision: str = "015_federation_stubs"
down_revision: str | None = "014_posted_as_pub_sub"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_names(inspector) -> set[str]:
    return set(inspector.get_table_names())


def _col_names(inspector, table: str) -> set[str]:
    if table not in _table_names(inspector):
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def _fk_names(inspector, table: str) -> set[str]:
    if table not in _table_names(inspector):
        return set()
    return {fk["name"] for fk in inspector.get_foreign_keys(table) if fk.get("name")}


def _column_has_fk(inspector, table: str, column: str) -> bool:
    """True when *column* already has a foreign key, named or not.

    ``001_baseline`` ``create_all`` emits unnamed SQLite FKs. Adding a named
    copy of the same constraint leaves two ``FOREIGN KEY`` clauses in
    ``sqlite_master`` and SQLAlchemy warns that the parsed DDL does not match
    ``PRAGMA foreign_key_list``.
    """
    if table not in _table_names(inspector):
        return False
    return any(column in (fk.get("constrained_columns") or []) for fk in inspector.get_foreign_keys(table))


def _fk_names_on_column(inspector, table: str, column: str) -> list[str]:
    """FK names that constrain *column*, including TiDB auto-names such as ``fk_3``."""
    names: list[str] = []
    if table not in _table_names(inspector):
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


def _column_nullable(inspector, table: str, column: str) -> bool | None:
    if table not in _table_names(inspector):
        return None
    for col in inspector.get_columns(table):
        if col["name"] == column:
            return bool(col.get("nullable"))
    return None


def _set_not_null(table: str, column: str, existing_type) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _column_nullable(inspector, table, column) is not True:
        return
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=existing_type, nullable=False)
        return
    op.alter_column(
        table,
        column,
        existing_type=existing_type,
        existing_nullable=True,
        nullable=False,
    )


def _drop_column_with_fks(table: str, column: str) -> None:
    """Drop *column* after dropping every FK that still references it."""
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


_PAIRING_REQUEST_CODE_FK = "fk_federation_pairing_requests_pairing_code_id"


def _pairing_code_fk_rules(bind, inspector) -> list[tuple[str | None, str]]:
    """Return ``(constraint_name, DELETE_RULE)`` for ``pairing_code_id``."""
    table = "federation_pairing_requests"
    if table not in _table_names(inspector):
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
    """Cancel / consume must delete a pairing code while a request still points at it."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _table_names(inspector)
    if "federation_pairing_requests" not in tables or "federation_pairing_codes" not in tables:
        return
    rules = _pairing_code_fk_rules(bind, inspector)
    if rules and all(rule.replace("_", " ") == "SET NULL" for _, rule in rules):
        return
    names = [name for name, _ in rules if name]
    if not names and bind.dialect.name == "mysql":
        names = _fk_names_on_column(inspector, "federation_pairing_requests", "pairing_code_id")
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


def _index_names(inspector, table: str) -> set[str]:
    if table not in _table_names(inspector):
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table) if idx.get("name")}


def _public_key_fingerprint(public_key_pem: str) -> str:
    import hashlib

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    public_key = load_pem_public_key(public_key_pem.encode())
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("not_ed25519_public_key")
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


def _encrypt_private_pem(private_pem: str) -> str:
    try:
        from helpers.encryption import encrypt_bot_token

        return encrypt_bot_token(private_pem) or private_pem
    except Exception:
        return private_pem


def _ensure_self_instance(bind) -> str:
    existing_self = bind.execute(
        sa.text(
            """
            SELECT instance_id FROM instances
            WHERE private_key_encrypted IS NOT NULL
            LIMIT 1
            """
        )
    ).fetchone()
    if existing_self:
        return existing_self[0]

    now = datetime.now(UTC).replace(tzinfo=None)
    inspector = sa.inspect(bind)
    if "instance_keys" in _table_names(inspector):
        row = bind.execute(
            sa.text(
                """
                SELECT public_key, private_key_encrypted, instance_id, created_at
                FROM instance_keys ORDER BY id LIMIT 1
                """
            )
        ).fetchone()
        if row:
            public_key, private_enc, cached_id, created_at = row
            instance_id = (cached_id or "").strip() or _public_key_fingerprint(public_key)
            bind.execute(
                sa.text(
                    """
                    INSERT INTO instances (
                        instance_id, public_key, private_key_encrypted, webhook_url,
                        status, trust_status, name, primary_team_id, primary_workspace_name,
                        created_at, updated_at
                    ) VALUES (
                        :iid, :pub, :priv, NULL, 'active', 'trusted', NULL, NULL, NULL,
                        :created, NULL
                    )
                    """
                ),
                {
                    "iid": instance_id,
                    "pub": public_key,
                    "priv": private_enc,
                    "created": created_at or now,
                },
            )
            return instance_id

    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    private_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    instance_id = _public_key_fingerprint(public_pem)
    bind.execute(
        sa.text(
            """
            INSERT INTO instances (
                instance_id, public_key, private_key_encrypted, webhook_url,
                status, trust_status, name, primary_team_id, primary_workspace_name,
                created_at, updated_at
            ) VALUES (
                :iid, :pub, :priv, NULL, 'active', 'trusted', NULL, NULL, NULL,
                :created, NULL
            )
            """
        ),
        {
            "iid": instance_id,
            "pub": public_pem,
            "priv": _encrypt_private_pem(private_pem),
            "created": now,
        },
    )
    return instance_id


def _migrate_peers_into_instances(bind) -> dict[int, str]:
    inspector = sa.inspect(bind)
    if "federated_workspaces" not in _table_names(inspector):
        return {}

    cols = _col_names(inspector, "federated_workspaces")
    has_trust = "trust_status" in cols
    trust_expr = "trust_status" if has_trust else "'trusted'"
    rows = bind.execute(
        sa.text(
            f"""
            SELECT id, instance_id, webhook_url, public_key, status,
                   {trust_expr},
                   name, primary_team_id, primary_workspace_name, created_at, updated_at
            FROM federated_workspaces
            """
        )
    ).fetchall()

    mapping: dict[int, str] = {}
    now = datetime.now(UTC).replace(tzinfo=None)
    for row in rows:
        (
            old_id,
            peer_fingerprint,
            webhook_url,
            public_key,
            status,
            trust_status,
            name,
            primary_team_id,
            primary_workspace_name,
            created_at,
            updated_at,
        ) = row
        iid = (peer_fingerprint or "").strip()
        if not iid:
            try:
                iid = _public_key_fingerprint(public_key)
            except Exception:
                iid = secrets.token_hex(32)
        exists = bind.execute(
            sa.text("SELECT 1 FROM instances WHERE instance_id = :iid"),
            {"iid": iid},
        ).fetchone()
        if not exists:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO instances (
                        instance_id, public_key, private_key_encrypted, webhook_url,
                        status, trust_status, name, primary_team_id, primary_workspace_name,
                        created_at, updated_at
                    ) VALUES (
                        :iid, :pub, NULL, :url, :status, :trust, :name, :pt, :pn,
                        :created, :updated
                    )
                    """
                ),
                {
                    "iid": iid,
                    "pub": public_key,
                    "url": webhook_url,
                    "status": status or "active",
                    "trust": trust_status or "trusted",
                    "name": name,
                    "pt": primary_team_id,
                    "pn": primary_workspace_name,
                    "created": created_at or now,
                    "updated": updated_at,
                },
            )
        mapping[int(old_id)] = iid
    return mapping


def _convert_fed_members_to_stubs(bind, fed_id_to_instance: dict[int, str]) -> None:
    """Convert 014-era member.federated_workspace_id rows into stub workspaces."""
    members = bind.execute(
        sa.text(
            """
            SELECT id, group_id, federated_workspace_id, status, role, joined_at
            FROM workspace_group_members
            WHERE federated_workspace_id IS NOT NULL
              AND deleted_at IS NULL
            """
        )
    ).fetchall()

    inspector = sa.inspect(bind)
    ws_cols = _col_names(inspector, "workspaces")
    token_select = ", bot_token" if "bot_token" in ws_cols else ", NULL AS bot_token"

    for row in members:
        member_id, _group_id, fed_id, _status, _role, _joined_at = row
        peer_iid = fed_id_to_instance.get(int(fed_id))
        if not peer_iid:
            bind.execute(
                sa.text("DELETE FROM workspace_group_members WHERE id = :id"),
                {"id": member_id},
            )
            continue

        fed = bind.execute(
            sa.text(
                """
                SELECT primary_team_id, primary_workspace_name, name
                FROM instances WHERE instance_id = :iid
                """
            ),
            {"iid": peer_iid},
        ).fetchone()
        if not fed:
            bind.execute(
                sa.text("DELETE FROM workspace_group_members WHERE id = :id"),
                {"id": member_id},
            )
            continue

        primary_team_id, primary_name, fed_name = fed
        team_id = (primary_team_id or "").strip()
        if not team_id:
            bind.execute(
                sa.text("DELETE FROM workspace_group_members WHERE id = :id"),
                {"id": member_id},
            )
            continue

        existing = bind.execute(
            sa.text(f"SELECT id{token_select} FROM workspaces WHERE team_id = :team_id"),
            {"team_id": team_id},
        ).fetchone()

        display = (primary_name or fed_name or team_id)[:100]
        if existing:
            ws_id, bot_token = existing
            if bot_token:
                bind.execute(
                    sa.text(
                        """
                        UPDATE workspace_group_members
                        SET workspace_id = :ws_id, federated_workspace_id = NULL
                        WHERE id = :id
                        """
                    ),
                    {"ws_id": ws_id, "id": member_id},
                )
            else:
                updates = [
                    "instance_id = :iid",
                    "workspace_name = COALESCE(workspace_name, :name)",
                    "deleted_at = NULL",
                ]
                if "bot_token" in ws_cols:
                    updates.append("bot_token = NULL")
                bind.execute(
                    sa.text(f"UPDATE workspaces SET {', '.join(updates)} WHERE id = :ws_id"),
                    {"iid": peer_iid, "name": display, "ws_id": ws_id},
                )
                bind.execute(
                    sa.text(
                        """
                        UPDATE workspace_group_members
                        SET workspace_id = :ws_id, federated_workspace_id = NULL
                        WHERE id = :id
                        """
                    ),
                    {"ws_id": ws_id, "id": member_id},
                )
        else:
            insert_cols = ["team_id", "workspace_name", "instance_id", "deleted_at"]
            insert_vals = [":team_id", ":name", ":iid", "NULL"]
            if "bot_token" in ws_cols:
                insert_cols.append("bot_token")
                insert_vals.append("NULL")
            result = bind.execute(
                sa.text(f"INSERT INTO workspaces ({', '.join(insert_cols)}) VALUES ({', '.join(insert_vals)})"),
                {"team_id": team_id, "name": display, "iid": peer_iid},
            )
            ws_id = result.lastrowid
            if ws_id is None:
                ws_id = bind.execute(
                    sa.text("SELECT id FROM workspaces WHERE team_id = :team_id"),
                    {"team_id": team_id},
                ).scalar()
            bind.execute(
                sa.text(
                    """
                    UPDATE workspace_group_members
                    SET workspace_id = :ws_id, federated_workspace_id = NULL
                    WHERE id = :id
                    """
                ),
                {"ws_id": ws_id, "id": member_id},
            )

    bind.execute(
        sa.text(
            """
            DELETE FROM workspace_group_members
            WHERE federated_workspace_id IS NOT NULL AND workspace_id IS NULL
            """
        )
    )

    empty_groups = bind.execute(
        sa.text(
            """
            SELECT g.id FROM workspace_groups g
            WHERE g.name LIKE 'Federation — %'
              AND NOT EXISTS (
                SELECT 1 FROM workspace_group_members m
                WHERE m.group_id = g.id AND m.deleted_at IS NULL
              )
              AND NOT EXISTS (
                SELECT 1 FROM syncs s WHERE s.group_id = g.id
              )
            """
        )
    ).fetchall()
    for (gid,) in empty_groups:
        bind.execute(sa.text("DELETE FROM workspace_groups WHERE id = :id"), {"id": gid})


def _backfill_uid(bind, table: str) -> None:
    inspector = sa.inspect(bind)
    cols = _col_names(inspector, table)

    if "uid" not in cols:
        if "federation_uid" in cols:
            op.add_column(table, sa.Column("uid", sa.String(length=36), nullable=True))
            bind.execute(sa.text(f"UPDATE {table} SET uid = federation_uid WHERE federation_uid IS NOT NULL"))
        else:
            op.add_column(table, sa.Column("uid", sa.String(length=36), nullable=True))

    rows = bind.execute(sa.text(f"SELECT id FROM {table} WHERE uid IS NULL OR uid = ''")).fetchall()
    for (row_id,) in rows:
        bind.execute(
            sa.text(f"UPDATE {table} SET uid = :uid WHERE id = :id"),
            {"uid": str(uuid.uuid4()), "id": row_id},
        )

    inspector = sa.inspect(bind)
    cols = _col_names(inspector, table)
    if "federation_uid" in cols:
        idx_name = f"ix_{table}_federation_uid"
        if idx_name in _index_names(inspector, table):
            op.drop_index(idx_name, table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_column("federation_uid")

    inspector = sa.inspect(bind)
    uid_idx = f"ix_{table}_uid"
    if uid_idx not in _index_names(inspector, table):
        op.create_index(uid_idx, table, ["uid"], unique=True)

    _set_not_null(table, "uid", sa.String(length=36))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _table_names(inspector)

    if "instances" not in tables:
        op.create_table(
            "instances",
            sa.Column("instance_id", sa.String(length=64), primary_key=True),
            sa.Column("public_key", sa.Text(), nullable=False),
            sa.Column("private_key_encrypted", sa.Text(), nullable=True),
            sa.Column("webhook_url", sa.String(length=500), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
            sa.Column("trust_status", sa.String(length=20), nullable=False, server_default="trusted"),
            sa.Column("name", sa.String(length=200), nullable=True),
            sa.Column("primary_team_id", sa.String(length=100), nullable=True),
            sa.Column("primary_workspace_name", sa.String(length=100), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )

    self_iid = _ensure_self_instance(bind)
    fed_id_to_instance = _migrate_peers_into_instances(bind)

    inspector = sa.inspect(bind)
    ws_cols = _col_names(inspector, "workspaces")
    had_fed_fk = "federated_workspace_id" in ws_cols
    if "instance_id" not in ws_cols:
        op.add_column("workspaces", sa.Column("instance_id", sa.String(length=64), nullable=True))

    # Backfill stubs from old int FK, then everything else to self
    if had_fed_fk:
        for old_id, peer_iid in fed_id_to_instance.items():
            bind.execute(
                sa.text(
                    """
                    UPDATE workspaces SET instance_id = :iid
                    WHERE federated_workspace_id = :old_id
                      AND (instance_id IS NULL OR instance_id = '')
                    """
                ),
                {"iid": peer_iid, "old_id": old_id},
            )
    bind.execute(
        sa.text("UPDATE workspaces SET instance_id = :iid WHERE instance_id IS NULL OR instance_id = ''"),
        {"iid": self_iid},
    )

    inspector = sa.inspect(bind)
    member_cols = _col_names(inspector, "workspace_group_members")
    if "federated_workspace_id" in member_cols:
        _convert_fed_members_to_stubs(bind, fed_id_to_instance)
        _drop_column_with_fks("workspace_group_members", "federated_workspace_id")

    inspector = sa.inspect(bind)
    ws_cols = _col_names(inspector, "workspaces")
    if "federated_workspace_id" in ws_cols:
        _drop_column_with_fks("workspaces", "federated_workspace_id")

    inspector = sa.inspect(bind)
    if not _column_has_fk(inspector, "workspaces", "instance_id"):
        with op.batch_alter_table("workspaces") as batch:
            batch.create_foreign_key(
                "fk_workspaces_instance_id",
                "instances",
                ["instance_id"],
                ["instance_id"],
            )

    _set_not_null("workspaces", "instance_id", sa.String(length=64))

    _backfill_uid(bind, "workspace_groups")
    _backfill_uid(bind, "syncs")

    inspector = sa.inspect(bind)
    tables = _table_names(inspector)
    if "federation_pairing_codes" not in tables:
        op.create_table(
            "federation_pairing_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(length=20), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("subject_team_id", sa.String(length=32), nullable=True),
            sa.Column("label", sa.String(length=200), nullable=True),
            sa.Column("allowed_workspace_ids", sa.Text(), nullable=True),
            sa.UniqueConstraint("code"),
        )
    else:
        pc_cols = _col_names(inspector, "federation_pairing_codes")
        if "subject_team_id" not in pc_cols:
            op.add_column(
                "federation_pairing_codes",
                sa.Column("subject_team_id", sa.String(length=32), nullable=True),
            )
        if "created_by_workspace_id" in pc_cols:
            bind.execute(
                sa.text(
                    """
                    UPDATE federation_pairing_codes
                    SET subject_team_id = (
                        SELECT team_id FROM workspaces
                        WHERE workspaces.id = federation_pairing_codes.created_by_workspace_id
                    )
                    WHERE created_by_workspace_id IS NOT NULL AND subject_team_id IS NULL
                    """
                )
            )
            _drop_column_with_fks("federation_pairing_codes", "created_by_workspace_id")
        if "allowed_workspace_ids" not in pc_cols:
            op.add_column(
                "federation_pairing_codes",
                sa.Column("allowed_workspace_ids", sa.Text(), nullable=True),
            )

    inspector = sa.inspect(bind)
    if "federation_pairing_requests" not in _table_names(inspector):
        op.create_table(
            "federation_pairing_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("subject_team_id", sa.String(length=32), nullable=False),
            sa.Column("requested_by_user_id", sa.String(length=100), nullable=False),
            sa.Column("requested_at", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("resolved_by_user_id", sa.String(length=100), nullable=True),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.Column("pairing_code_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(
                ["pairing_code_id"],
                ["federation_pairing_codes.id"],
                name="fk_federation_pairing_requests_pairing_code_id",
                ondelete="SET NULL",
            ),
        )
    _ensure_pairing_request_code_ondelete_set_null()

    inspector = sa.inspect(bind)
    if "federation_pending_stubs" not in _table_names(inspector):
        op.create_table(
            "federation_pending_stubs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("instance_id", sa.String(length=64), nullable=False),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.ForeignKeyConstraint(["instance_id"], ["instances.instance_id"]),
            sa.UniqueConstraint("workspace_id", "instance_id", name="uq_federation_pending_stubs_ws_peer"),
        )

    inspector = sa.inspect(bind)
    tables = _table_names(inspector)
    if "federation_workspace_allowlist" in tables:
        al_cols = _col_names(inspector, "federation_workspace_allowlist")
        if "federated_workspace_id" in al_cols:
            if "instance_id" not in al_cols:
                op.add_column(
                    "federation_workspace_allowlist",
                    sa.Column("instance_id", sa.String(length=64), nullable=True),
                )
            for old_id, peer_iid in fed_id_to_instance.items():
                bind.execute(
                    sa.text(
                        """
                        UPDATE federation_workspace_allowlist
                        SET instance_id = :iid
                        WHERE federated_workspace_id = :old_id
                        """
                    ),
                    {"iid": peer_iid, "old_id": old_id},
                )
            bind.execute(sa.text("DELETE FROM federation_workspace_allowlist WHERE instance_id IS NULL"))
            _drop_column_with_fks("federation_workspace_allowlist", "federated_workspace_id")
            inspector = sa.inspect(bind)
            if "fk_federation_allowlist_instance_id" not in _fk_names(inspector, "federation_workspace_allowlist"):
                with op.batch_alter_table("federation_workspace_allowlist") as batch:
                    batch.create_foreign_key(
                        "fk_federation_allowlist_instance_id",
                        "instances",
                        ["instance_id"],
                        ["instance_id"],
                    )
            inspector = sa.inspect(bind)
            uq_names = {c["name"] for c in inspector.get_unique_constraints("federation_workspace_allowlist")}
            if "uq_federation_workspace_allowlist_peer_ws" not in uq_names:
                with op.batch_alter_table("federation_workspace_allowlist") as batch:
                    batch.create_unique_constraint(
                        "uq_federation_workspace_allowlist_peer_ws",
                        ["instance_id", "workspace_id"],
                    )
    else:
        op.create_table(
            "federation_workspace_allowlist",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("instance_id", sa.String(length=64), nullable=False),
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["instance_id"], ["instances.instance_id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.UniqueConstraint(
                "instance_id",
                "workspace_id",
                name="uq_federation_workspace_allowlist_peer_ws",
            ),
        )

    inspector = sa.inspect(bind)
    if "federation_file_parts" not in _table_names(inspector):
        op.create_table(
            "federation_file_parts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("part_index", sa.Integer(), nullable=False),
            sa.Column("payload", sa.LargeBinary(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("sha256", "part_index", name="uq_federation_file_parts_sha_idx"),
        )

    inspector = sa.inspect(bind)
    tables = _table_names(inspector)
    if "federated_workspaces" in tables:
        op.drop_table("federated_workspaces")
    if "instance_keys" in tables:
        op.drop_table("instance_keys")


def downgrade() -> None:
    """Best-effort reverse toward 014 + WIP federated_workspaces shape."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _table_names(inspector)
    now = datetime.now(UTC).replace(tzinfo=None)

    if "instance_keys" not in tables:
        op.create_table(
            "instance_keys",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("public_key", sa.Text(), nullable=False),
            sa.Column("private_key_encrypted", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("instance_id", sa.String(length=64), nullable=True),
        )
        self_row = bind.execute(
            sa.text(
                """
                SELECT instance_id, public_key, private_key_encrypted, created_at
                FROM instances WHERE private_key_encrypted IS NOT NULL LIMIT 1
                """
            )
        ).fetchone()
        if self_row:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO instance_keys (public_key, private_key_encrypted, created_at, instance_id)
                    VALUES (:pub, :priv, :created, :iid)
                    """
                ),
                {
                    "pub": self_row[1],
                    "priv": self_row[2],
                    "created": self_row[3] or now,
                    "iid": self_row[0],
                },
            )

    if "federated_workspaces" not in tables:
        op.create_table(
            "federated_workspaces",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("instance_id", sa.String(length=64), nullable=False, unique=True),
            sa.Column("webhook_url", sa.String(length=500), nullable=False),
            sa.Column("public_key", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("trust_status", sa.String(length=20), nullable=False, server_default="trusted"),
            sa.Column("name", sa.String(length=200), nullable=True),
            sa.Column("primary_team_id", sa.String(length=100), nullable=True),
            sa.Column("primary_workspace_name", sa.String(length=100), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        peers = bind.execute(
            sa.text(
                """
                SELECT instance_id, webhook_url, public_key, status, trust_status, name,
                       primary_team_id, primary_workspace_name, created_at, updated_at
                FROM instances WHERE private_key_encrypted IS NULL AND webhook_url IS NOT NULL
                """
            )
        ).fetchall()
        for peer in peers:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO federated_workspaces (
                        instance_id, webhook_url, public_key, status, trust_status, name,
                        primary_team_id, primary_workspace_name, created_at, updated_at
                    ) VALUES (
                        :iid, :url, :pub, :status, :trust, :name, :pt, :pn, :created, :updated
                    )
                    """
                ),
                {
                    "iid": peer[0],
                    "url": peer[1],
                    "pub": peer[2],
                    "status": peer[3],
                    "trust": peer[4],
                    "name": peer[5],
                    "pt": peer[6],
                    "pn": peer[7],
                    "created": peer[8] or now,
                    "updated": peer[9],
                },
            )

    for tbl in (
        "federation_pending_stubs",
        "federation_pairing_requests",
        "federation_file_parts",
        "federation_workspace_allowlist",
        "federation_pairing_codes",
    ):
        inspector = sa.inspect(bind)
        if tbl in _table_names(inspector):
            op.drop_table(tbl)

    for table, idx in (("syncs", "ix_syncs_uid"), ("workspace_groups", "ix_workspace_groups_uid")):
        inspector = sa.inspect(bind)
        cols = _col_names(inspector, table)
        if "uid" in cols:
            if idx in _index_names(inspector, table):
                op.drop_index(idx, table_name=table)
            with op.batch_alter_table(table) as batch:
                batch.drop_column("uid")

    inspector = sa.inspect(bind)
    member_cols = _col_names(inspector, "workspace_group_members")
    if "federated_workspace_id" not in member_cols:
        with op.batch_alter_table("workspace_group_members") as batch:
            batch.add_column(sa.Column("federated_workspace_id", sa.Integer(), nullable=True))

    inspector = sa.inspect(bind)
    ws_cols = _col_names(inspector, "workspaces")
    if "instance_id" in ws_cols:
        with op.batch_alter_table("workspaces") as batch:
            if "fk_workspaces_instance_id" in _fk_names(inspector, "workspaces"):
                batch.drop_constraint("fk_workspaces_instance_id", type_="foreignkey")
            batch.drop_column("instance_id")
        with op.batch_alter_table("workspaces") as batch:
            batch.add_column(sa.Column("federated_workspace_id", sa.Integer(), nullable=True))

    inspector = sa.inspect(bind)
    if "instances" in _table_names(inspector):
        op.drop_table("instances")
