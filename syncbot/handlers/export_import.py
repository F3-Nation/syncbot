"""Backup/Restore and Data Migration handlers (modals and submissions)."""

import contextlib
import json
import logging
from datetime import UTC, datetime
from logging import Logger

from slack_sdk.web import WebClient

import builders
import helpers
from db import DbManager, schemas
from helpers import export_import as ei
from helpers.workspace import invalidate_fed_ws_for_sync_cache
from slack import actions

_logger = logging.getLogger(__name__)

# Uploaded JSON (backup / migration) download limits — matches interaction-time budget.
_UPLOAD_DOWNLOAD_TIMEOUT = 10
_MAX_IMPORT_BYTES = 50 * 1024 * 1024  # 50 MiB


def _download_uploaded_file(file_url: str, token: str) -> tuple[str | None, str | None]:
    """Download a Slack-hosted uploaded file. Returns ``(utf8_text, None)`` or ``(None, error_message)``."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(file_url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=_UPLOAD_DOWNLOAD_TIMEOUT) as resp:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_IMPORT_BYTES:
                    return None, "Uploaded file exceeds maximum size (50 MB)."
                chunks.append(chunk)
            raw = b"".join(chunks)
    except urllib.error.HTTPError as e:
        _logger.exception("upload download HTTP error: %s", e)
        return None, "Failed to download the uploaded file."
    except TimeoutError as e:
        _logger.exception("upload download timed out: %s", e)
        return None, "Failed to download the uploaded file."
    except OSError as e:
        _logger.exception("upload download failed: %s", e)
        return None, "Failed to download the uploaded file."
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as e:
        return None, f"Invalid encoding in uploaded file: {e}"


def _is_admin(client: WebClient, user_id: str, body: dict) -> bool:
    team_id = (
        helpers.safe_get(body, "team", "id")
        or helpers.safe_get(body, "view", "team_id")
        or helpers.safe_get(body, "team_id")
    )
    return helpers.is_workspace_admin(client, user_id) if user_id and team_id else False


def _team_id_for_backup_gate(body: dict) -> str | None:
    """Slack team_id for primary-workspace backup/restore gating."""
    return (
        helpers.safe_get(body, "team", "id")
        or helpers.safe_get(body, "view", "team_id")
        or helpers.safe_get(body, "team_id")
    )


def _open_dm_channel(client: WebClient, user_id: str) -> str:
    """Open (or reopen) a DM with *user_id* and return the channel ID."""
    resp = client.conversations_open(users=[user_id])
    return resp["channel"]["id"]


# ---------------------------------------------------------------------------
# Backup/Restore
# ---------------------------------------------------------------------------


def handle_backup_restore(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open Backup/Restore modal (admin only)."""
    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    if not helpers.is_backup_visible_for_workspace(_team_id_for_backup_gate(body)):
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    from slack import orm

    download_blocks = [
        orm.SectionBlock(label="*Backup*\nSend a JSON backup file as a SyncBot DM."),
        orm.ActionsBlock(
            elements=[
                orm.ButtonElement(
                    label=":floppy_disk: Send Backup File",
                    action=actions.CONFIG_BACKUP_DOWNLOAD,
                ),
            ],
        ),
        orm.DividerBlock(),
        orm.SectionBlock(
            label="*Restore*\nUpload a JSON backup file. The integrity of the file will be checked.",
        ),
    ]

    restore_block = {
        "type": "input",
        "block_id": actions.CONFIG_BACKUP_RESTORE_JSON_INPUT,
        "label": {"type": "plain_text", "text": " "},
        "element": {
            "type": "file_input",
            "action_id": actions.CONFIG_BACKUP_RESTORE_JSON_INPUT,
            "filetypes": ["json"],
            "max_files": 1,
        },
    }

    view = orm.BlockView(blocks=download_blocks)
    modal_blocks = view.as_form_field()
    modal_blocks.append(restore_block)

    orm.open_or_push_view(
        client,
        trigger_id,
        {
            "type": "modal",
            "callback_id": actions.CONFIG_BACKUP_RESTORE_SUBMIT,
            "title": {"type": "plain_text", "text": "Backup / Restore"},
            "submit": {"type": "plain_text", "text": "Restore"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": modal_blocks,
        },
        body=body,
    )


def handle_backup_download(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Generate backup and send to user's DM (called from modal button)."""
    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    if not helpers.is_backup_visible_for_workspace(_team_id_for_backup_gate(body)):
        return
    try:
        payload = ei.build_full_backup()
        json_str = json.dumps(payload, default=ei._json_serializer, indent=2)
        dm_channel = _open_dm_channel(client, user_id)
        client.files_upload_v2(
            content=json_str,
            filename=f"syncbot-backup-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json",
            channel=dm_channel,
            initial_comment=":floppy_disk: Here is your SyncBot JSON backup. Keep this file secure.",
        )
    except Exception as e:
        _logger.exception("backup_download failed: %s", e)
        return

    view_id = helpers.safe_get(body, "view", "id")
    if view_id:
        with contextlib.suppress(Exception):
            client.views_update(
                view_id=view_id,
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Backup / Restore"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": ":white_check_mark: *Backup Sent!*\n\nCheck your SyncBot DMs to download the backup file.",
                            },
                        },
                    ],
                },
            )


def handle_backup_restore_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase: validate upload; return errors, push confirm modal, or ``None`` to close."""
    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return None
    if not helpers.is_backup_visible_for_workspace(_team_id_for_backup_gate(body)):
        return None

    values = helpers.safe_get(body, "view", "state", "values") or {}
    file_data = helpers.safe_get(
        values, actions.CONFIG_BACKUP_RESTORE_JSON_INPUT, actions.CONFIG_BACKUP_RESTORE_JSON_INPUT
    )
    files = file_data.get("files") if file_data else None

    if not files:
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: "Upload a JSON backup file to restore."},
        }

    file_info = files[0]
    file_url = file_info.get("url_private_download") or file_info.get("url_private")
    if not file_url:
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: "Could not retrieve the uploaded file."},
        }

    json_text, dl_err = _download_uploaded_file(file_url, client.token)
    if dl_err:
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: dl_err},
        }

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: f"Invalid JSON in uploaded file: {e}"},
        }

    if data.get("version") != ei.BACKUP_VERSION:
        return {
            "response_action": "errors",
            "errors": {
                actions.CONFIG_BACKUP_RESTORE_JSON_INPUT: f"Unsupported backup version (expected {ei.BACKUP_VERSION})."
            },
        }

    hmac_ok = ei.verify_backup_hmac(data)
    key_ok = ei.verify_backup_encryption_key(data)

    if not hmac_ok or not key_ok:
        from helpers._cache import _cache_set

        cache_key = f"restore_pending:{user_id}"
        _cache_set(cache_key, data, ttl=600)
        return {
            "response_action": "push",
            "view": {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Confirm Restore"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                (
                                    "*WARNING: Integrity Check Failed!* The file has been tampered with. Only proceed if you intentionally edited the file.\n\n"
                                    if not hmac_ok
                                    else ""
                                )
                                + (
                                    "*WARNING: Encryption Key Mismatch!* Restored bot tokens will not be usable. Workspaces will have to reinstall the app.\n\n"
                                    if not key_ok
                                    else ""
                                )
                                + "Do you want to proceed with the restore anyway?"
                            ),
                        },
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Proceed Anyway"},
                                "style": "danger",
                                "action_id": actions.CONFIG_BACKUP_RESTORE_PROCEED,
                                "value": user_id,
                            },
                        ],
                    },
                ],
            },
        }

    return None


def handle_backup_restore_submit_work(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Lazy work phase: run restore after modal closed (happy path)."""
    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    if not helpers.is_backup_visible_for_workspace(_team_id_for_backup_gate(body)):
        return

    values = helpers.safe_get(body, "view", "state", "values") or {}
    file_data = helpers.safe_get(
        values, actions.CONFIG_BACKUP_RESTORE_JSON_INPUT, actions.CONFIG_BACKUP_RESTORE_JSON_INPUT
    )
    files = file_data.get("files") if file_data else None
    if not files:
        return

    file_info = files[0]
    file_url = file_info.get("url_private_download") or file_info.get("url_private")
    if not file_url:
        return

    json_text, dl_err = _download_uploaded_file(file_url, client.token)
    if dl_err:
        return

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return

    if data.get("version") != ei.BACKUP_VERSION:
        return

    hmac_ok = ei.verify_backup_hmac(data)
    key_ok = ei.verify_backup_encryption_key(data)
    if not hmac_ok or not key_ok:
        return

    _do_restore(data, client, user_id)


def handle_backup_restore_proceed(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Proceed with restore after user clicked the danger button despite warnings."""
    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    if not helpers.is_backup_visible_for_workspace(_team_id_for_backup_gate(body)):
        return
    from helpers._cache import _cache_get

    data = _cache_get(f"restore_pending:{user_id}")
    if not data:
        _logger.warning("backup_restore_proceed: restore data expired for user %s", user_id)
        return
    _do_restore(data, client, user_id)


def _do_restore(data: dict, client: WebClient, user_id: str) -> None:
    """Run restore, invalidate caches, and refresh the Home tab for all restored workspaces."""
    try:
        team_ids = ei.restore_full_backup(data, skip_hmac_check=True, skip_encryption_key_check=True)
        ei.invalidate_home_tab_caches_for_all_teams(team_ids)
        invalidate_fed_ws_for_sync_cache()
    except Exception as e:
        _logger.exception("restore failed: %s", e)
        raise

    for team_id in team_ids:
        workspace_rows = DbManager.find_records(schemas.Workspace, [schemas.Workspace.team_id == team_id])
        if workspace_rows:
            try:
                builders.refresh_home_tab_for_workspace(workspace_rows[0], _logger)
            except Exception as e:
                _logger.warning("_do_restore: failed to refresh home tab for %s: %s", team_id, e)


# ---------------------------------------------------------------------------
# Data Migration
# ---------------------------------------------------------------------------


def handle_data_migration(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open Data Migration modal (primary-workspace admin only, federation enabled)."""
    if not helpers.federation_enabled():
        return
    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    team_id = _team_id_for_backup_gate(body)
    if not _is_admin(client, user_id, body) or not helpers.is_primary_workspace(team_id):
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    from slack import orm

    export_blocks = [
        orm.SectionBlock(
            label="*Export*\nDownload your Workspace data for migration to another instance. You will receive a JSON file in your DM.",
        ),
        orm.ActionsBlock(
            elements=[
                orm.ButtonElement(
                    label=":outbox_tray: Export my Workspace data",
                    action=actions.CONFIG_DATA_MIGRATION_EXPORT,
                ),
            ],
        ),
        orm.DividerBlock(),
        orm.SectionBlock(
            label="*Import*\nUpload a migration JSON file. Existing Sync Channels in the federated Group will be replaced.",
        ),
    ]

    import_block = {
        "type": "input",
        "block_id": actions.CONFIG_DATA_MIGRATION_JSON_INPUT,
        "label": {"type": "plain_text", "text": " "},
        "element": {
            "type": "file_input",
            "action_id": actions.CONFIG_DATA_MIGRATION_JSON_INPUT,
            "filetypes": ["json"],
            "max_files": 1,
        },
    }

    view = orm.BlockView(blocks=export_blocks)
    modal_blocks = view.as_form_field()
    modal_blocks.append(import_block)

    orm.open_or_push_view(
        client,
        trigger_id,
        {
            "type": "modal",
            "callback_id": actions.CONFIG_DATA_MIGRATION_SUBMIT,
            "title": {"type": "plain_text", "text": "Data Migration"},
            "submit": {"type": "plain_text", "text": "Import"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": modal_blocks,
        },
        body=body,
    )


def handle_data_migration_export(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Export workspace migration JSON and send to user's DM."""
    if not helpers.federation_enabled():
        return
    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    team_id = helpers.safe_get(body, "team", "id") or helpers.safe_get(body, "team_id")
    if not _is_admin(client, user_id, body):
        return
    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return
    try:
        payload = ei.build_migration_export(workspace_record.id, include_source_instance=True)
        json_str = json.dumps(payload, default=ei._json_serializer, indent=2)
        dm_channel = _open_dm_channel(client, user_id)
        client.files_upload_v2(
            content=json_str,
            filename=f"syncbot-migration-{workspace_record.team_id}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json",
            channel=dm_channel,
            initial_comment="Your SyncBot workspace migration file. Use it on the new instance after connecting via federation.",
        )
    except Exception as e:
        _logger.exception("data_migration_export failed: %s", e)


def _data_migration_prepare(
    body: dict,
    client: WebClient,
    context: dict,
) -> tuple[dict | None, dict | None, int | None, dict[str, int] | None, object | None]:
    """Shared validation for migration ack/work.

    Returns ``(error_ack_dict, data, group_id, team_id_to_workspace_id, workspace_record)``.
    """
    if not helpers.federation_enabled():
        return None, None, None, None, None
    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    team_id = helpers.safe_get(body, "view", "team_id") or helpers.safe_get(body, "team_id")
    if not _is_admin(client, user_id, body):
        return None, None, None, None, None

    values = helpers.safe_get(body, "view", "state", "values") or {}
    file_data = helpers.safe_get(
        values, actions.CONFIG_DATA_MIGRATION_JSON_INPUT, actions.CONFIG_DATA_MIGRATION_JSON_INPUT
    )
    files = file_data.get("files") if file_data else None

    if not files:
        return (
            {
                "response_action": "errors",
                "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: "Upload a migration JSON file to import."},
            },
            None,
            None,
            None,
            None,
        )

    file_info = files[0]
    file_url = file_info.get("url_private_download") or file_info.get("url_private")
    if not file_url:
        return (
            {
                "response_action": "errors",
                "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: "Could not retrieve the uploaded file."},
            },
            None,
            None,
            None,
            None,
        )

    json_text, dl_err = _download_uploaded_file(file_url, client.token)
    if dl_err:
        return (
            {
                "response_action": "errors",
                "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: dl_err},
            },
            None,
            None,
            None,
            None,
        )

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        return (
            {
                "response_action": "errors",
                "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: f"Invalid JSON in uploaded file: {e}"},
            },
            None,
            None,
            None,
            None,
        )

    if data.get("version") != ei.MIGRATION_VERSION:
        return (
            {
                "response_action": "errors",
                "errors": {
                    actions.CONFIG_DATA_MIGRATION_JSON_INPUT: f"Unsupported migration version (expected {ei.MIGRATION_VERSION})."
                },
            },
            None,
            None,
            None,
            None,
        )

    workspace_payload = data.get("workspace", {})
    export_team_id = workspace_payload.get("team_id")
    if not export_team_id:
        return (
            {
                "response_action": "errors",
                "errors": {actions.CONFIG_DATA_MIGRATION_JSON_INPUT: "Migration file missing workspace.team_id."},
            },
            None,
            None,
            None,
            None,
        )

    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record or workspace_record.team_id != export_team_id:
        return (
            {
                "response_action": "errors",
                "errors": {
                    actions.CONFIG_DATA_MIGRATION_JSON_INPUT: "This migration file is for a different workspace. Open the app from the workspace that matches the migration file."
                },
            },
            None,
            None,
            None,
            None,
        )

    team_id_to_workspace_id = {workspace_record.team_id: workspace_record.id}
    workspaces_b = DbManager.find_records(schemas.Workspace, [schemas.Workspace.deleted_at.is_(None)])
    for w in workspaces_b:
        if w.team_id:
            team_id_to_workspace_id[w.team_id] = w.id

    source = data.get("source_instance")
    if source and source.get("connection_code"):
        import secrets

        from federation import core as federation

        result = federation.initiate_federation_connect(
            source["webhook_url"],
            source["connection_code"],
            team_id=workspace_record.team_id,
            workspace_name=workspace_record.workspace_name or None,
        )
        if result and result.get("ok"):
            fed_ws = federation.get_or_create_federated_workspace(
                instance_id=source["instance_id"],
                webhook_url=source["webhook_url"],
                public_key=source["public_key"],
                name=f"Connection {source['instance_id'][:8]}",
            )
            my_groups = helpers.get_groups_for_workspace(workspace_record.id)
            my_group_ids = {g.id for g, _ in my_groups}
            fed_members = DbManager.find_records(
                schemas.WorkspaceGroupMember,
                [
                    schemas.WorkspaceGroupMember.federated_workspace_id == fed_ws.id,
                    schemas.WorkspaceGroupMember.deleted_at.is_(None),
                    schemas.WorkspaceGroupMember.status == "active",
                ],
            )
            found = False
            for fm in fed_members:
                if fm.group_id in my_group_ids:
                    found = True
                    break
            if not found:
                now = datetime.now(UTC)
                new_group = schemas.WorkspaceGroup(
                    name=f"Federation — {fed_ws.name}",
                    invite_code=f"FED-{secrets.token_hex(4).upper()}",
                    status="active",
                    created_at=now,
                )
                DbManager.create_record(new_group)
                DbManager.create_record(
                    schemas.WorkspaceGroupMember(
                        group_id=new_group.id,
                        workspace_id=workspace_record.id,
                        status="active",
                        role="owner",
                        joined_at=now,
                    )
                )
                DbManager.create_record(
                    schemas.WorkspaceGroupMember(
                        group_id=new_group.id,
                        federated_workspace_id=fed_ws.id,
                        status="active",
                        role="member",
                        joined_at=now,
                    )
                )

    my_groups = helpers.get_groups_for_workspace(workspace_record.id)
    my_group_ids = {g.id for g, _ in my_groups}
    fed_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.federated_workspace_id.isnot(None),
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
            schemas.WorkspaceGroupMember.status == "active",
        ],
    )
    candidate_groups = [fm.group_id for fm in fed_members if fm.group_id in my_group_ids]
    group_id = candidate_groups[0] if candidate_groups else None
    if not group_id:
        return (
            {
                "response_action": "errors",
                "errors": {
                    actions.CONFIG_DATA_MIGRATION_JSON_INPUT: "No federation connection found. Connect to the other instance first (Enter Connection Code), then import."
                },
            },
            None,
            None,
            None,
            None,
        )

    return None, data, group_id, team_id_to_workspace_id, workspace_record


def handle_data_migration_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase: validate; return errors, push confirm, or ``None`` to close before lazy import."""
    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    err, data, group_id, team_id_to_workspace_id, workspace_record = _data_migration_prepare(body, client, context)
    if err is not None:
        return err
    if data is None or group_id is None or team_id_to_workspace_id is None or workspace_record is None:
        return None

    source = data.get("source_instance")
    sig_ok = ei.verify_migration_signature(data)
    if not sig_ok and source:
        from helpers._cache import _cache_set

        cache_key = f"migration_import_pending:{user_id}"
        _cache_set(
            cache_key,
            {
                "data": data,
                "group_id": group_id,
                "workspace_id": workspace_record.id,
                "team_id_to_workspace_id": team_id_to_workspace_id,
            },
            ttl=600,
        )
        return {
            "response_action": "push",
            "view": {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Confirm Import"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*Integrity check failed.* The file may have been modified or could be malicious. Only proceed if you intentionally edited the file.\n\nProceed with import anyway?",
                        },
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Proceed Anyway"},
                                "style": "danger",
                                "action_id": actions.CONFIG_DATA_MIGRATION_PROCEED,
                                "value": user_id,
                            },
                        ],
                    },
                ],
            },
        }

    return None


def handle_data_migration_submit_work(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Lazy work phase: import migration data after modal closed."""
    if not helpers.federation_enabled():
        return
    err, data, group_id, team_id_to_workspace_id, workspace_record = _data_migration_prepare(body, client, context)
    if (
        err is not None
        or data is None
        or group_id is None
        or team_id_to_workspace_id is None
        or workspace_record is None
    ):
        return

    source = data.get("source_instance")
    sig_ok = ei.verify_migration_signature(data)
    if not sig_ok and source:
        return

    ei.import_migration_data(
        data,
        workspace_record.id,
        group_id,
        team_id_to_workspace_id=team_id_to_workspace_id,
    )
    ei.invalidate_home_tab_caches_for_team(workspace_record.team_id)


def handle_data_migration_proceed(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Proceed with import after user clicked the danger button despite warnings."""
    if not helpers.federation_enabled():
        return
    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    from helpers._cache import _cache_get

    meta = _cache_get(f"migration_import_pending:{user_id}")
    if not meta:
        _logger.warning("data_migration_proceed: import data expired for user %s", user_id)
        return
    data = meta.get("data")
    group_id = meta.get("group_id")
    workspace_id = meta.get("workspace_id")
    team_id_to_workspace_id = meta.get("team_id_to_workspace_id", {})
    if not data or not group_id or not workspace_id:
        return

    workspace_record = helpers.get_workspace_by_id(workspace_id)
    if not workspace_record:
        return

    ei.import_migration_data(
        data,
        workspace_record.id,
        group_id,
        team_id_to_workspace_id=team_id_to_workspace_id,
    )
    ei.invalidate_home_tab_caches_for_team(workspace_record.team_id)
