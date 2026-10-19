"""Backup/restore and data migration export/import helpers.

Full-instance backup: dump durable tables as JSON with HMAC for tampering detection.
Ephemeral tables (processed_events, user_action_echoes, federation_file_parts) are omitted.
Data migration: workspace-scoped export with Ed25519 signature; import with replace mode.
"""

import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import MetaData, Table, delete, select

import constants
from db import DbManager, get_engine, schemas
from helpers.user_action_echo import post_meta_ts, slack_message_ts
from helpers.workspace import get_workspace_by_id
from logger import log_info, log_warning

# Dump-format integers. Bump when the JSON shape is incompatible with this
# restore/import path. ``syncbot_version`` is the running package label only.
BACKUP_VERSION = 1
MIGRATION_VERSION = 1
_RAW_BACKUP_TABLES = ("slack_bots", "slack_installations", "slack_oauth_states")
# In-flight / consume-once tables. Never dump or restore, even if a dump includes them.
_EPHEMERAL_BACKUP_TABLES = frozenset(
    {
        "processed_events",
        "user_action_echoes",
        "federation_file_parts",
    }
)
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
                v = slack_message_ts(v) if k == "ts" else float(v)
            d[k] = v
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Full-instance backup
# ---------------------------------------------------------------------------


def build_full_backup() -> dict:
    """Build full-instance backup payload (durable tables, version, exported_at, encryption_key_hash, hmac)."""
    payload = {
        "version": BACKUP_VERSION,
        "syncbot_version": constants.app_version(),
        "exported_at": datetime.now(UTC).isoformat() + "Z",
        "encryption_key_hash": _compute_encryption_key_hash(),
    }
    # Durable tables only. Never add names from _EPHEMERAL_BACKUP_TABLES.
    tables = [
        ("instances", schemas.Instance),
        ("workspaces", schemas.Workspace),
        ("workspace_groups", schemas.WorkspaceGroup),
        ("workspace_group_members", schemas.WorkspaceGroupMember),
        ("syncs", schemas.Sync),
        ("sync_channels", schemas.SyncChannel),
        ("post_meta", schemas.PostMeta),
        ("user_directory", schemas.UserDirectory),
        ("user_mappings", schemas.UserMapping),
        ("federation_pairing_codes", schemas.FederationPairingCode),
        ("federation_workspace_allowlist", schemas.FederationWorkspaceAllowlist),
        ("workspace_settings", schemas.WorkspaceSetting),
        ("instance_settings", schemas.InstanceSetting),
    ]
    for table_name, cls in tables:
        if table_name in _EPHEMERAL_BACKUP_TABLES:
            continue
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
        "instances",
        "workspaces",
        "workspace_groups",
        "workspace_group_members",
        "syncs",
        "sync_channels",
        "post_meta",
        "user_directory",
        "user_mappings",
        "federation_pairing_codes",
        "federation_workspace_allowlist",
        "workspace_settings",
        "instance_settings",
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
        "instances": schemas.Instance,
        "federation_pairing_codes": schemas.FederationPairingCode,
        "federation_workspace_allowlist": schemas.FederationWorkspaceAllowlist,
        "workspace_settings": schemas.WorkspaceSetting,
        "instance_settings": schemas.InstanceSetting,
    }
    datetime_keys = {"created_at", "updated_at", "deleted_at", "joined_at", "mapped_at", "matched_at"}
    for table_name in tables:
        if table_name in _EPHEMERAL_BACKUP_TABLES:
            continue
        rows = data.get(table_name, [])
        # Pre-015 backups split this table into federated_workspaces (peers) and
        # instance_keys (self keypair); merge them into instances.
        if table_name == "instances" and not rows:
            rows = list(data.get("federated_workspaces", []) or []) + list(data.get("instance_keys", []) or [])
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
                    kwargs[k] = post_meta_ts(v)
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


# ---------------------------------------------------------------------------
# Data migration export (workspace-scoped)
# ---------------------------------------------------------------------------


def build_migration_export(
    workspace_id: int,
    include_source_instance: bool = False,
    *,
    connection_code: str | None = None,
) -> dict:
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
            group_members = DbManager.find_records(
                schemas.WorkspaceGroupMember,
                [
                    schemas.WorkspaceGroupMember.group_id == g.id,
                    schemas.WorkspaceGroupMember.status == "active",
                ],
            )
            member_team_ids = []
            member_workspaces = []
            for group_member in group_members:
                member_workspace = get_workspace_by_id(group_member.workspace_id) if group_member.workspace_id else None
                if member_workspace and member_workspace.team_id != team_id:
                    member_team_ids.append(member_workspace.team_id)
                    member_workspaces.append(
                        {
                            "team_id": member_workspace.team_id,
                            "workspace_name": member_workspace.workspace_name or None,
                        }
                    )
            groups_data.append(
                {
                    "uid": g.uid,
                    "name": g.name,
                    "role": role,
                    "member_team_ids": sorted(set(member_team_ids)),
                    "member_workspaces": member_workspaces,
                }
            )

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
        syncs_data.append(
            {
                "uid": sync.uid,
                "group_uid": None,
                "title": sync.title,
                "sync_mode": sync.sync_mode or "group",
            }
        )
        group = DbManager.get_record(schemas.WorkspaceGroup, id=sync.group_id) if sync.group_id else None
        syncs_data[-1]["group_uid"] = group.uid if group else None
        all_channels = DbManager.find_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.sync_id == sync_id],
        )
        for sync_channel in all_channels:
            owner = get_workspace_by_id(sync_channel.workspace_id) if sync_channel.workspace_id else None
            channel_team = (owner.team_id if owner else None) or (
                team_id if sync_channel.workspace_id == workspace_id else None
            )
            if not channel_team:
                continue
            channel_name = (getattr(sync_channel, "channel_name", None) or "").strip() or None
            sync_channels_data.append(
                {
                    "sync_uid": sync.uid,
                    "team_id": channel_team,
                    "channel_id": sync_channel.channel_id,
                    "channel_name": channel_name,
                    "status": sync_channel.status or "active",
                    "publishes": sync_channel.publishes,
                    "subscribes": sync_channel.subscribes,
                    "reaction_style": sync_channel.reaction_style,
                }
            )
            key = f"{sync.uid}:{sync_channel.channel_id}"
            post_metas = DbManager.find_records(
                schemas.PostMeta,
                [schemas.PostMeta.sync_channel_id == sync_channel.id],
            )
            post_meta_by_key[key] = [
                {
                    "post_id": post_meta.post_id,
                    "ts": slack_message_ts(post_meta.ts),
                    "kind": getattr(post_meta, "kind", constants.POST_META_KIND_MESSAGE)
                    or constants.POST_META_KIND_MESSAGE,
                    "parent_post_id": getattr(post_meta, "parent_post_id", None),
                    "reaction": getattr(post_meta, "reaction", None),
                    "source_user_id": getattr(post_meta, "source_user_id", None),
                    "source_team_id": (
                        get_workspace_by_id(post_meta.source_workspace_id).team_id
                        if getattr(post_meta, "source_workspace_id", None)
                        and get_workspace_by_id(post_meta.source_workspace_id)
                        else None
                    ),
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

    # Workspace-scoped durable data only. No tokens, private key, or federation_file_parts.
    payload = {
        "version": MIGRATION_VERSION,
        "syncbot_version": constants.app_version(),
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
                payload["source_instance"] = {
                    "webhook_url": endpoint,
                    "instance_id": instance_id,
                    "public_key": public_key_pem,
                }
                if connection_code:
                    payload["source_instance"]["connection_code"] = connection_code
        except Exception as e:
            log_warning("build_migration_export", error=str(e))

    # Sign with Ed25519 (exclude signature from signed bytes; include signed_at)
    try:
        from federation import core as federation

        payload["signed_at"] = datetime.now(UTC).isoformat() + "Z"
        to_sign = {k: v for k, v in payload.items() if k != "signature"}
        raw = canonical_json_dumps(to_sign).decode("utf-8")
        payload["signature"] = federation.sign_body(raw)
    except Exception as e:
        log_warning("build_migration_export", error=str(e))

    log_info(
        "migration_export",
        team_id=team_id,
        signed=bool(payload.get("signature")),
        has_source_instance=bool(payload.get("source_instance")),
        has_connection_code=bool((payload.get("source_instance") or {}).get("connection_code")),
        groups=len(groups_data),
        syncs=len(syncs_data),
        channels=len(sync_channels_data),
    )
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


def _peer_instance_id_from_migration(data: dict) -> str | None:
    source = data.get("source_instance") if isinstance(data.get("source_instance"), dict) else None
    instance_id = str((source or {}).get("instance_id") or "").strip()
    if instance_id:
        peers = DbManager.find_records(
            schemas.Instance,
            [schemas.Instance.instance_id == instance_id],
        )
        if peers and not getattr(peers[0], "private_key_encrypted", None):
            return instance_id
        return None
    return _sole_trusted_peer_instance_id()


def _sole_trusted_peer_instance_id() -> str | None:
    """The only trusted External Connection, if this instance has exactly one."""
    peers = DbManager.find_records(
        schemas.Instance,
        [
            schemas.Instance.private_key_encrypted.is_(None),
            schemas.Instance.status == "active",
            schemas.Instance.trust_status == "trusted",
        ],
    )
    ids = [str(peer.instance_id).strip() for peer in peers if getattr(peer, "instance_id", None)]
    if len(ids) != 1:
        return None
    return ids[0]


def _workspace_id_for_imported_team(
    team_id: str | None,
    *,
    this_workspace_id: int,
    team_id_to_workspace_id: dict[str, int],
) -> int | None:
    """Resolve a Slack Team ID from the file to an existing workspace PK (live or stub)."""
    tid = (team_id or "").strip()
    if not tid:
        return this_workspace_id
    if tid in team_id_to_workspace_id:
        return team_id_to_workspace_id[tid]
    matches = DbManager.find_records(schemas.Workspace, [schemas.Workspace.team_id == tid])
    if matches and getattr(matches[0], "id", None):
        team_id_to_workspace_id[tid] = matches[0].id
        return matches[0].id
    return None


def _ensure_group_membership(group_id: int, workspace_id: int, *, role: str, now) -> None:
    if role == "creator":
        role = "member"
    if role not in ("owner", "member"):
        role = "member"
    memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.workspace_id == workspace_id,
        ],
    )
    if memberships:
        DbManager.update_records(
            schemas.WorkspaceGroupMember,
            [schemas.WorkspaceGroupMember.id == memberships[0].id],
            {
                schemas.WorkspaceGroupMember.status: "active",
                schemas.WorkspaceGroupMember.role: role,
                schemas.WorkspaceGroupMember.deleted_at: None,
            },
        )
        return
    DbManager.create_record(
        schemas.WorkspaceGroupMember(
            group_id=group_id,
            workspace_id=workspace_id,
            status="active",
            role=role,
            joined_at=now,
        )
    )


def import_migration_data(
    data: dict,
    workspace_id: int,
    group_id: int | None = None,
    *,
    team_id_to_workspace_id: dict[str, int],
) -> None:
    """Merge migration data by stable group/sync uid without creating live workspaces."""
    syncs_export = data.get("syncs", [])
    sync_channels_export = data.get("sync_channels", [])
    post_meta_export = data.get("post_meta", {})
    user_directory_export = data.get("user_directory", [])
    user_mappings_export = data.get("user_mappings", [])
    now = datetime.now(UTC)
    this_workspace = get_workspace_by_id(workspace_id)
    if this_workspace and this_workspace.team_id:
        from helpers.workspace_kind import heal_workspace_to_local

        heal_workspace_to_local(this_workspace.team_id, source="import")
        this_workspace = get_workspace_by_id(workspace_id)
        if this_workspace:
            workspace_id = this_workspace.id
            team_id_to_workspace_id[this_workspace.team_id] = workspace_id

    group_uid_to_id: dict[str, int] = {}
    for group_entry in data.get("groups", []):
        uid = str(group_entry.get("uid") or "").strip()
        name = str(group_entry.get("name") or "").strip()
        if not uid or not name:
            continue
        existing = DbManager.find_records(schemas.WorkspaceGroup, [schemas.WorkspaceGroup.uid == uid])
        if existing:
            group = existing[0]
        else:
            group = DbManager.create_record(
                schemas.WorkspaceGroup(
                    uid=uid,
                    name=name[:100],
                    invite_code=f"MIG-{secrets.token_hex(4).upper()}",
                    status="active",
                    created_at=now,
                )
            )
        group_uid_to_id[uid] = group.id
        this_role = group_entry.get("role") or "member"
        _ensure_group_membership(group.id, workspace_id, role=this_role, now=now)
        for other_team in group_entry.get("member_team_ids") or []:
            other_id = _workspace_id_for_imported_team(
                str(other_team) if other_team else None,
                this_workspace_id=workspace_id,
                team_id_to_workspace_id=team_id_to_workspace_id,
            )
            if not other_id or other_id == workspace_id:
                continue
            _ensure_group_membership(group.id, other_id, role="member", now=now)

    sync_uid_to_id: dict[str, int] = {}
    for sync_entry in syncs_export:
        uid = str(sync_entry.get("uid") or "").strip()
        title = str(sync_entry.get("title") or "").strip()
        target_group_id = group_uid_to_id.get(str(sync_entry.get("group_uid") or ""))
        if not uid or not title or not target_group_id:
            continue
        existing = DbManager.find_records(schemas.Sync, [schemas.Sync.uid == uid])
        if existing:
            sync = existing[0]
            DbManager.update_records(
                schemas.Sync,
                [schemas.Sync.id == sync.id],
                {
                    schemas.Sync.title: title[:100],
                    schemas.Sync.group_id: target_group_id,
                    schemas.Sync.sync_mode: sync_entry.get("sync_mode") or "group",
                },
            )
        else:
            sync = DbManager.create_record(
                schemas.Sync(
                    uid=uid,
                    title=title[:100],
                    group_id=target_group_id,
                    sync_mode=sync_entry.get("sync_mode") or "group",
                )
            )
        sync_uid_to_id[uid] = sync.id

    for sc_entry in sync_channels_export:
        sync_uid = str(sc_entry.get("sync_uid") or "")
        channel_id = sc_entry.get("channel_id")
        status = sc_entry.get("status", "active")
        sync_id = sync_uid_to_id.get(sync_uid)
        if not sync_id or not channel_id:
            continue
        channel_name = str(sc_entry.get("channel_name") or "").strip().removeprefix("#")[:100] or None
        target_ws_id = _workspace_id_for_imported_team(
            str(sc_entry.get("team_id") or "") or None,
            this_workspace_id=workspace_id,
            team_id_to_workspace_id=team_id_to_workspace_id,
        )
        if not target_ws_id:
            continue
        existing_channels = DbManager.find_records(
            schemas.SyncChannel,
            [
                schemas.SyncChannel.sync_id == sync_id,
                schemas.SyncChannel.workspace_id == target_ws_id,
                schemas.SyncChannel.channel_id == channel_id,
            ],
        )
        if existing_channels:
            new_sync_channel = existing_channels[0]
            updates = {
                schemas.SyncChannel.status: status,
                schemas.SyncChannel.publishes: sc_entry.get("publishes", True),
                schemas.SyncChannel.subscribes: sc_entry.get("subscribes", True),
                schemas.SyncChannel.reaction_style: sc_entry.get("reaction_style"),
                schemas.SyncChannel.deleted_at: None,
            }
            if channel_name:
                updates[schemas.SyncChannel.channel_name] = channel_name
            DbManager.update_records(
                schemas.SyncChannel,
                [schemas.SyncChannel.id == new_sync_channel.id],
                updates,
            )
        else:
            new_sync_channel = DbManager.create_record(
                schemas.SyncChannel(
                    sync_id=sync_id,
                    workspace_id=target_ws_id,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    status=status,
                    publishes=sc_entry.get("publishes", True),
                    subscribes=sc_entry.get("subscribes", True),
                    reaction_style=sc_entry.get("reaction_style"),
                    created_at=now,
                )
            )
        key = f"{sync_uid}:{channel_id}"
        existing_post_keys = {
            (row.post_id, slack_message_ts(row.ts))
            for row in DbManager.find_records(
                schemas.PostMeta,
                [schemas.PostMeta.sync_channel_id == new_sync_channel.id],
            )
        }
        for post_meta in post_meta_export.get(key, []):
            post_key = (post_meta.get("post_id"), slack_message_ts(post_meta.get("ts")))
            if post_key in existing_post_keys:
                continue
            source_team_id = post_meta.get("source_team_id")
            DbManager.create_record(
                schemas.PostMeta(
                    post_id=post_meta["post_id"],
                    sync_channel_id=new_sync_channel.id,
                    ts=post_meta_ts(post_meta["ts"]),
                    kind=post_meta.get("kind") or constants.POST_META_KIND_MESSAGE,
                    parent_post_id=post_meta.get("parent_post_id"),
                    reaction=post_meta.get("reaction"),
                    source_user_id=post_meta.get("source_user_id"),
                    source_workspace_id=team_id_to_workspace_id.get(source_team_id) if source_team_id else None,
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
                mapped_at=now,
                group_id=None,
            )
        )

    log_info(
        "migration_import",
        team_id=(data.get("workspace") or {}).get("team_id"),
        workspace_id=workspace_id,
        groups=len(group_uid_to_id),
        syncs=len(sync_uid_to_id),
        channels=len(sync_channels_export),
        mappings=len(user_mappings_export),
    )
