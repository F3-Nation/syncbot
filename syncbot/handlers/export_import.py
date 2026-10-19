"""Backup/Restore and Data Migration handlers (modals and submissions)."""

import json
import logging
import os
from datetime import UTC, datetime
from logging import Logger

from slack_sdk.web import WebClient

import builders
import constants
import helpers
from db import DbManager, schemas
from handlers._common import _close_modal_done, _update_wait_modal, _wait_modal_ack
from helpers import export_import as ei
from helpers.workspace import invalidate_fed_ws_for_sync_cache
from logger import log_debug, log_error, log_warning
from slack import actions, orm

# Uploaded JSON (backup / migration) download timeout — size follows Slack's per-file max.
_UPLOAD_DOWNLOAD_TIMEOUT = 10


def _primary_workspace_code_tick() -> str:
    """Primary Workspace name in code ticks, or ``Not set``."""
    team_id = (os.environ.get(constants.PRIMARY_WORKSPACE) or "").strip()
    if not team_id:
        return "`Not set`"
    matches = DbManager.find_records(
        schemas.Workspace,
        [schemas.Workspace.team_id == team_id, schemas.Workspace.deleted_at.is_(None)],
    )
    if not matches:
        return f"`{team_id}`"
    return f"`{helpers.resolve_workspace_name(matches[0]) or team_id}`"


def _download_uploaded_file(file_url: str, token: str) -> tuple[str | None, str | None]:
    """Download a Slack-hosted uploaded file. Returns ``(utf8_text, None)`` or ``(None, error_message)``."""
    import urllib.error
    import urllib.request

    from helpers.files import max_file_bytes

    req = urllib.request.Request(file_url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=_UPLOAD_DOWNLOAD_TIMEOUT) as resp:
            chunks: list[bytes] = []
            total = 0
            limit = max_file_bytes()
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    return None, "Uploaded file exceeds Slack's per-file size limit."
                chunks.append(chunk)
            raw = b"".join(chunks)
    except urllib.error.HTTPError as e:
        log_error("upload_download_http_error", error=str(e), exc_info=True)
        return None, "Failed to download the uploaded file."
    except TimeoutError as e:
        log_error("upload_download_timeout", error=str(e), exc_info=True)
        return None, "Failed to download the uploaded file."
    except OSError as e:
        log_error("upload_download_failed", error=str(e), exc_info=True)
        return None, "Failed to download the uploaded file."
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as e:
        return None, f"Invalid encoding in uploaded file: {e}"


def _is_admin(client: WebClient, user_id: str, body: dict) -> bool:
    team_id = helpers.get_team_id_from_body(body)
    return helpers.is_workspace_admin(client, user_id) if user_id and team_id else False


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
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    if not helpers.is_backup_visible_for_workspace(helpers.get_team_id_from_body(body)):
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

    orm.update_opened_view(
        client,
        body,
        trigger_id,
        {
            "type": "modal",
            "callback_id": actions.CONFIG_BACKUP_RESTORE_SUBMIT,
            "title": {"type": "plain_text", "text": "Backup / Restore"},
            "submit": {"type": "plain_text", "text": "Restore"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": modal_blocks,
        },
    )


def handle_backup_download(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Generate backup and send to user's DM (called from modal button)."""
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    if not helpers.is_backup_visible_for_workspace(helpers.get_team_id_from_body(body)):
        return
    _update_wait_modal(
        client,
        body,
        title="Backup / Restore",
        callback_id=actions.CONFIG_BACKUP_RESTORE_SUBMIT,
        dm=True,
    )
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
        log_error("backup_download_failed", error=str(e), exc_info=True)
        _close_modal_done(
            client,
            body,
            ":warning: SyncBot could not send the backup file. Try Download again.",
        )


def handle_backup_restore_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase: validate upload; return errors, push confirm modal, or the wait view."""
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return None
    if not helpers.is_backup_visible_for_workspace(helpers.get_team_id_from_body(body)):
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

    return _wait_modal_ack(
        title="Backup / Restore",
        callback_id=actions.CONFIG_BACKUP_RESTORE_SUBMIT,
        dm=False,
    )


def handle_backup_restore_submit_work(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Lazy work phase: run restore after the ack shows the wait modal."""
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    if not helpers.is_backup_visible_for_workspace(helpers.get_team_id_from_body(body)):
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

    _finish_restore(data, client, body, user_id)


def handle_backup_restore_proceed(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Proceed with restore after user clicked the danger button despite warnings."""
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    if not helpers.is_backup_visible_for_workspace(helpers.get_team_id_from_body(body)):
        return
    from helpers._cache import _cache_get

    data = _cache_get(f"restore_pending:{user_id}")
    if not data:
        log_warning("backup_restore_proceed", user_id=user_id)
        return
    _update_wait_modal(client, body, title="Confirm Restore", dm=False)
    _finish_restore(data, client, body, user_id)


def _finish_restore(data: dict, client: WebClient, body: dict, user_id: str) -> None:
    """Run restore, then replace the wait modal with the outcome."""
    try:
        _do_restore(data, client, user_id)
    except Exception:
        _close_modal_done(client, body, ":warning: Restore failed. Try Backup / Restore again.")
        return
    _close_modal_done(client, body, ":white_check_mark: Restore finished.")


def _do_restore(data: dict, client: WebClient, user_id: str) -> None:
    """Run restore, invalidate caches, and refresh the Home tab for all restored workspaces."""
    try:
        team_ids = ei.restore_full_backup(data, skip_hmac_check=True, skip_encryption_key_check=True)
        ei.invalidate_home_tab_caches_for_all_teams(team_ids)
        invalidate_fed_ws_for_sync_cache()
    except Exception as e:
        log_error("restore_failed", error=str(e), exc_info=True)
        raise

    for team_id in team_ids:
        workspace_rows = DbManager.find_records(schemas.Workspace, [schemas.Workspace.team_id == team_id])
        if workspace_rows:
            try:
                builders.refresh_home_tab_for_workspace(workspace_rows[0], logging.getLogger("syncbot"))
            except Exception as e:
                log_warning("home_refresh_failed", team_id=team_id, error=str(e))


# ---------------------------------------------------------------------------
# Data Migration
# ---------------------------------------------------------------------------


def handle_data_migration(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open Data Migration modal for any Slack workspace admin."""
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    from slack import orm

    export_blocks = [
        orm.SectionBlock(
            label=(
                "*Export*\n"
                "Download this Workspace's SyncBot data (syncs, channels, people mapping; no tokens) "
                "as a JSON file in your DM. Use this when the other instance is already connected, "
                "or you only need the file."
            ),
        ),
        orm.ActionsBlock(
            elements=[
                orm.ButtonElement(
                    label=":outbox_tray: Export",
                    action=actions.CONFIG_DATA_MIGRATION_EXPORT,
                ),
            ],
        ),
    ]
    if helpers.federation_enabled():
        primary = _primary_workspace_code_tick()
        export_blocks.extend(
            [
                orm.SectionBlock(
                    label=(
                        "*Export and Request Connection*\n"
                        f"Same JSON file, plus a request to the admins of {primary} for a "
                        "one-time connection code bundled with the export. Use this when you are "
                        "moving this Workspace to a new SyncBot instance that is not connected yet. "
                        "They Approve or Decline in a DM; you get the file after they approve."
                    ),
                ),
                orm.ActionsBlock(
                    elements=[
                        orm.ButtonElement(
                            label=":link: Export and Request Connection",
                            action=actions.CONFIG_DATA_MIGRATION_REQUEST,
                        ),
                    ],
                ),
            ]
        )
    export_blocks.extend(
        [
            orm.DividerBlock(),
            orm.SectionBlock(
                label="*Import*\nUpload a migration JSON file. Existing Groups and Syncs are merged by their stable IDs.",
            ),
        ]
    )

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

    orm.update_opened_view(
        client,
        body,
        trigger_id,
        {
            "type": "modal",
            "callback_id": actions.CONFIG_DATA_MIGRATION_SUBMIT,
            "title": {"type": "plain_text", "text": "Data Migration"},
            "submit": {"type": "plain_text", "text": "Import"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": modal_blocks,
        },
    )


def handle_data_migration_export(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Export workspace migration JSON and send to user's DM."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return
    _update_wait_modal(
        client,
        body,
        title="Data Migration",
        callback_id=actions.CONFIG_DATA_MIGRATION_SUBMIT,
        dm=True,
    )
    try:
        payload = ei.build_migration_export(workspace_record.id, include_source_instance=False)
        json_str = json.dumps(payload, default=ei._json_serializer, indent=2)
        dm_channel = _open_dm_channel(client, user_id)
        client.files_upload_v2(
            content=json_str,
            filename=f"syncbot-migration-{workspace_record.team_id}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json",
            channel=dm_channel,
            initial_comment=(
                f":outbox_tray: Here is the SyncBot migration file for "
                f"`{helpers.resolve_workspace_name(workspace_record) or workspace_record.team_id}`."
            ),
        )
    except Exception as e:
        log_error("data_migration_export_failed", error=str(e), exc_info=True)
        _close_modal_done(
            client,
            body,
            ":warning: SyncBot could not send the migration file. Try Export again.",
        )


def _request_id_from_action(body: dict, prefix: str) -> int | None:
    action = helpers.safe_get(body, "actions", 0) or {}
    raw = str(action.get("value") or str(action.get("action_id") or "").removeprefix(prefix + "_"))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _dm_migration_requester(request: schemas.FederationPairingRequest, text: str) -> WebClient | None:
    workspace = DbManager.get_record(schemas.Workspace, id=request.subject_team_id)
    if not workspace or workspace.deleted_at is not None or not helpers.get_bot_token(workspace):
        return None
    try:
        workspace_client = WebClient(token=helpers.get_bot_token(workspace))
        channel = _open_dm_channel(workspace_client, request.requested_by_user_id)
        workspace_client.chat_postMessage(channel=channel, text=text)
        return workspace_client
    except Exception:
        log_error("migration_requester_dm_failed", request_id=request.id, exc_info=True)
        return None


def handle_data_migration_request(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Request a primary-admin-approved pairing code for this Workspace export."""
    if not helpers.federation_enabled():
        return
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not _is_admin(client, user_id, body):
        return
    workspace = helpers.get_workspace_record(team_id, body, context, client)
    primary_tick = _primary_workspace_code_tick()
    pending = DbManager.find_records(
        schemas.FederationPairingRequest,
        [
            schemas.FederationPairingRequest.subject_team_id == team_id,
            schemas.FederationPairingRequest.status == "pending",
        ],
    )
    if pending:
        already = (
            f":outbox_tray: A request to export data and create a remote connection is already waiting "
            f"for admins in {primary_tick}."
        )
        _dm_migration_requester(pending[0], already)
        _close_modal_done(client, body, f"{already} You can close this now.")
        return
    request = DbManager.create_record(
        schemas.FederationPairingRequest(
            subject_team_id=team_id,
            requested_by_user_id=user_id,
            requested_at=datetime.now(UTC),
            status="pending",
        )
    )
    primary_team_id = (os.environ.get(constants.PRIMARY_WORKSPACE) or "").strip()
    primary = DbManager.get_record(schemas.Workspace, id=primary_team_id) if primary_team_id else None
    if not primary or primary.deleted_at is not None or not helpers.get_bot_token(primary):
        unreachable = f":warning: SyncBot could not reach the admins in {primary_tick}. You can still use Export."
        _dm_migration_requester(request, unreachable)
        _close_modal_done(client, body, unreachable)
        return
    primary_client = WebClient(token=helpers.get_bot_token(primary))
    display_name, _ = helpers.get_user_info(client, user_id)
    source_name = helpers.resolve_workspace_name(workspace) or team_id
    requester = helpers.code_ticked_display_name(display_name, source_name)
    text = f":globe_with_meridians: {requester} requested to export data and create a remote connection."
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":globe_with_meridians: Approve and Create"},
                    "action_id": f"{actions.CONFIG_PAIRING_REQUEST_APPROVE}_{request.id}",
                    "value": str(request.id),
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":eject: Decline"},
                    "action_id": f"{actions.CONFIG_PAIRING_REQUEST_DECLINE}_{request.id}",
                    "value": str(request.id),
                    "style": "danger",
                },
            ],
        },
    ]
    helpers.notify_admins_dm(
        primary_client,
        text,
        blocks=blocks,
        include_managers=False,
        team_id=primary.team_id,
    )
    _dm_migration_requester(
        request,
        f":outbox_tray: Your request to export data and create a remote connection has been sent to admins in the {primary_tick} workspace.",
    )
    _close_modal_done(
        client,
        body,
        f":outbox_tray: A DM was sent to admins in {primary_tick}. You can close this now.",
    )


def handle_pairing_request_approve(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open Create Connection so the primary admin can name it and pick Workspaces."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    request_id = _request_id_from_action(body, actions.CONFIG_PAIRING_REQUEST_APPROVE)
    if (
        not user_id
        or not request_id
        or not helpers.is_primary_workspace(team_id)
        or not helpers.is_workspace_admin(client, user_id)
    ):
        return
    request = DbManager.get_record(schemas.FederationPairingRequest, id=request_id)
    if not request or request.status != "pending":
        return
    workspace = DbManager.get_record(schemas.Workspace, id=request.subject_team_id)
    if not workspace:
        return
    from handlers.federation_cmds import open_create_external_connection_modal

    leaving = helpers.resolve_workspace_name(workspace) or workspace.team_id
    open_create_external_connection_modal(
        body,
        client,
        request_id=request.id,
        exclude_workspace_id=workspace.id if getattr(workspace, "id", None) else None,
        initial_name=leaving,
        leaving_workspace_name=leaving,
    )


def fulfill_pairing_request(
    request: schemas.FederationPairingRequest,
    *,
    encoded: str,
    raw_code: str,
    resolved_by_user_id: str,
) -> None:
    """Mark the request approved and DM the requester the signed export plus connection code."""
    workspace = DbManager.get_record(schemas.Workspace, id=request.subject_team_id)
    if not workspace:
        return
    code_rows = DbManager.find_records(
        schemas.FederationPairingCode,
        [schemas.FederationPairingCode.code == raw_code],
    )
    payload = ei.build_migration_export(
        workspace.id,
        include_source_instance=True,
        connection_code=encoded,
    )
    DbManager.update_records(
        schemas.FederationPairingRequest,
        [schemas.FederationPairingRequest.id == request.id],
        {
            schemas.FederationPairingRequest.status: "approved",
            schemas.FederationPairingRequest.resolved_by_user_id: resolved_by_user_id or None,
            schemas.FederationPairingRequest.resolved_at: datetime.now(UTC),
            schemas.FederationPairingRequest.pairing_code_id: code_rows[0].id if code_rows else None,
        },
    )
    workspace_client = _dm_migration_requester(
        request,
        f":white_check_mark: Admins in {_primary_workspace_code_tick()} approved your request "
        "to export data and create a remote connection.",
    )
    if workspace_client:
        workspace_client.files_upload_v2(
            content=json.dumps(payload, default=ei._json_serializer, indent=2),
            filename=f"syncbot-migration-{workspace.team_id}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json",
            channel=_open_dm_channel(workspace_client, request.requested_by_user_id),
            initial_comment=(
                f":outbox_tray: Your migration file for "
                f"`{helpers.resolve_workspace_name(workspace) or workspace.team_id}` "
                "includes a 24-hour connection code. Use it on the new instance to Join the connection."
            ),
        )


def handle_pairing_request_decline(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Decline a pending migration pairing request and notify its requester."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    request_id = _request_id_from_action(body, actions.CONFIG_PAIRING_REQUEST_DECLINE)
    if (
        not user_id
        or not request_id
        or not helpers.is_primary_workspace(team_id)
        or not helpers.is_workspace_admin(client, user_id)
    ):
        return
    request = DbManager.get_record(schemas.FederationPairingRequest, id=request_id)
    if not request or request.status != "pending":
        return
    DbManager.update_records(
        schemas.FederationPairingRequest,
        [schemas.FederationPairingRequest.id == request.id],
        {
            schemas.FederationPairingRequest.status: "declined",
            schemas.FederationPairingRequest.resolved_by_user_id: user_id,
            schemas.FederationPairingRequest.resolved_at: datetime.now(UTC),
        },
    )
    _dm_migration_requester(
        request,
        f":eject: Admins in {_primary_workspace_code_tick()} declined your request "
        "to export data and create a remote connection. You can still use Export for the file only.",
    )


def _data_migration_prepare(
    body: dict,
    client: WebClient,
    context: dict,
) -> tuple[dict | None, dict | None, int | None, dict[str, int] | None, object | None]:
    """Shared validation for migration ack/work.

    Returns ``(error_ack_dict, data, group_id, team_id_to_workspace_id, workspace_record)``.
    """
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
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
    workspaces_b = DbManager.find_records(schemas.Workspace, [])
    for w in workspaces_b:
        if w.team_id:
            team_id_to_workspace_id[w.team_id] = w.id

    return None, data, 0, team_id_to_workspace_id, workspace_record


def _migration_import_cache_key(user_id: str) -> str:
    return f"migration_import_pending:{user_id}"


def _migration_message_count(post_meta: dict, sync_uid: str, channel_id: str) -> int:
    key = f"{sync_uid}:{channel_id}"
    count = 0
    for row in post_meta.get(key) or []:
        if not isinstance(row, dict):
            continue
        kind = row.get("kind") or constants.POST_META_KIND_MESSAGE
        if kind == constants.POST_META_KIND_MESSAGE:
            count += 1
    return count


def _migration_overview_blocks(data: dict) -> list:
    """Home-style Group / sync / mapping / message counts from migration JSON."""
    from slack.blocks import section

    groups = [item for item in (data.get("groups") or []) if isinstance(item, dict)]
    syncs = [item for item in (data.get("syncs") or []) if isinstance(item, dict)]
    channels = [item for item in (data.get("sync_channels") or []) if isinstance(item, dict)]
    mappings = [item for item in (data.get("user_mappings") or []) if isinstance(item, dict)]
    post_meta = data.get("post_meta") if isinstance(data.get("post_meta"), dict) else {}
    channels_by_sync: dict[str, list] = {}
    for channel in channels:
        uid = str(channel.get("sync_uid") or "")
        if uid:
            channels_by_sync.setdefault(uid, []).append(channel)
    mapped_count = len(mappings)
    blocks = [section("*This file will import*")]
    if not groups:
        blocks.append(
            section(
                f"No Workspace Groups.\nMapped Users: `{mapped_count}`\n"
                f"Synced Channels: `{len(channels)}`\nSynced Messages: "
                f"`{sum(_migration_message_count(post_meta, str(s.get('uid') or ''), str(c.get('channel_id') or '')) for s in syncs for c in channels_by_sync.get(str(s.get('uid') or ''), []))}`"
            )
        )
        return blocks
    for group in groups:
        uid = str(group.get("uid") or "")
        name = str(group.get("name") or "Unnamed group").strip() or "Unnamed group"
        group_syncs = [item for item in syncs if str(item.get("group_uid") or "") == uid]
        titles = [str(item.get("title") or "").strip() for item in group_syncs if str(item.get("title") or "").strip()]
        channel_count = 0
        message_count = 0
        for sync in group_syncs:
            sync_uid = str(sync.get("uid") or "")
            sync_channels = channels_by_sync.get(sync_uid, [])
            channel_count += len(sync_channels)
            for channel in sync_channels:
                message_count += _migration_message_count(post_meta, sync_uid, str(channel.get("channel_id") or ""))
        title_line = ", ".join(f"`{title}`" for title in titles) if titles else "`None`"
        blocks.append(
            section(
                f"*{name}*\n"
                f"Synced Channels: `{channel_count}` {title_line}\n"
                f"Mapped Users: `{mapped_count}`\n"
                f"Synced Messages: `{message_count}`"
            )
        )
    return blocks


def _ensure_migration_connection(
    data: dict,
    workspace_record,
    context: dict,
) -> None:
    """Join from the bundled connection code, or attach locally if already paired."""
    source = data.get("source_instance")
    if not source:
        return
    encoded = source.get("connection_code")
    if not encoded:
        log_debug(
            "federation_pair",
            direction="outbound",
            ok=False,
            reason="no_connection_code",
            team_id=workspace_record.team_id,
            peer_instance_id=source.get("instance_id"),
        )
        return
    from federation import core as federation

    parsed = federation.parse_federation_code(encoded)
    if not parsed:
        log_warning(
            "federation_pair",
            direction="outbound",
            ok=False,
            reason="invalid_connection_code",
            team_id=workspace_record.team_id,
            peer_instance_id=source.get("instance_id"),
        )
        return
    remote_instance_id = parsed["instance_id"]
    existing = DbManager.find_records(
        schemas.Instance,
        [
            schemas.Instance.instance_id == remote_instance_id,
            schemas.Instance.status == "active",
        ],
    )
    result = None
    if not existing:
        result = federation.initiate_federation_connect(
            parsed["webhook_url"],
            parsed["code"],
            team_id=workspace_record.team_id,
            workspace_name=workspace_record.workspace_name or None,
            context=context,
        )
        if not result or not result.get("ok"):
            log_warning(
                "federation_pair",
                direction="outbound",
                ok=False,
                reason="connect_failed_using_blob",
                team_id=workspace_record.team_id,
                peer_instance_id=remote_instance_id,
            )
    remote_team_id = result.get("team_id") if result and isinstance(result.get("team_id"), str) else None
    remote_workspace_name = None
    if result and isinstance(result.get("workspace_name"), str):
        remote_workspace_name = result.get("workspace_name")
    if remote_team_id:
        remote_team_id = remote_team_id.strip() or None
    remote_name = (parsed.get("label") or "").strip() or f"Connection {remote_instance_id[:8]}"
    primary_name = (parsed.get("primary_workspace_name") or "").strip() or None
    primary_team = (parsed.get("primary_team_id") or "").strip() or None
    fed_ws = federation.get_or_create_instance(
        instance_id=remote_instance_id,
        webhook_url=parsed["webhook_url"],
        public_key=(result.get("public_key") if result else None) or parsed["public_key"],
        name=remote_name,
        primary_team_id=primary_team or remote_team_id,
        primary_workspace_name=primary_name or remote_workspace_name,
    )
    existing_allow = DbManager.find_records(
        schemas.FederationWorkspaceAllowlist,
        [
            schemas.FederationWorkspaceAllowlist.instance_id == fed_ws.instance_id,
            schemas.FederationWorkspaceAllowlist.workspace_id == workspace_record.id,
        ],
    )
    if not existing_allow:
        DbManager.create_record(
            schemas.FederationWorkspaceAllowlist(
                instance_id=fed_ws.instance_id,
                workspace_id=workspace_record.id,
            )
        )
    invalidate_fed_ws_for_sync_cache()


def _run_migration_import(
    data: dict,
    workspace_record,
    team_id_to_workspace_id: dict,
    group_id: int,
    *,
    client: WebClient,
    acting_user_id: str | None = None,
    context: dict | None = None,
) -> None:
    ei.import_migration_data(
        data,
        workspace_record.id,
        group_id,
        team_id_to_workspace_id=team_id_to_workspace_id,
    )
    restored = helpers.get_workspace_by_id(workspace_record.id) or workspace_record
    helpers.heal_restored_sync_channels(
        restored,
        client=client,
        acting_user_id=acting_user_id,
        context=context,
        source="import",
    )
    ei.invalidate_home_tab_caches_for_team(workspace_record.team_id)


def handle_data_migration_submit_ack(
    body: dict,
    client: WebClient,
    context: dict,
) -> dict | None:
    """Ack phase: validate the file and update the modal to an import review."""
    user_id = helpers.get_user_id_from_body(body)
    err, data, group_id, team_id_to_workspace_id, workspace_record = _data_migration_prepare(body, client, context)
    if err is not None:
        return err
    if data is None or group_id is None or team_id_to_workspace_id is None or workspace_record is None:
        return None

    source = data.get("source_instance")
    sig_ok = ei.verify_migration_signature(data)
    from helpers._cache import _cache_set

    _cache_set(
        _migration_import_cache_key(user_id),
        {
            "data": data,
            "group_id": group_id,
            "workspace_id": workspace_record.id,
            "team_id_to_workspace_id": team_id_to_workspace_id,
        },
        ttl=600,
    )
    from federation import core as federation
    from handlers.federation_cmds import _connection_detail_blocks
    from slack.blocks import section

    parsed = None
    if source and source.get("connection_code"):
        parsed = federation.parse_federation_code(source["connection_code"])
    blocks = []
    if not sig_ok and source:
        blocks.append(
            section(
                ":warning: *Integrity check failed.* The file may have been modified. "
                "Only Import if you intentionally edited it."
            )
        )
    if parsed:
        blocks.extend(_connection_detail_blocks(parsed))
    elif source and source.get("connection_code"):
        blocks.append(
            section(
                ":warning: The bundled connection code is invalid or expired. Import will still merge Groups and Syncs."
            )
        )
    blocks.extend(_migration_overview_blocks(data))
    review = orm.BlockView(blocks=blocks)
    return {
        "response_action": "update",
        "view": review.as_ack_update(
            title_text="Confirm Import",
            callback_id=actions.CONFIG_DATA_MIGRATION_REVIEW,
            submit_button_text="Import",
            close_button_text="Cancel",
        ),
    }


_IMPORT_DONE_TEXT = ":white_check_mark: Import finished. Refresh Home to see Groups and Connections."
_IMPORT_FAIL_TEXT = ":warning: Import failed. Try Data Migration again."
_IMPORT_EXPIRED_TEXT = ":warning: That import expired. Open Data Migration and upload the file again."


def handle_data_migration_review_ack(body: dict, client: WebClient, context: dict) -> dict | None:
    """Keep the modal open while import runs."""
    return _wait_modal_ack(
        title="Confirm Import",
        callback_id=actions.CONFIG_DATA_MIGRATION_REVIEW,
        dm=True,
    )


def _notify_import_result(client: WebClient, body: dict, user_id: str | None, message: str) -> None:
    helpers.notify_user_dm(client, user_id, message)
    _close_modal_done(client, body, message)


def _complete_data_migration_import(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
    *,
    user_id: str | None,
) -> None:
    if not user_id:
        return
    from helpers._cache import _cache_get

    meta = _cache_get(_migration_import_cache_key(user_id))
    if not meta:
        log_warning("data_migration_import", user_id=user_id)
        _notify_import_result(client, body, user_id, _IMPORT_EXPIRED_TEXT)
        return
    data = meta.get("data")
    group_id = meta.get("group_id")
    workspace_id = meta.get("workspace_id")
    team_id_to_workspace_id = meta.get("team_id_to_workspace_id", {})
    if not data or group_id is None or not workspace_id:
        _notify_import_result(client, body, user_id, _IMPORT_EXPIRED_TEXT)
        return
    workspace_record = helpers.get_workspace_by_id(workspace_id)
    if not workspace_record:
        _notify_import_result(client, body, user_id, _IMPORT_FAIL_TEXT)
        return
    try:
        _ensure_migration_connection(data, workspace_record, context)
        _run_migration_import(
            data,
            workspace_record,
            team_id_to_workspace_id,
            group_id,
            client=client,
            acting_user_id=user_id,
            context=context,
        )
        builders.refresh_home_tab_for_workspace(
            workspace_record,
            logger,
            context=context,
            user_id=user_id,
        )
    except Exception:
        log_error("data_migration_import_failed", user_id=user_id, exc_info=True)
        _notify_import_result(client, body, user_id, _IMPORT_FAIL_TEXT)
        return
    _notify_import_result(client, body, user_id, _IMPORT_DONE_TEXT)


def handle_data_migration_review(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Connect (if needed) and import after the review modal is submitted."""
    user_id = helpers.get_user_id_from_body(body)
    if not user_id or not _is_admin(client, user_id, body):
        return
    _complete_data_migration_import(body, client, logger, context, user_id=user_id)


def handle_data_migration_proceed(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Proceed with import after user clicked the danger button despite warnings."""
    user_id = helpers.get_user_id_from_body(body)
    if not _is_admin(client, user_id, body):
        return
    _complete_data_migration_import(body, client, logger, context, user_id=user_id)
