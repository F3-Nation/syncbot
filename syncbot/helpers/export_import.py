"""Backup/restore and data migration export/import helpers.

Full-instance backup: dump all tables as JSON with HMAC for tampering detection.
Data migration: workspace-scoped export with Ed25519 signature; import with replace mode.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import MetaData, Table, delete, select

import constants
from db import DbManager, get_engine, schemas
from helpers.workspace import get_workspace_by_id

_logger = logging.getLogger(__name__)

BACKUP_VERSION = 1
MIGRATION_VERSION = 1
_RAW_BACKUP_TABLES = ("slack_bots", "slack_installations", "slack_oauth_states")
_DATETIME_COLUMNS = frozenset(
    {
        "bot_token_expires_at",
        "user_token_expires_at",
        "installed_at",
        "expire_at",
    }
)


def _dump_raw_table(table_name: str) -> list[dict]:
    """Return all rows from a non-ORM table as dictionaries (dialect-neutral via reflection)."""
    engine = get_engine()
    meta = MetaData()
    table = Table(table_name, meta, autoload_with=engine)
    with engine.connect() as conn:
        rows = conn.execute(select(table)).mappings().all()
    return [dict(row) for row in rows]


def _restore_raw_table(table_name: str, rows: list[dict]) -> None:
    """Replace table contents for a non-ORM table from backup rows (dialect-neutral)."""
    engine = get_engine()
    meta = MetaData()
    table = Table(table_name, meta, autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(delete(table))
        for row in rows:
            if not row:
                continue
            parsed: dict[str, Any] = {}
            for key, value in row.items():
                if key not in table.c:
                    continue
                if isinstance(value, str) and key in _DATETIME_COLUMNS:
                    try:
                        parsed[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    except ValueError:
                        parsed[key] = value
                else:
                    parsed[key] = value
            if parsed:
                conn.execute(table.insert().values(**parsed))


def _json_serializer(obj: Any) -> Any:
    """Convert datetime and Decimal for JSON."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def canonical_json_dumps(obj: dict) -> bytes:
    """Serialize to canonical JSON (sort_keys, no extra whitespace) for signing/HMAC."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_serializer,
    ).encode("utf-8")


def _compute_encryption_key_hash() -> str | None:
    """SHA-256 hex of DATA_ENCRYPTION_KEY, or None if unset."""
    key = os.environ.get(constants.DATA_ENCRYPTION_KEY) or os.environ.get(constants._DATA_ENCRYPTION_KEY_LEGACY, "")
    if not key or key == "123":
        return None
    return hashlib.sha256(key.encode()).hexdigest()


def _compute_backup_hmac(payload_without_hmac: dict) -> str:
    """HMAC-SHA256 of canonical JSON of payload (excluding hmac field), keyed by DATA_ENCRYPTION_KEY."""
    key = os.environ.get(constants.DATA_ENCRYPTION_KEY) or os.environ.get(constants._DATA_ENCRYPTION_KEY_LEGACY, "")
    if not key:
        key = ""
    raw = canonical_json_dumps(payload_without_hmac)
    return hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()


def _records_to_list(records: list, cls: type) -> list[dict]:
    """Convert ORM records to list of dicts with serializable values."""
    out = []
    for r in records:
        d = {}
        for k in cls._get_column_keys():
            v = getattr(r, k)
            if isinstance(v, datetime):
                v = v.isoformat()
            elif isinstance(v, Decimal):
                v = float(v)
            d[k] = v
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Full-instance backup
# ---------------------------------------------------------------------------


def build_full_backup() -> dict:
    """Build full-instance backup payload (all tables, version, exported_at, encryption_key_hash, hmac)."""
    payload = {
        "version": BACKUP_VERSION,
        "exported_at": datetime.now(UTC).isoformat() + "Z",
        "encryption_key_hash": _compute_encryption_key_hash(),
    }
    # processed_events and user_action_echoes are ephemeral — omit from backup.
    tables = [
        ("workspaces", schemas.Workspace),
        ("workspace_groups", schemas.WorkspaceGroup),
        ("workspace_group_members", schemas.WorkspaceGroupMember),
        ("syncs", schemas.Sync),
        ("sync_channels", schemas.SyncChannel),
        ("post_meta", schemas.PostMeta),
        ("user_directory", schemas.UserDirectory),
        ("user_mappings", schemas.UserMapping),
        ("federated_workspaces", schemas.FederatedWorkspace),
        ("instance_keys", schemas.InstanceKey),
        ("workspace_settings", schemas.WorkspaceSetting),
    ]
    for table_name, cls in tables:
        records = DbManager.find_records(cls, [])
        payload[table_name] = _records_to_list(records, cls)
    for table_name in _RAW_BACKUP_TABLES:
        payload[table_name] = _dump_raw_table(table_name)

    payload["hmac"] = _compute_backup_hmac({k: v for k, v in payload.items() if k != "hmac"})
    return payload


def verify_backup_hmac(data: dict) -> bool:
    """Return True if HMAC in data matches recomputed HMAC (excluding hmac field)."""
    stored = data.get("hmac")
    if not stored:
        return False
    payload_without_hmac = {k: v for k, v in data.items() if k != "hmac"}
    expected = _compute_backup_hmac(payload_without_hmac)
    return hmac.compare_digest(stored, expected)  # noqa: S324


def verify_backup_encryption_key(data: dict) -> bool:
    """Return True if current encryption key hash matches backup's."""
    current = _compute_encryption_key_hash()
    backup_hash = data.get("encryption_key_hash")
    if backup_hash is None and current is None:
        return True
    if backup_hash is None or current is None:
        return False
    return hmac.compare_digest(current, backup_hash)  # noqa: S324


def restore_full_backup(
    data: dict,
    *,
    skip_hmac_check: bool = False,
    skip_encryption_key_check: bool = False,
) -> list[str]:
    """Restore full backup into DB. Inserts in FK order. Returns list of team_ids for cache invalidation.

    Caller must have validated version and structure. Does not truncate tables; assumes empty or
    intentional overwrite (e.g. restore after rebuild).
    """
    team_ids: list[str] = []
    tables = [
        "slack_bots",
        "slack_installations",
        "slack_oauth_states",
        "workspaces",
        "workspace_groups",
        "workspace_group_members",
        "syncs",
        "sync_channels",
        "post_meta",
        "user_directory",
        "user_mappings",
        "federated_workspaces",
        "instance_keys",
        "workspace_settings",
    ]
    table_to_schema = {
        "workspaces": schemas.Workspace,
        "workspace_groups": schemas.WorkspaceGroup,
        "workspace_group_members": schemas.WorkspaceGroupMember,
        "syncs": schemas.Sync,
        "sync_channels": schemas.SyncChannel,
        "post_meta": schemas.PostMeta,
        "user_directory": schemas.UserDirectory,
        "user_mappings": schemas.UserMapping,
        "federated_workspaces": schemas.FederatedWorkspace,
        "instance_keys": schemas.InstanceKey,
        "workspace_settings": schemas.WorkspaceSetting,
    }
    datetime_keys = {"created_at", "updated_at", "deleted_at", "joined_at", "mapped_at", "matched_at"}
    for table_name in tables:
        rows = data.get(table_name, [])
        if table_name in _RAW_BACKUP_TABLES:
            _restore_raw_table(table_name, rows)
            continue
        cls = table_to_schema[table_name]
        # Backups taken before a column was dropped still carry it; passing an
        # unknown kwarg to the model would raise. Skip anything the current
        # schema no longer has (e.g. workspace_groups.created_by_workspace_id).
        known_columns = {col.name for col in cls.__table__.columns}
        for row in rows:
            # Remap old backup keys before known_columns skips unknown names.
            if table_name == "user_mappings" and "mapped_at" not in row and "matched_at" in row:
                row = {**row, "mapped_at": row["matched_at"]}
            if table_name == "workspace_settings" and row.get("key") == "last_auto_match":
                row = {**row, "key": "last_auto_map"}
            kwargs = {}
            for k, v in row.items():
                if k not in known_columns:
                    continue
                if v is None:
                    kwargs[k] = None
                elif isinstance(v, str) and k in datetime_keys:
                    try:
                        kwargs[k] = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    except ValueError:
                        kwargs[k] = v
                elif k == "ts" and v is not None:
                    kwargs[k] = Decimal(str(v))
                else:
                    kwargs[k] = v
            # Legacy backups use the pre-003 role name. Without this the Home tab
            # owner label silently disappears and the workspace loses owner rights.
            if table_name == "workspace_group_members" and kwargs.get("role") == "creator":
                kwargs["role"] = "owner"
            rec = cls(**kwargs)
            DbManager.merge_record(rec)
            if table_name == "workspaces" and rec.team_id:
                team_ids.append(rec.team_id)
    return team_ids


# ---------------------------------------------------------------------------
# Cache invalidation after restore/import
# ---------------------------------------------------------------------------


def invalidate_home_tab_caches_for_team(team_id: str) -> None:
    """Clear home_tab_hash and home_tab_blocks for a team so next Refresh does full rebuild."""
    from helpers._cache import _cache_delete_prefix

    _cache_delete_prefix(f"home_tab_hash:{team_id}")
    _cache_delete_prefix(f"home_tab_blocks:{team_id}")


def invalidate_home_tab_caches_for_all_teams(team_ids: list[str]) -> None:
    """Clear home tab caches for each team_id (e.g. after full restore)."""
    for tid in team_ids:
        invalidate_home_tab_caches_for_team(tid)


def invalidate_channel_memberships_cache(channel_id: str) -> None:
    """Clear membership caches for a channel."""
    from helpers.sync_participation import invalidate_channel_memberships

    invalidate_channel_memberships(channel_id)


# ---------------------------------------------------------------------------
# Data migration export (workspace-scoped)
# ---------------------------------------------------------------------------


def build_migration_export(workspace_id: int, include_source_instance: bool = True) -> dict:
    """Build workspace-scoped migration JSON. Optionally sign with Ed25519 and include source_instance."""
    workspace = get_workspace_by_id(workspace_id)
    if not workspace or workspace.deleted_at:
        raise ValueError("Workspace not found")

    team_id = workspace.team_id
    workspace_name = workspace.workspace_name or ""

    # Groups W is in
    memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id == workspace_id,
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
            schemas.WorkspaceGroupMember.status == "active",
        ],
    )
    groups_data = []
    for membership in memberships:
        g = DbManager.get_record(schemas.WorkspaceGroup, membership.group_id)
        if g:
            role = "owner" if membership.role == "creator" else membership.role
            groups_data.append({"name": g.name, "role": role})

    # Syncs that have at least one SyncChannel for W
    sync_channels_w = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.workspace_id == workspace_id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    sync_ids = {sync_channel.sync_id for sync_channel in sync_channels_w}
    syncs_data = []
    sync_channels_data = []
    post_meta_by_key = {}

    for sync_id in sync_ids:
        sync = DbManager.get_record(schemas.Sync, sync_id)
        if not sync:
            continue
        pub_team = None
        tgt_team = None
        if sync.publisher_workspace_id:
            publisher_ws = get_workspace_by_id(sync.publisher_workspace_id)
            if publisher_ws:
                pub_team = publisher_ws.team_id
        if sync.target_workspace_id:
            tw = get_workspace_by_id(sync.target_workspace_id)
            if tw:
                tgt_team = tw.team_id
        syncs_data.append(
            {
                "title": sync.title,
                "sync_mode": sync.sync_mode or "group",
                "publisher_team_id": pub_team,
                "target_team_id": tgt_team,
                "is_publisher": sync.publisher_workspace_id == workspace_id,
            }
        )
        for sync_channel in sync_channels_w:
            if sync_channel.sync_id != sync_id:
                continue
            sync_channels_data.append(
                {
                    "sync_title": sync.title,
                    "channel_id": sync_channel.channel_id,
                    "status": sync_channel.status or "active",
                    "publishes": sync_channel.publishes,
                    "subscribes": sync_channel.subscribes,
                    "reaction_style": sync_channel.reaction_style,
                    "reaction_direction": sync_channel.reaction_direction,
                }
            )
            key = f"{sync.title}:{sync_channel.channel_id}"
            post_metas = DbManager.find_records(
                schemas.PostMeta,
                [schemas.PostMeta.sync_channel_id == sync_channel.id],
            )
            post_meta_by_key[key] = [
                {
                    "post_id": post_meta.post_id,
                    "ts": float(post_meta.ts),
                    "kind": getattr(post_meta, "kind", constants.POST_META_KIND_MESSAGE)
                    or constants.POST_META_KIND_MESSAGE,
                    "parent_post_id": getattr(post_meta, "parent_post_id", None),
                    "reaction": getattr(post_meta, "reaction", None),
                    "source_user_id": getattr(post_meta, "source_user_id", None),
                    "source_workspace_id": getattr(post_meta, "source_workspace_id", None),
                    "posted_as_user_id": getattr(post_meta, "posted_as_user_id", None),
                }
                for post_meta in post_metas
            ]

    # user_directory for W
    ud_records = DbManager.find_records(
        schemas.UserDirectory,
        [
            schemas.UserDirectory.workspace_id == workspace_id,
            schemas.UserDirectory.deleted_at.is_(None),
        ],
    )
    user_directory_data = []
    for u in ud_records:
        user_directory_data.append(
            {
                "slack_user_id": u.slack_user_id,
                "email": u.email,
                "real_name": u.real_name,
                "display_name": u.display_name,
                "normalized_name": u.normalized_name,
                "updated_at": u.updated_at.isoformat() if u.updated_at else None,
            }
        )

    # user_mappings involving W (export with team_id for other side)
    um_records = DbManager.find_records(
        schemas.UserMapping,
        [
            (schemas.UserMapping.source_workspace_id == workspace_id)
            | (schemas.UserMapping.target_workspace_id == workspace_id),
        ],
    )
    user_mappings_data = []
    for um in um_records:
        src_ws = get_workspace_by_id(um.source_workspace_id) if um.source_workspace_id else None
        tgt_ws = get_workspace_by_id(um.target_workspace_id) if um.target_workspace_id else None
        user_mappings_data.append(
            {
                "source_team_id": src_ws.team_id if src_ws else None,
                "target_team_id": tgt_ws.team_id if tgt_ws else None,
                "source_user_id": um.source_user_id,
                "target_user_id": um.target_user_id,
                "map_method": um.map_method,
            }
        )

    payload = {
        "version": MIGRATION_VERSION,
        "exported_at": datetime.now(UTC).isoformat() + "Z",
        "workspace": {"team_id": team_id, "workspace_name": workspace_name},
        "groups": groups_data,
        "syncs": syncs_data,
        "sync_channels": sync_channels_data,
        "post_meta": post_meta_by_key,
        "user_directory": user_directory_data,
        "user_mappings": user_mappings_data,
    }

    if include_source_instance:
        from federation import core as federation

        try:
            endpoint = federation.federation_endpoint_url()
            instance_id = federation.get_instance_id()
            _, public_key_pem = federation.get_or_create_instance_keypair()
            if endpoint:
                code = federation.encode_federation_connection_blob(
                    endpoint, instance_id, public_key_pem, "FED-" + secrets.token_hex(4).upper()
                )
                payload["source_instance"] = {
                    "webhook_url": endpoint,
                    "instance_id": instance_id,
                    "public_key": public_key_pem,
                    "connection_code": code,
                }
        except Exception as e:
            _logger.warning("build_migration_export: could not add source_instance: %s", e)

    # Sign with Ed25519 (exclude signature from signed bytes; include signed_at)
    try:
        from federation import core as federation

        payload["signed_at"] = datetime.now(UTC).isoformat() + "Z"
        to_sign = {k: v for k, v in payload.items() if k != "signature"}
        raw = canonical_json_dumps(to_sign).decode("utf-8")
        payload["signature"] = federation.sign_body(raw)
    except Exception as e:
        _logger.warning("build_migration_export: could not sign: %s", e)

    return payload


def verify_migration_signature(data: dict) -> bool:
    """Verify Ed25519 signature using source_instance.public_key. Returns False if no signature or invalid."""
    sig = data.get("signature")
    source = data.get("source_instance")
    if not sig or not source:
        return False
    public_key = source.get("public_key")
    if not public_key:
        return False
    to_verify = {k: v for k, v in data.items() if k != "signature"}
    raw = canonical_json_dumps(to_verify).decode("utf-8")
    from federation import core as federation

    return federation.verify_body(raw, sig, public_key)


def import_migration_data(
    data: dict,
    workspace_id: int,
    group_id: int,
    *,
    team_id_to_workspace_id: dict[str, int],
) -> None:
    """Import migration payload into DB (replace mode). Caller must have resolved federated group and team_id_to_workspace_id on B.

    - Replace mode: soft-delete W's SyncChannels in this group and their PostMeta, then create from export.
    - team_id_to_workspace_id: map export team_id -> B's workspace id (for publisher/target and user_mappings).
    """
    syncs_export = data.get("syncs", [])
    sync_channels_export = data.get("sync_channels", [])
    post_meta_export = data.get("post_meta", {})
    user_directory_export = data.get("user_directory", [])
    user_mappings_export = data.get("user_mappings", [])
    workspace_export = data.get("workspace", {})
    export_team_id = workspace_export.get("team_id")

    # Replace mode: find syncs in group, then SyncChannels for this workspace in those syncs
    syncs_in_group = DbManager.find_records(schemas.Sync, [schemas.Sync.group_id == group_id])
    sync_ids_in_group = [s.id for s in syncs_in_group]
    if sync_ids_in_group:
        channels_to_remove = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id.in_(sync_ids_in_group),
                schemas.SyncChannel.workspace_id == workspace_id,
                schemas.SyncChannel.deleted_at.is_(None),
            ],
        )
        now = datetime.now(UTC)
        for sync_channel in channels_to_remove:
            DbManager.delete_records(
                schemas.PostMeta,
                [schemas.PostMeta.sync_channel_id == sync_channel.id],
            )
            DbManager.update_records(
                schemas.SyncChannel,
                [schemas.SyncChannel.id == sync_channel.id],
                {schemas.SyncChannel.deleted_at: now},
            )

    # Build sync title -> sync_id (B) for this group (create or reuse)
    title_to_sync = {}
    for s in syncs_export:
        title = s.get("title")
        if not title:
            continue
        existing = DbManager.find_records(
            schemas.Sync,
            [schemas.Sync.group_id == group_id, schemas.Sync.title == title],
        )
        if existing:
            title_to_sync[title] = existing[0].id
        else:
            pub_team = s.get("publisher_team_id")
            tgt_team = s.get("target_team_id")
            is_publisher = s.get("is_publisher")
            pub_ws_id = (workspace_id if is_publisher else team_id_to_workspace_id.get(pub_team)) if pub_team else None
            tgt_ws_id = (
                (workspace_id if tgt_team == export_team_id else team_id_to_workspace_id.get(tgt_team))
                if tgt_team
                else None
            )
            new_sync = schemas.Sync(
                title=title,
                group_id=group_id,
                sync_mode=s.get("sync_mode", "group"),
                publisher_workspace_id=pub_ws_id,
                target_workspace_id=tgt_ws_id,
            )
            DbManager.create_record(new_sync)
            title_to_sync[title] = new_sync.id

    # Create SyncChannels and PostMeta
    for sc_entry in sync_channels_export:
        sync_title = sc_entry.get("sync_title")
        channel_id = sc_entry.get("channel_id")
        status = sc_entry.get("status", "active")
        sync_id = title_to_sync.get(sync_title)
        if not sync_id:
            continue
        new_sync_channel = schemas.SyncChannel(
            sync_id=sync_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            status=status,
            publishes=sc_entry.get("publishes", True),
            subscribes=sc_entry.get("subscribes", True),
            reaction_style=sc_entry.get("reaction_style"),
            reaction_direction=sc_entry.get("reaction_direction") or constants.DEFAULT_REACTION_DIRECTION,
            created_at=datetime.now(UTC),
        )
        DbManager.create_record(new_sync_channel)
        key = f"{sync_title}:{channel_id}"
        for post_meta in post_meta_export.get(key, []):
            DbManager.create_record(
                schemas.PostMeta(
                    post_id=post_meta["post_id"],
                    sync_channel_id=new_sync_channel.id,
                    ts=Decimal(str(post_meta["ts"])),
                    kind=post_meta.get("kind") or constants.POST_META_KIND_MESSAGE,
                    parent_post_id=post_meta.get("parent_post_id"),
                    reaction=post_meta.get("reaction"),
                    source_user_id=post_meta.get("source_user_id"),
                    source_workspace_id=post_meta.get("source_workspace_id"),
                    posted_as_user_id=post_meta.get("posted_as_user_id"),
                )
            )

    # user_directory for W (replace: remove existing for this workspace then insert)
    DbManager.delete_records(
        schemas.UserDirectory,
        [schemas.UserDirectory.workspace_id == workspace_id],
    )
    for u in user_directory_export:
        DbManager.create_record(
            schemas.UserDirectory(
                workspace_id=workspace_id,
                slack_user_id=u["slack_user_id"],
                email=u.get("email"),
                real_name=u.get("real_name"),
                display_name=u.get("display_name"),
                normalized_name=u.get("normalized_name"),
                updated_at=datetime.fromisoformat(u["updated_at"].replace("Z", "+00:00"))
                if u.get("updated_at")
                else datetime.now(UTC),
            )
        )

    # user_mappings where both source and target workspace exist on B
    for um in user_mappings_export:
        src_team = um.get("source_team_id")
        tgt_team = um.get("target_team_id")
        src_ws_id = team_id_to_workspace_id.get(src_team) if src_team else None
        tgt_ws_id = team_id_to_workspace_id.get(tgt_team) if tgt_team else None
        if not src_ws_id or not tgt_ws_id:
            continue
        existing = DbManager.find_records(
            schemas.UserMapping,
            [
                schemas.UserMapping.source_workspace_id == src_ws_id,
                schemas.UserMapping.source_user_id == um["source_user_id"],
                schemas.UserMapping.target_workspace_id == tgt_ws_id,
            ],
        )
        if existing:
            continue
        DbManager.create_record(
            schemas.UserMapping(
                source_workspace_id=src_ws_id,
                source_user_id=um["source_user_id"],
                target_workspace_id=tgt_ws_id,
                target_user_id=um.get("target_user_id"),
                map_method=um.get("map_method") or um.get("match_method", "none"),
                mapped_at=datetime.now(UTC),
                group_id=group_id,
            )
        )
