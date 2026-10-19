"""Federation command handlers — Create / Join / Edit / Leave External Connection."""

import json
from datetime import UTC, datetime, timedelta
from logging import Logger

from slack_sdk.web import WebClient

import builders
import federation
import helpers
from db import DbManager, schemas
from handlers._common import _close_modal_done, _parse_private_metadata, _wait_modal_ack
from helpers.workspace import invalidate_fed_ws_for_sync_cache, replace_federation_allowlist
from logger import log_error, log_info, log_warning
from slack import actions, orm
from slack.blocks import section


def _dm_actor(client: WebClient, body: dict, text: str) -> None:
    """DM the acting user. Best-effort; never raises."""
    user_id = helpers.get_user_id_from_body(body)
    if not user_id:
        return
    try:
        dm = client.conversations_open(users=[user_id])
        dm_channel = helpers.safe_get(dm, "channel", "id")
        if dm_channel:
            client.chat_postMessage(channel=dm_channel, text=text)
    except Exception as e:
        log_warning("failed_to_dm_federation_notice", error=str(e))


def _require_primary_admin(
    body: dict,
    client: WebClient,
    context: dict,
    *,
    action: str,
) -> schemas.Workspace | None:
    """Return the workspace when the actor is a primary-workspace Slack admin."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id:
        log_warning("authorization_denied", user_id=user_id, action=action)
        return None
    if not helpers.is_primary_workspace(team_id) or not helpers.is_workspace_admin(client, user_id):
        log_warning("authorization_denied", user_id=user_id, action=action, team_id=team_id)
        return None
    return helpers.get_workspace_record(team_id, body, context, client)


def _exchange_user_directory(
    fed_ws: schemas.Instance,
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
            "team_id": workspace_record.team_id,
            "workspace_name": workspace_record.workspace_name,
        },
    )

    if result and result.get("users"):
        remote_users = result["users"]
        now = datetime.now(UTC)
        for u in remote_users:
            remote_team_id = u.get("team_id")
            if not (isinstance(remote_team_id, str) and remote_team_id.strip()):
                continue
            stubs = DbManager.find_records(
                schemas.Workspace,
                [
                    schemas.Workspace.team_id == remote_team_id.strip(),
                    schemas.Workspace.instance_id == fed_ws.instance_id,
                    schemas.Workspace.deleted_at.is_(None),
                ],
            )
            if not stubs:
                continue
            remote_ws_id = stubs[0].id
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

        log_info(
            "federation_user_exchange_complete",
            remote=fed_ws.instance_id,
            sent=len(users_payload),
            received=len(remote_users),
        )


def _view_values(body: dict) -> dict:
    return helpers.safe_get(body, "view", "state", "values") or {}


def _input_text(values: dict, action_id: str) -> str:
    for block_data in values.values():
        if action_id in block_data:
            return (block_data[action_id].get("value") or "").strip()
    return ""


def _multi_select_values(values: dict, action_id: str) -> list[str]:
    for block_data in values.values():
        if action_id in block_data:
            options = block_data[action_id].get("selected_options") or []
            return [str(option.get("value")) for option in options if option.get("value")]
    return []


def _local_workspace_options(*, exclude_ids: list[int] | None = None) -> list[orm.SelectorOption]:
    """Installed local workspaces, keyed by integer workspace id."""
    from helpers.workspace_kind import is_local_workspace

    skipped = {int(wid) for wid in (exclude_ids or []) if wid}
    options: list[orm.SelectorOption] = []
    for workspace in DbManager.find_records(schemas.Workspace, [schemas.Workspace.deleted_at.is_(None)]):
        if not is_local_workspace(workspace) or workspace.id in skipped:
            continue
        options.append(
            orm.SelectorOption(
                name=helpers.resolve_workspace_name(workspace) or workspace.team_id,
                value=str(workspace.id),
            )
        )
    return sorted(options, key=lambda option: option.name.lower())


def _parse_workspace_ids(raw_ids: list[str]) -> list[int]:
    parsed: list[int] = []
    for raw in raw_ids:
        try:
            parsed.append(int(raw))
        except (TypeError, ValueError):
            continue
    return parsed


def _ticked_names(names: list[str]) -> str:
    cleaned = sorted({name for name in names if name})
    if not cleaned:
        return "`None yet`"
    return ", ".join(f"`{name}`" for name in cleaned)


def _allowlist_field_error(action_id: str) -> dict:
    return {
        "response_action": "errors",
        "errors": {action_id: "Select at least one Workspace."},
    }


def _connection_name_block(*, action: str, initial: str | None = None) -> list:
    return [
        orm.InputBlock(
            label="Name for this connection",
            action=action,
            element=orm.PlainTextInputElement(
                placeholder="e.g. Partner Org, Regional HQ...",
                initial_value=(initial or "").strip()[:200] or None,
                max_length=200,
            ),
            optional=False,
        ),
        orm.ContextBlock(
            element=orm.ContextElement(
                initial_value="Give this connection a friendly name so you can identify it later.",
            ),
        ),
    ]


def _instance_id_from_action(body: dict, prefix: str) -> str:
    action_data = helpers.safe_get(body, "actions", 0) or {}
    action_id = str(action_data.get("action_id") or "")
    if action_id.startswith(prefix + "_"):
        return action_id[len(prefix) + 1 :].strip()
    value = action_data.get("value")
    return str(value).strip() if value else ""


def _peer_or_none(instance_id: str) -> schemas.Instance | None:
    instance_id = (instance_id or "").strip()
    if not instance_id:
        return None
    peer = DbManager.get_record(schemas.Instance, id=instance_id)
    if not peer or getattr(peer, "private_key_encrypted", None):
        return None
    return peer


def _drop_excluded_workspace_ids(workspace_ids: list[int], exclude_id) -> list[int]:
    try:
        skipped = int(exclude_id) if exclude_id is not None else None
    except (TypeError, ValueError):
        skipped = None
    if skipped is None:
        return workspace_ids
    return [wid for wid in workspace_ids if wid != skipped]


def _workspace_id_for_team(team_id: str | None) -> int | None:
    team_id = (team_id or "").strip()
    if not team_id:
        return None
    workspace = DbManager.get_record(schemas.Workspace, id=team_id)
    if not workspace or workspace.deleted_at is not None:
        return None
    return workspace.id


def _workspaces_block(
    *,
    action: str,
    initial_ids: list[str] | None = None,
    optional: bool = False,
    exclude_ids: list[int] | None = None,
    show_owner_note: bool = False,
) -> list:
    options = _local_workspace_options(exclude_ids=exclude_ids)
    if not options:
        return [
            section("No other Workspaces on this instance to allow on this connection."),
        ]
    note = "Select Workspaces on this instance that the other SyncBot may see."
    if show_owner_note:
        note += " A Workspace that owns a group with members on this connection has to Give Up Ownership first."
    return [
        orm.InputBlock(
            label="Workspaces allowed on this connection",
            action=action,
            element=orm.MultiStaticSelectElement(
                placeholder="Select Workspaces on this instance",
                initial_values=initial_ids or [],
                options=options,
            ),
            optional=optional,
        ),
        orm.ContextBlock(
            element=orm.ContextElement(
                initial_value=note,
            ),
        ),
    ]


def _connection_detail_blocks(payload: dict, *, remote_names: list[str] | None = None) -> list:
    label = (payload.get("label") or "").strip() or "Unnamed connection"
    webhook = payload.get("webhook_url") or ""
    fingerprint = payload.get("instance_id") or ""
    primary_name = (payload.get("primary_workspace_name") or "").strip()
    primary_team = (payload.get("primary_team_id") or "").strip()
    lines = [f":globe_with_meridians: {webhook}"]
    if primary_name:
        lines.append(f"Primary Workspace: `{primary_name}`")
    if primary_team:
        lines.append(f"Team ID: `{primary_team}`")
    if remote_names is not None:
        lines.append(f"Remote Workspaces: {_ticked_names(remote_names)}")
    lines.append(f"Fingerprint: `{fingerprint}`")
    return [
        section(f"*{label}*"),
        orm.ContextBlock(
            element=orm.ContextElement(initial_value="\n".join(lines)),
        ),
    ]


def _pairing_created_naive(pairing) -> datetime | None:
    created = getattr(pairing, "created_at", None)
    if created is None:
        return None
    if getattr(created, "tzinfo", None):
        return created.replace(tzinfo=None)
    return created


def _operator_pairing_unexpired(pairing, *, now: datetime | None = None) -> bool:
    created = _pairing_created_naive(pairing)
    if created is None:
        return True
    current = now or datetime.now(UTC).replace(tzinfo=None)
    return (current - created).total_seconds() <= 24 * 3600


def _allowed_workspace_ids(pairing) -> list[int]:
    raw = getattr(pairing, "allowed_workspace_ids", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return _parse_workspace_ids([str(item) for item in parsed])


def _connection_code_blocks(label: str, encoded: str, created_at: datetime | None) -> list:
    created = created_at or datetime.now(UTC).replace(tzinfo=None)
    if getattr(created, "tzinfo", None):
        created = created.replace(tzinfo=None)
    expires_ts = int((created + timedelta(hours=24)).timestamp())
    return [
        section(f":globe_with_meridians: *{label}*"),
        section(f"Share this code with the other SyncBot admin:\n\n```{encoded}```"),
        orm.ContextBlock(
            element=orm.ContextElement(
                initial_value=(
                    f"This code expires <!date^{expires_ts}^{{date_short_pretty}} at {{time}}|in 24 hours>."
                ),
            ),
        ),
    ]


def _update_connection_code_modal(client: WebClient, body: dict, blocks: list) -> None:
    view_id = helpers.safe_get(body, "view", "id")
    if not view_id:
        return
    orm.BlockView(blocks=blocks).update_modal(
        client=client,
        view_id=view_id,
        title_text="Create Connection",
        callback_id=actions.CONFIG_CREATE_EXTERNAL_CONNECTION_SUBMIT,
        submit_button_text=None,
        close_button_text="Close",
    )


def _reencode_pairing_code(pairing, context: dict | None) -> str | None:
    try:
        endpoint = federation.federation_endpoint_url(context)
        if not endpoint:
            return None
        instance_id = federation.get_instance_id()
        _, public_key_pem = federation.get_or_create_instance_keypair()
        return federation.encode_federation_connection_blob(
            endpoint,
            instance_id,
            public_key_pem,
            pairing.code,
            label=(getattr(pairing, "label", None) or "External connection")[:200],
            primary_team_id=federation.this_primary_team_id(),
            primary_workspace_name=federation.this_primary_workspace_name(),
        )
    except Exception:
        log_warning("federation_reencode_failed", exc_info=True)
        return None


def open_create_external_connection_modal(
    body: dict,
    client: WebClient,
    *,
    request_id: int | None = None,
    exclude_workspace_id: int | None = None,
    initial_name: str | None = None,
    leaving_workspace_name: str | None = None,
) -> None:
    """Open the Create Connection modal, optionally for an approved migration request."""
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return
    exclude_ids = [exclude_workspace_id] if exclude_workspace_id else None
    blocks = []
    if request_id and leaving_workspace_name:
        blocks.append(
            section(f"`{leaving_workspace_name}` is leaving this instance, so it is not in the Workspace list.")
        )
    blocks.extend(_connection_name_block(action=actions.CONFIG_CREATE_EXTERNAL_CONNECTION_NAME, initial=initial_name))
    blocks.extend(_workspaces_block(action=actions.CONFIG_CREATE_EXTERNAL_WORKSPACES, exclude_ids=exclude_ids))
    metadata = {}
    if request_id:
        metadata["request_id"] = request_id
    if exclude_workspace_id:
        metadata["exclude_workspace_id"] = exclude_workspace_id
        if not _local_workspace_options(exclude_ids=exclude_ids):
            metadata["allow_empty_allowlist"] = True
    orm.BlockView(blocks=blocks).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_CREATE_EXTERNAL_CONNECTION_SUBMIT,
        title_text="Create Connection",
        submit_button_text="Create",
        close_button_text="Cancel",
        parent_metadata=metadata or None,
        body=body,
    )


def handle_create_external_connection(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the Create External Connection modal."""
    if not helpers.federation_enabled():
        return
    if not _require_primary_admin(body, client, context, action="create_external_connection"):
        return
    open_create_external_connection_modal(body, client)


def handle_create_external_connection_submit_ack(body: dict, client: WebClient, context: dict) -> dict | None:
    """Ack-phase validation for Create External Connection."""
    if not helpers.federation_enabled():
        return None
    values = _view_values(body)
    label = _input_text(values, actions.CONFIG_CREATE_EXTERNAL_CONNECTION_NAME)
    if not label:
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_CREATE_EXTERNAL_CONNECTION_NAME: "Enter a name for this connection."},
        }
    meta = _parse_private_metadata(body)
    workspace_ids = _drop_excluded_workspace_ids(
        _parse_workspace_ids(_multi_select_values(values, actions.CONFIG_CREATE_EXTERNAL_WORKSPACES)),
        meta.get("exclude_workspace_id"),
    )
    if not workspace_ids and not meta.get("allow_empty_allowlist"):
        return _allowlist_field_error(actions.CONFIG_CREATE_EXTERNAL_WORKSPACES)
    payload = {"label": label, "workspace_ids": workspace_ids}
    if meta.get("request_id"):
        payload["request_id"] = meta["request_id"]
    if meta.get("exclude_workspace_id") is not None:
        payload["exclude_workspace_id"] = meta["exclude_workspace_id"]
    if meta.get("allow_empty_allowlist"):
        payload["allow_empty_allowlist"] = True
    return _wait_modal_ack(
        title="Create Connection",
        callback_id=actions.CONFIG_CREATE_EXTERNAL_CONNECTION_SUBMIT,
        dm=True,
        private_metadata=json.dumps(payload),
    )


def handle_create_external_connection_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Generate the connection code after Create External Connection."""
    if not helpers.federation_enabled():
        return

    workspace_record = _require_primary_admin(body, client, context, action="create_external_connection_submit")
    if not workspace_record:
        return

    meta = _parse_private_metadata(body)
    values = _view_values(body)
    label = str(meta.get("label") or "").strip() or _input_text(values, actions.CONFIG_CREATE_EXTERNAL_CONNECTION_NAME)
    raw_ids = meta.get("workspace_ids") or []
    workspace_ids = _drop_excluded_workspace_ids(
        _parse_workspace_ids([str(item) for item in raw_ids])
        or _parse_workspace_ids(_multi_select_values(values, actions.CONFIG_CREATE_EXTERNAL_WORKSPACES)),
        meta.get("exclude_workspace_id"),
    )
    if not label:
        return
    if not workspace_ids and not meta.get("allow_empty_allowlist"):
        return

    request = None
    request_id = meta.get("request_id")
    if request_id:
        try:
            request = DbManager.get_record(schemas.FederationPairingRequest, id=int(request_id))
        except (TypeError, ValueError):
            request = None
        if request is None or request.status != "pending":
            _update_connection_code_modal(
                client,
                body,
                [section(":warning: This request was already approved or declined.")],
            )
            return

    missing_url = (
        ":warning: SyncBot does not know this instance's public URL yet. "
        "Open the Home tab (or wait for a Slack event), then create the connection again."
    )
    public_url = federation.get_public_url(context)
    if not public_url:
        log_warning("federation_no_public_url")
        _dm_actor(client, body, missing_url)
        _update_connection_code_modal(client, body, [section(missing_url)])
        return

    try:
        encoded, raw_code = federation.generate_federation_code(
            label=label,
            subject_team_id=request.subject_team_id if request else None,
            context=context,
            workspace_ids=workspace_ids,
        )
    except ValueError:
        log_warning("federation_no_public_url")
        _dm_actor(client, body, missing_url)
        _update_connection_code_modal(client, body, [section(missing_url)])
        return

    created_at = datetime.now(UTC).replace(tzinfo=None)
    _update_connection_code_modal(client, body, _connection_code_blocks(label, encoded, created_at))

    user_id = helpers.get_user_id_from_body(body)
    if user_id:
        try:
            dm = client.conversations_open(users=[user_id])
            dm_channel = helpers.safe_get(dm, "channel", "id")
            if dm_channel:
                expires_ts = int((created_at + timedelta(hours=24)).timestamp())
                client.chat_postMessage(
                    channel=dm_channel,
                    text=":globe_with_meridians: External Connection created"
                    + (f" — `{label}`" if label else "")
                    + f"\n\nShare this code with the admin of the other SyncBot instance:\n\n```{encoded}```"
                    + f"\nThis code expires <!date^{expires_ts}^{{date_short_pretty}} at {{time}}|in 24 hours>.",
                )
        except Exception as e:
            log_warning("failed_to_dm_connection_code", error=str(e))

    if request:
        from handlers.export_import import fulfill_pairing_request

        try:
            fulfill_pairing_request(
                request,
                encoded=encoded,
                raw_code=raw_code,
                resolved_by_user_id=user_id or "",
            )
        except Exception:
            log_error("pairing_request_fulfill_failed", request_id=request.id, exc_info=True)

    log_info("federation_code_generated", workspace_id=workspace_record.id, label=label)

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)


def handle_show_external_connection_code(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Re-show unexpired Create External Connection codes."""
    if not helpers.federation_enabled():
        return
    if not _require_primary_admin(body, client, context, action="show_external_connection_code"):
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    raw_id = _instance_id_from_action(body, actions.CONFIG_SHOW_EXTERNAL_CONNECTION_CODE)
    try:
        pairing_id = int(raw_id)
    except (TypeError, ValueError):
        pairing_id = 0
    pairing = DbManager.get_record(schemas.FederationPairingCode, id=pairing_id) if pairing_id else None
    if pairing is None or not _operator_pairing_unexpired(pairing):
        blocks = [
            section(
                "This connection code is no longer available. "
                "Codes expire after 24 hours or when the other admin joins."
            )
        ]
    else:
        encoded = _reencode_pairing_code(pairing, context)
        if not encoded:
            blocks = [
                section(
                    f":warning: *{pairing.label or 'External connection'}*\n"
                    "SyncBot does not know this instance's public URL yet. Open Home and try again."
                )
            ]
        else:
            blocks = _connection_code_blocks(
                pairing.label or "External connection",
                encoded,
                _pairing_created_naive(pairing),
            )
    orm.BlockView(blocks=blocks).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_SHOW_EXTERNAL_CONNECTION_CODE,
        title_text="Connection Code",
        submit_button_text=None,
        close_button_text="Close",
        body=body,
    )


def handle_join_external_connection(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the Join External Connection modal (paste the connection code)."""
    if not helpers.federation_enabled():
        return
    if not _require_primary_admin(body, client, context, action="join_external_connection"):
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    blocks = [
        orm.InputBlock(
            label="Paste the connection code from the remote SyncBot instance",
            action=actions.CONFIG_JOIN_EXTERNAL_CONNECTION_CODE,
            element=orm.PlainTextInputElement(
                placeholder="Paste the full code here...",
                multiline=True,
            ),
            optional=False,
        ),
    ]
    view = orm.BlockView(blocks=blocks)
    view.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_JOIN_EXTERNAL_CONNECTION_SUBMIT,
        title_text="Join Connection",
        submit_button_text="Continue",
        close_button_text="Cancel",
        body=body,
    )


def handle_join_external_connection_submit_ack(body: dict, client: WebClient, context: dict) -> dict | None:
    """Parse the pasted code and update the modal to the review screen."""
    if not helpers.federation_enabled():
        return None
    code_text = _input_text(_view_values(body), actions.CONFIG_JOIN_EXTERNAL_CONNECTION_CODE)
    if not code_text:
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_JOIN_EXTERNAL_CONNECTION_CODE: "Paste the full connection code."},
        }
    payload = federation.parse_federation_code(code_text)
    if not payload:
        return {
            "response_action": "errors",
            "errors": {
                actions.CONFIG_JOIN_EXTERNAL_CONNECTION_CODE: (
                    "That connection code is invalid or was tampered with. Ask the other admin for a new one."
                )
            },
        }
    review = orm.BlockView(
        blocks=[
            *_connection_detail_blocks(payload),
            *_workspaces_block(action=actions.CONFIG_JOIN_EXTERNAL_WORKSPACES),
        ]
    )
    return {
        "response_action": "update",
        "view": review.as_ack_update(
            title_text="Join Connection",
            callback_id=actions.CONFIG_JOIN_EXTERNAL_CONNECTION_REVIEW,
            submit_button_text="Join",
            close_button_text="Cancel",
            parent_metadata={"code": code_text},
        ),
    }


def handle_join_external_connection_review_ack(body: dict, client: WebClient, context: dict) -> dict | None:
    """Ack-phase validation for Join External Connection review."""
    if not helpers.federation_enabled():
        return None
    workspace_ids = _parse_workspace_ids(
        _multi_select_values(_view_values(body), actions.CONFIG_JOIN_EXTERNAL_WORKSPACES)
    )
    if not workspace_ids:
        return _allowlist_field_error(actions.CONFIG_JOIN_EXTERNAL_WORKSPACES)
    raw_meta = helpers.safe_get(body, "view", "private_metadata")
    return _wait_modal_ack(
        title="Join Connection",
        callback_id=actions.CONFIG_JOIN_EXTERNAL_CONNECTION_REVIEW,
        dm=False,
        private_metadata=raw_meta if isinstance(raw_meta, str) else None,
    )


def handle_join_external_connection_review(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Connect after the admin reviews the code and picks allowed Workspaces."""
    if not helpers.federation_enabled():
        return

    workspace_record = _require_primary_admin(body, client, context, action="join_external_connection_review")
    if not workspace_record:
        return

    meta = _parse_private_metadata(body)
    code_text = str(meta.get("code") or "").strip()
    workspace_ids = _parse_workspace_ids(
        _multi_select_values(_view_values(body), actions.CONFIG_JOIN_EXTERNAL_WORKSPACES)
    )
    if not code_text or not workspace_ids:
        return

    payload = federation.parse_federation_code(code_text)
    if not payload:
        invalid_code = (
            ":warning: That connection code is invalid or was tampered with. Ask the other admin to create a new one."
        )
        _dm_actor(client, body, invalid_code)
        _close_modal_done(client, body, invalid_code)
        return

    remote_url = payload["webhook_url"]
    remote_code = payload["code"]
    remote_instance_id = payload["instance_id"]
    remote_name = (payload.get("label") or "").strip() or f"Connection {remote_instance_id[:8]}"
    primary_name = (payload.get("primary_workspace_name") or "").strip() or None
    primary_team = (payload.get("primary_team_id") or "").strip() or None

    result = federation.initiate_federation_connect(
        remote_url,
        remote_code,
        team_id=workspace_record.team_id,
        workspace_name=workspace_record.workspace_name or None,
        context=context,
    )
    if not result or not result.get("ok"):
        log_error("federation_connect_failed", remote_url=remote_url, result=result)
        connect_failed = (
            f":warning: Could not connect to `{remote_name}`. Check that federation is enabled there and try again."
        )
        _dm_actor(client, body, connect_failed)
        _close_modal_done(client, body, connect_failed)
        return

    remote_public_key = result.get("public_key", "")
    remote_team_id = result.get("team_id") if isinstance(result.get("team_id"), str) else None
    remote_workspace_name = result.get("workspace_name") if isinstance(result.get("workspace_name"), str) else None
    if remote_team_id:
        remote_team_id = remote_team_id.strip() or None

    fed_ws = federation.get_or_create_instance(
        instance_id=remote_instance_id,
        webhook_url=remote_url,
        public_key=remote_public_key,
        name=remote_name,
        primary_team_id=primary_team or remote_team_id,
        primary_workspace_name=primary_name or remote_workspace_name,
    )

    if remote_team_id:
        from helpers.workspace_kind import ensure_stub_workspace

        ensure_stub_workspace(
            team_id=remote_team_id,
            workspace_name=remote_workspace_name,
            instance_id=fed_ws.instance_id,
        )

    replace_federation_allowlist(fed_ws.instance_id, workspace_ids)

    invalidate_fed_ws_for_sync_cache()
    federation.push_allowed_workspaces(fed_ws)
    try:
        from federation.replicate import replicate_peer_snapshot

        replicate_peer_snapshot(fed_ws)
    except Exception:
        log_error("federation_snapshot", peer_instance_id=fed_ws.instance_id, ok=False)

    log_info(
        "federation_connection_established",
        workspace_id=workspace_record.id,
        remote_instance=remote_instance_id,
        instance_id=fed_ws.instance_id,
    )

    _exchange_user_directory(fed_ws, workspace_record)

    acting_user_id = helpers.get_user_id_from_body(body)
    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=acting_user_id)
    _close_modal_done(client, body, ":white_check_mark: Connection finished.")


def handle_edit_external_connection(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open Edit Connection to rename this connection and change the local Workspaces allowed on it."""
    if not helpers.federation_enabled():
        return
    if not _require_primary_admin(body, client, context, action="edit_external_connection"):
        return

    peer = _peer_or_none(_instance_id_from_action(body, actions.CONFIG_EDIT_EXTERNAL_CONNECTION))
    if not peer:
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    current = DbManager.find_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == peer.instance_id],
    )
    initial_ids = [str(row.workspace_id) for row in current if row.workspace_id]
    view = orm.BlockView(
        blocks=[
            *_connection_name_block(
                action=actions.CONFIG_EDIT_EXTERNAL_CONNECTION_NAME,
                initial=peer.name,
            ),
            *_workspaces_block(
                action=actions.CONFIG_EDIT_EXTERNAL_WORKSPACES,
                initial_ids=initial_ids,
                show_owner_note=True,
            ),
        ]
    )
    view.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_EDIT_EXTERNAL_CONNECTION_SUBMIT,
        title_text="Edit Connection",
        submit_button_text="Save",
        close_button_text="Cancel",
        parent_metadata={"instance_id": peer.instance_id},
        body=body,
    )


def handle_edit_external_connection_submit_ack(body: dict, client: WebClient, context: dict) -> dict | None:
    """Ack-phase validation for Edit Connection."""
    if not helpers.federation_enabled():
        return None
    values = _view_values(body)
    if not _input_text(values, actions.CONFIG_EDIT_EXTERNAL_CONNECTION_NAME):
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_EDIT_EXTERNAL_CONNECTION_NAME: "Enter a name for this connection."},
        }
    workspace_ids = _parse_workspace_ids(_multi_select_values(values, actions.CONFIG_EDIT_EXTERNAL_WORKSPACES))
    if not workspace_ids:
        return _allowlist_field_error(actions.CONFIG_EDIT_EXTERNAL_WORKSPACES)
    meta = _parse_private_metadata(body)
    if meta.get("pairing_id"):
        return None
    instance_id = str(meta.get("instance_id") or "").strip()
    if not instance_id:
        return None
    current = DbManager.find_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == instance_id],
    )
    current_ids = [row.workspace_id for row in current if row.workspace_id]
    dropped = [wid for wid in current_ids if wid not in workspace_ids]
    blocked = helpers.get_owner_ids_blocking_allowlist_drop(instance_id, dropped)
    if not blocked:
        return None
    names: list[str] = []
    for workspace_id in blocked:
        workspace = helpers.get_workspace_by_id(workspace_id)
        if workspace:
            names.append(workspace.workspace_name or workspace.team_id or str(workspace_id))
        else:
            names.append(str(workspace_id))
    named = ", ".join(name for name in names if name) or "that Workspace"
    return {
        "response_action": "errors",
        "errors": {
            actions.CONFIG_EDIT_EXTERNAL_WORKSPACES: (
                f"{named} owns a group with members on this connection. Give Up Ownership first."
            ),
        },
    }


def handle_edit_external_connection_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Save the connection name and local workspaces allowed on this connection or waiting code."""
    if not helpers.federation_enabled():
        return
    workspace_record = _require_primary_admin(body, client, context, action="edit_external_connection_submit")
    if not workspace_record:
        return
    meta = _parse_private_metadata(body)
    workspace_ids = _drop_excluded_workspace_ids(
        _parse_workspace_ids(_multi_select_values(_view_values(body), actions.CONFIG_EDIT_EXTERNAL_WORKSPACES)),
        meta.get("exclude_workspace_id"),
    )
    label = _input_text(_view_values(body), actions.CONFIG_EDIT_EXTERNAL_CONNECTION_NAME)[:200]
    if not workspace_ids or not label:
        return
    pairing_id = meta.get("pairing_id")
    if pairing_id:
        try:
            pairing = DbManager.get_record(schemas.FederationPairingCode, id=int(pairing_id))
        except (TypeError, ValueError):
            return
        if pairing is None or not _operator_pairing_unexpired(pairing):
            return
        DbManager.update_records(
            schemas.FederationPairingCode,
            [schemas.FederationPairingCode.id == pairing.id],
            {
                schemas.FederationPairingCode.label: label,
                schemas.FederationPairingCode.allowed_workspace_ids: json.dumps(sorted(set(workspace_ids))),
            },
        )
        log_info("federation_pending_connection_edited", pairing_id=pairing.id)
    else:
        peer = _peer_or_none(str(meta.get("instance_id") or ""))
        if not peer:
            return
        now = datetime.now(UTC).replace(tzinfo=None)
        DbManager.update_records(
            schemas.Instance,
            [schemas.Instance.instance_id == peer.instance_id],
            {schemas.Instance.name: label, schemas.Instance.updated_at: now},
        )
        replace_federation_allowlist(peer.instance_id, workspace_ids)
        invalidate_fed_ws_for_sync_cache()
        federation.push_allowed_workspaces(peer)
        try:
            from federation.replicate import replicate_peer_snapshot

            replicate_peer_snapshot(peer)
        except Exception:
            log_error("federation_snapshot", peer_instance_id=peer.instance_id, ok=False)
        log_info("federation_connection_edited", instance_id=peer.instance_id)
    acting_user_id = helpers.get_user_id_from_body(body)
    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=acting_user_id)


def _pairing_from_action(body: dict, prefix: str):
    raw_id = _instance_id_from_action(body, prefix)
    try:
        pairing_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    pairing = DbManager.get_record(schemas.FederationPairingCode, id=pairing_id)
    if pairing is None or not _operator_pairing_unexpired(pairing):
        return None
    return pairing


def handle_edit_pending_external_connection(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open Edit Connection for a waiting pairing code's name and local Workspaces."""
    if not helpers.federation_enabled():
        return
    if not _require_primary_admin(body, client, context, action="edit_pending_external_connection"):
        return
    pairing = _pairing_from_action(body, actions.CONFIG_EDIT_PENDING_EXTERNAL_CONNECTION)
    if not pairing:
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return
    exclude_id = _workspace_id_for_team(getattr(pairing, "subject_team_id", None))
    exclude_ids = [exclude_id] if exclude_id else None
    initial_ids = [str(wid) for wid in _allowed_workspace_ids(pairing)]
    metadata = {"pairing_id": pairing.id}
    if exclude_id:
        metadata["exclude_workspace_id"] = exclude_id
    view = orm.BlockView(
        blocks=[
            *_connection_name_block(
                action=actions.CONFIG_EDIT_EXTERNAL_CONNECTION_NAME,
                initial=pairing.label,
            ),
            *_workspaces_block(
                action=actions.CONFIG_EDIT_EXTERNAL_WORKSPACES,
                initial_ids=initial_ids,
                exclude_ids=exclude_ids,
            ),
        ]
    )
    view.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_EDIT_EXTERNAL_CONNECTION_SUBMIT,
        title_text="Edit Connection",
        submit_button_text="Save",
        close_button_text="Cancel",
        parent_metadata=metadata,
        body=body,
    )


def handle_cancel_pending_external_connection(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Confirm cancelling a waiting connection code."""
    if not _require_primary_admin(body, client, context, action="cancel_pending_external_connection"):
        return
    pairing = _pairing_from_action(body, actions.CONFIG_CANCEL_PENDING_EXTERNAL_CONNECTION)
    if not pairing:
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return
    name = pairing.label or "this External Connection"
    confirm = orm.BlockView(
        blocks=[
            section(
                f":warning: *Cancel {name}?*\n\n"
                "The connection code will stop working. You can Create External Connection again later."
            ),
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=":wastebasket: Cancel Connection",
                        action=actions.CONFIG_CANCEL_PENDING_EXTERNAL_CONNECTION_CONFIRM,
                        value=str(pairing.id),
                        style="danger",
                    ),
                ]
            ),
        ]
    )
    confirm.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_CANCEL_PENDING_EXTERNAL_CONNECTION_CONFIRM,
        title_text="Cancel Connection",
        submit_button_text=None,
        close_button_text="Keep",
        parent_metadata={"pairing_id": pairing.id},
        body=body,
    )


def handle_cancel_pending_external_connection_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Delete a waiting pairing code."""
    workspace_record = _require_primary_admin(
        body, client, context, action="cancel_pending_external_connection_confirm"
    )
    if not workspace_record:
        return
    meta = _parse_private_metadata(body)
    raw_id = meta.get("pairing_id")
    if not raw_id:
        raw_id = (helpers.safe_get(body, "actions", 0) or {}).get("value")
    try:
        pairing_id = int(raw_id)
    except (TypeError, ValueError):
        return
    pairing = DbManager.get_record(schemas.FederationPairingCode, id=pairing_id)
    if pairing is None or not _operator_pairing_unexpired(pairing):
        return
    name = pairing.label or "this External Connection"
    try:
        federation.delete_pairing_code(pairing.id)
    except Exception as exc:
        log_error(
            "federation_pending_connection_cancel_failed",
            pairing_id=pairing.id,
            error=str(exc),
        )
        _close_modal_done(
            client,
            body,
            ":warning: Could not cancel that connection. Please try again.",
        )
        return
    log_info("federation_pending_connection_cancelled", pairing_id=pairing.id)
    _close_modal_done(client, body, f":wastebasket: Cancelled {name}.")
    acting_user_id = helpers.get_user_id_from_body(body)
    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=acting_user_id)


def handle_verify_external_connection(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open Verify Trust with the details the admin should confirm out of band."""
    if not helpers.federation_enabled():
        return
    if not _require_primary_admin(body, client, context, action="verify_external_connection"):
        return
    peer = _peer_or_none(_instance_id_from_action(body, actions.CONFIG_VERIFY_EXTERNAL_CONNECTION))
    if not peer:
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    stubs = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.instance_id == peer.instance_id,
            schemas.Workspace.deleted_at.is_(None),
        ],
    )
    remote_names = [helpers.resolve_workspace_name(stub) or stub.team_id for stub in stubs if stub.team_id]
    view = orm.BlockView(
        blocks=[
            section(
                ":warning: *Verify this External Connection before trusting it again.*\n\n"
                "Confirm these details with the other admin. Trusting a wrong instance "
                "lets that SyncBot send and receive messages for the Workspaces on this connection."
            ),
            *_connection_detail_blocks(
                {
                    "label": peer.name or "Unnamed connection",
                    "webhook_url": peer.webhook_url or "",
                    "instance_id": peer.instance_id,
                    "primary_workspace_name": getattr(peer, "primary_workspace_name", None) or "",
                    "primary_team_id": getattr(peer, "primary_team_id", None) or "",
                },
                remote_names=remote_names,
            ),
        ]
    )
    view.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_VERIFY_EXTERNAL_CONNECTION_SUBMIT,
        title_text="Verify Trust",
        submit_button_text="Verify Trust",
        close_button_text="Cancel",
        parent_metadata={"instance_id": peer.instance_id},
        body=body,
    )


def handle_verify_external_connection_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Mark the peer trusted and unpause its stub SyncChannels."""
    if not helpers.federation_enabled():
        return
    workspace_record = _require_primary_admin(body, client, context, action="verify_external_connection_submit")
    if not workspace_record:
        return
    peer = _peer_or_none(str(_parse_private_metadata(body).get("instance_id") or ""))
    if not peer:
        return
    from helpers.workspace_kind import mark_peer_trusted

    mark_peer_trusted(peer)
    invalidate_fed_ws_for_sync_cache()
    log_info("federation_trust_verified", instance_id=peer.instance_id)
    acting_user_id = helpers.get_user_id_from_body(body)
    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=acting_user_id)


def handle_leave_external_connection(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Show a confirmation modal before leaving an External Connection."""
    if not _require_primary_admin(body, client, context, action="leave_external_connection"):
        return
    peer = _peer_or_none(_instance_id_from_action(body, actions.CONFIG_LEAVE_EXTERNAL_CONNECTION))
    if not peer:
        log_warning(
            "leave_external_connection_refused_self",
            instance_id=_instance_id_from_action(body, actions.CONFIG_LEAVE_EXTERNAL_CONNECTION),
        )
        return
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    peer_name = peer.name or f"Connection {peer.instance_id[:8]}"
    retention_days = helpers.soft_delete_retention_days()
    confirm = orm.BlockView(
        blocks=[
            section(
                f":warning: *Are you sure you want to leave {peer_name}?*\n\n"
                "This will:\n"
                "\u2022 Pause remote Workspaces for this connection on this instance\n"
                f"\u2022 Keep their Sync history for {retention_days} days "
                "(reconnect or reinstall within that window restores it)\n"
                "\u2022 Leave the other instance still listing this connection until they leave too\n\n"
                "_No messages will be deleted from Slack._"
            ),
            orm.ActionsBlock(
                elements=[
                    orm.ButtonElement(
                        label=":wave: Leave Connection",
                        action=actions.CONFIG_LEAVE_EXTERNAL_CONNECTION_CONFIRM,
                        value=str(peer.instance_id),
                        style="danger",
                    ),
                ]
            ),
        ]
    )
    confirm.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_LEAVE_EXTERNAL_CONNECTION_CONFIRM,
        title_text="Leave Connection",
        submit_button_text=None,
        close_button_text="Cancel",
        parent_metadata={"instance_id": peer.instance_id},
        body=body,
    )


def handle_leave_external_connection_confirm(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Deactivate the peer and pause its stub workspaces for the retention window."""
    workspace_record = _require_primary_admin(body, client, context, action="leave_external_connection_confirm")
    if not workspace_record:
        return

    fed_ws_id = str(_parse_private_metadata(body).get("instance_id") or "").strip()
    if not fed_ws_id:
        action_data = helpers.safe_get(body, "actions", 0) or {}
        fed_ws_id = str(action_data.get("value") or "").strip()
    peer = _peer_or_none(fed_ws_id)
    if not peer:
        return

    stubs = DbManager.find_records(
        schemas.Workspace,
        [schemas.Workspace.instance_id == peer.instance_id],
    )
    for stub in stubs:
        helpers.soft_delete_workspace(stub)

    DbManager.delete_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == peer.instance_id],
    )

    now = datetime.now(UTC)
    DbManager.update_records(
        schemas.Instance,
        [schemas.Instance.instance_id == peer.instance_id],
        {
            schemas.Instance.status: "inactive",
            schemas.Instance.updated_at: now,
        },
    )

    invalidate_fed_ws_for_sync_cache()
    log_info("federation_connection_removed", instance_id=peer.instance_id)
    _close_modal_done(client, body, f":wave: Left {peer.name or 'this External Connection'}.")

    acting_user_id = helpers.get_user_id_from_body(body)
    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=acting_user_id)
