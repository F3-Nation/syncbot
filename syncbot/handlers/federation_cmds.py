"""Federation command handlers — code generation, entry, and connection via Slack UI."""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from logging import Logger

from slack_sdk.web import WebClient

import builders
import federation
import helpers
from db import DbManager, schemas
from helpers.workspace import invalidate_fed_ws_for_sync_cache
from slack import actions, orm

_logger = logging.getLogger(__name__)


def _dm_actor(client: WebClient, body: dict, text: str) -> None:
    """DM the acting user. Best-effort; never raises."""
    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    if not user_id:
        return
    try:
        dm = client.conversations_open(users=[user_id])
        dm_channel = helpers.safe_get(dm, "channel", "id")
        if dm_channel:
            client.chat_postMessage(channel=dm_channel, text=text)
    except Exception as e:
        _logger.warning(f"Failed to DM federation notice: {e}")


def _require_primary_admin(
    body: dict,
    client: WebClient,
    context: dict,
    *,
    action: str,
) -> schemas.Workspace | None:
    """Return the workspace when the actor is a primary-workspace Slack admin."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = (
        helpers.safe_get(body, "view", "team_id")
        or helpers.safe_get(body, "team", "id")
        or helpers.safe_get(body, "team_id")
    )
    if not user_id or not team_id:
        _logger.warning("authorization_denied", extra={"user_id": user_id, "action": action})
        return None
    if not helpers.is_primary_workspace(team_id) or not helpers.is_workspace_admin(client, user_id):
        _logger.warning("authorization_denied", extra={"user_id": user_id, "action": action, "team_id": team_id})
        return None
    return helpers.get_workspace_record(team_id, body, context, client)


def _exchange_user_directory(
    fed_ws: schemas.FederatedWorkspace,
    workspace_record: schemas.Workspace,
) -> None:
    """Push our local user directory to a federated workspace and store theirs."""
    local_users = DbManager.find_records(
        schemas.UserDirectory,
        [schemas.UserDirectory.workspace_id == workspace_record.id],
    )
    users_payload = [
        {
            "user_id": u.slack_user_id,
            "email": u.email,
            "real_name": u.real_name,
            "display_name": u.display_name,
        }
        for u in local_users
    ]

    result = federation.push_users(
        fed_ws,
        {
            "users": users_payload,
            "workspace_id": workspace_record.id,
        },
    )

    if result and result.get("users"):
        remote_users = result["users"]
        now = datetime.now(UTC)
        for u in remote_users:
            remote_ws_id = u.get("workspace_id")
            if not remote_ws_id:
                continue
            existing = DbManager.find_records(
                schemas.UserDirectory,
                [
                    schemas.UserDirectory.workspace_id == remote_ws_id,
                    schemas.UserDirectory.slack_user_id == u.get("user_id", ""),
                ],
            )
            if existing:
                DbManager.update_records(
                    schemas.UserDirectory,
                    [schemas.UserDirectory.id == existing[0].id],
                    {
                        schemas.UserDirectory.email: u.get("email"),
                        schemas.UserDirectory.real_name: u.get("real_name"),
                        schemas.UserDirectory.display_name: u.get("display_name"),
                        schemas.UserDirectory.updated_at: now,
                    },
                )
            else:
                record = schemas.UserDirectory(
                    workspace_id=remote_ws_id,
                    slack_user_id=u.get("user_id", ""),
                    email=u.get("email"),
                    real_name=u.get("real_name"),
                    display_name=u.get("display_name"),
                    updated_at=now,
                )
                DbManager.create_record(record)

        _logger.info(
            "federation_user_exchange_complete",
            extra={"remote": fed_ws.instance_id, "sent": len(users_payload), "received": len(remote_users)},
        )


def handle_generate_federation_code(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open a modal asking for a label before generating the connection code."""
    if not helpers.federation_enabled():
        return
    if not _require_primary_admin(body, client, context, action="generate_federation_code"):
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    blocks = [
        orm.InputBlock(
            label="Name for this connection",
            action=actions.CONFIG_FEDERATION_LABEL_INPUT,
            element=orm.PlainTextInputElement(
                placeholder="e.g. East Coast SyncBot, Partner Org...",
            ),
            optional=False,
        ),
        orm.ContextBlock(
            element=orm.ContextElement(
                initial_value="Give this connection a friendly name so you can identify it later.",
            ),
        ),
    ]

    view = orm.BlockView(blocks=blocks)
    orm.open_or_push_view(
        client,
        trigger_id,
        {
            "type": "modal",
            "callback_id": actions.CONFIG_FEDERATION_LABEL_SUBMIT,
            "title": {"type": "plain_text", "text": "New Connection"},
            "submit": {"type": "plain_text", "text": "Generate Code"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": view.as_form_field(),
        },
        body=body,
    )


def handle_federation_label_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Generate the connection code after the admin provides a label."""
    if not helpers.federation_enabled():
        return

    workspace_record = _require_primary_admin(body, client, context, action="federation_label_submit")
    if not workspace_record:
        return

    public_url = federation.get_public_url(context)
    if not public_url:
        _logger.warning("federation_no_public_url")
        _dm_actor(
            client,
            body,
            ":warning: SyncBot does not know this instance's public URL yet. "
            "Open the Home tab (or wait for a Slack event), then generate the code again.",
        )
        return

    values = helpers.safe_get(body, "view", "state", "values") or {}
    label = ""
    for block_data in values.values():
        for action_id, action_data in block_data.items():
            if action_id == actions.CONFIG_FEDERATION_LABEL_INPUT:
                label = (action_data.get("value") or "").strip()

    try:
        encoded, raw_code = federation.generate_federation_code(
            workspace_record.id, label=label or None, context=context
        )
    except ValueError:
        _logger.warning("federation_no_public_url")
        _dm_actor(
            client,
            body,
            ":warning: SyncBot does not know this instance's public URL yet. "
            "Open the Home tab (or wait for a Slack event), then generate the code again.",
        )
        return

    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    if user_id:
        try:
            dm = client.conversations_open(users=[user_id])
            dm_channel = helpers.safe_get(dm, "channel", "id")
            if dm_channel:
                expires_ts = int((datetime.now(UTC) + timedelta(hours=24)).timestamp())
                client.chat_postMessage(
                    channel=dm_channel,
                    text=":globe_with_meridians: *Connection Code Generated*"
                    + (f" — _{label}_" if label else "")
                    + f"\n\nShare this code with the admin of the other SyncBot instance:\n\n```{encoded}```"
                    + f"\nThis code expires <!date^{expires_ts}^{{date_short_pretty}} at {{time}}|in 24 hours>.",
                )
        except Exception as e:
            _logger.warning(f"Failed to DM connection code: {e}")

    _logger.info(
        "federation_code_generated",
        extra={"workspace_id": workspace_record.id, "code": raw_code, "label": label},
    )

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)


def handle_enter_federation_code(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open a modal for the admin to paste a federation code."""
    if not helpers.federation_enabled():
        return
    if not _require_primary_admin(body, client, context, action="enter_federation_code"):
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    blocks = [
        orm.InputBlock(
            label="Paste the connection code from the remote SyncBot instance",
            action=actions.CONFIG_FEDERATION_CODE_INPUT,
            element=orm.PlainTextInputElement(
                placeholder="Paste the full code here...",
                multiline=True,
            ),
        ),
    ]

    view = orm.BlockView(blocks=blocks)
    orm.open_or_push_view(
        client,
        trigger_id,
        {
            "type": "modal",
            "callback_id": actions.CONFIG_FEDERATION_CODE_SUBMIT,
            "title": {"type": "plain_text", "text": "Enter Connection Code"},
            "submit": {"type": "plain_text", "text": "Connect"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": view.as_form_field(),
        },
        body=body,
    )


def handle_federation_code_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Process a submitted federation code and initiate cross-instance connection."""
    if not helpers.federation_enabled():
        return

    workspace_record = _require_primary_admin(body, client, context, action="federation_code_submit")
    if not workspace_record:
        return

    values = helpers.safe_get(body, "view", "state", "values") or {}
    code_text = ""
    for _block_id, block_data in values.items():
        for action_id, action_data in block_data.items():
            if action_id == actions.CONFIG_FEDERATION_CODE_INPUT:
                code_text = (action_data.get("value") or "").strip()

    if not code_text:
        _logger.warning("federation_code_submit: empty code")
        _dm_actor(client, body, ":warning: Paste the full connection code from the other SyncBot instance.")
        return

    payload = federation.parse_federation_code(code_text)
    if not payload:
        _logger.warning("federation_code_submit: invalid code format")
        _dm_actor(
            client,
            body,
            ":warning: That connection code is invalid or was tampered with. Ask the other admin to generate a new one.",
        )
        return

    remote_url = payload["webhook_url"]
    remote_code = payload["code"]
    remote_instance_id = payload["instance_id"]

    result = federation.initiate_federation_connect(
        remote_url,
        remote_code,
        team_id=workspace_record.team_id,
        workspace_name=workspace_record.workspace_name or None,
        context=context,
    )
    if not result or not result.get("ok"):
        _logger.error(
            "federation_connect_failed",
            extra={"remote_url": remote_url, "result": result},
        )
        _dm_actor(
            client,
            body,
            ":warning: Could not connect to the other SyncBot instance. "
            "Check that federation is enabled there and try again.",
        )
        return

    remote_public_key = result.get("public_key", "")

    fed_ws = federation.get_or_create_federated_workspace(
        instance_id=remote_instance_id,
        webhook_url=remote_url,
        public_key=remote_public_key,
        name=f"Connection {remote_instance_id[:8]}",
    )

    now = datetime.now(UTC)
    group = schemas.WorkspaceGroup(
        name=f"Federation — {fed_ws.name}",
        invite_code=f"FED-{secrets.token_hex(4).upper()}",
        status="active",
        created_at=now,
    )
    DbManager.create_record(group)

    local_member = schemas.WorkspaceGroupMember(
        group_id=group.id,
        workspace_id=workspace_record.id,
        status="active",
        role="owner",
        joined_at=now,
    )
    DbManager.create_record(local_member)

    fed_member = schemas.WorkspaceGroupMember(
        group_id=group.id,
        federated_workspace_id=fed_ws.id,
        status="active",
        role="member",
        joined_at=now,
    )
    DbManager.create_record(fed_member)

    invalidate_fed_ws_for_sync_cache()

    _logger.info(
        "federation_connection_established",
        extra={
            "workspace_id": workspace_record.id,
            "remote_instance": remote_instance_id,
            "federated_workspace_id": fed_ws.id,
            "group_id": group.id,
        },
    )

    _exchange_user_directory(fed_ws, workspace_record)

    acting_user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=acting_user_id)


def handle_remove_federation_connection(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Remove a federation connection (group membership)."""
    workspace_record = _require_primary_admin(body, client, context, action="remove_federation_connection")
    if not workspace_record:
        return

    action_data = helpers.safe_get(body, "actions", 0) or {}
    action_id: str = action_data.get("action_id", "")
    member_id_str = action_id.replace(f"{actions.CONFIG_REMOVE_FEDERATION_CONNECTION}_", "")

    try:
        member_id = int(member_id_str)
    except (TypeError, ValueError):
        _logger.warning("remove_federation_connection_invalid_id", extra={"action_id": action_id})
        return

    member = DbManager.get_record(schemas.WorkspaceGroupMember, id=member_id)
    if not member:
        return

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [schemas.WorkspaceGroupMember.id == member_id],
        {
            schemas.WorkspaceGroupMember.status: "inactive",
            schemas.WorkspaceGroupMember.deleted_at: now,
        },
    )

    invalidate_fed_ws_for_sync_cache()

    _logger.info("federation_connection_removed", extra={"member_id": member_id})

    team_id = helpers.safe_get(body, "team", "id") or helpers.safe_get(body, "view", "team_id")
    workspace_record = helpers.get_workspace_record(team_id, body, context, client) if team_id else None
    if workspace_record:
        acting_user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
        builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=acting_user_id)
