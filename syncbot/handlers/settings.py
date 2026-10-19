"""Operator and workspace Settings modal.

Slack admins and owners on any installed workspace may open Settings.
Workspace fields (extra managers, private channels) always apply to that
workspace. Instance fields (federation, retention) appear only when
``PRIMARY_WORKSPACE`` matches the acting team.

Secrets, connection details, and the ``ENABLE_DB_RESET`` break-glass switch stay
in environment variables.
"""

import os
from logging import Logger

from slack_sdk.web import WebClient

import builders
import constants
import helpers
from db import DbManager, schemas
from logger import log_info, log_warning
from slack import actions, orm

_BOOL_YES = "true"
_BOOL_NO = "false"
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _app_version() -> str:
    """Installed package version, or ``dev`` when not installed."""
    return constants.app_version()


def _log_level_label() -> str:
    """Effective ``LOG_LEVEL`` (defaults to INFO, same as ``configure_logging``)."""
    raw = (os.environ.get("LOG_LEVEL") or "").strip().upper()
    return raw if raw in _LOG_LEVELS else "INFO"


def _primary_workspace_label() -> str:
    """Display name of ``PRIMARY_WORKSPACE``, or ``Not set``."""
    team_id = (os.environ.get(constants.PRIMARY_WORKSPACE) or "").strip()
    if not team_id:
        return "Not set"
    matches = DbManager.find_records(
        schemas.Workspace,
        [schemas.Workspace.team_id == team_id, schemas.Workspace.deleted_at.is_(None)],
    )
    if not matches:
        return team_id
    return helpers.resolve_workspace_name(matches[0]) or team_id


def _instance_fingerprint() -> str:
    import federation

    return federation.get_instance_id()


def _public_url_label(context: dict | None) -> str:
    return helpers.get_public_base_url(context) or "None yet"


def _database_label() -> str:
    return constants.get_database_backend()


def _information_blocks(team_id: str, context: dict | None = None) -> list[orm.BaseBlock]:
    """Read-only install facts. Primary-only lines stay off other Workspaces."""
    lines = [
        f"Version: `{_app_version()}`",
        f"Primary Workspace: `{_primary_workspace_label()}`",
    ]
    if helpers.is_primary_workspace(team_id):
        lines.extend(
            [
                f"Log level: `{_log_level_label()}`",
                f"Fingerprint: `{_instance_fingerprint()}`",
                f"Public URL: `{_public_url_label(context)}`",
                f"Database: `{_database_label()}`",
            ]
        )
    return [
        orm.DividerBlock(),
        orm.HeaderBlock(text="Information"),
        orm.SectionBlock(label="\n".join(lines)),
    ]


def _build_settings_form(team_id: str, context: dict | None = None) -> orm.BlockView:
    """Build the settings modal for *team_id*."""
    blocks: list[orm.BaseBlock] = [
        orm.InputBlock(
            label="Extra managers",
            action=actions.CONFIG_SETTINGS_EXTRA_MANAGERS,
            element=orm.MultiUsersSelectElement(
                placeholder="Optional — members who can configure groups and syncs",
                initial_value=helpers.extra_manager_user_ids(team_id),
            ),
            optional=True,
        ),
        orm.ContextBlock(
            element=orm.ContextElement(
                initial_value=(
                    "Extra managers may create groups, publish, and subscribe, but they cannot open "
                    "Settings, Backup/Restore, Reset Database, or External Connections."
                ),
            ),
        ),
        orm.InputBlock(
            label="Allow private Channels in this Workspace",
            action=actions.CONFIG_SETTINGS_ALLOW_PRIVATE_CHANNELS,
            element=orm.RadioButtonsElement(
                initial_value=_BOOL_YES if helpers.allow_private_channels(team_id) else _BOOL_NO,
                options=[
                    orm.SelectorOption(name="No — public Channels only (recommended)", value=_BOOL_NO),
                    orm.SelectorOption(name="Yes — allow private Channels", value=_BOOL_YES),
                ],
            ),
            optional=False,
        ),
        orm.ContextBlock(
            element=orm.ContextElement(
                initial_value=(
                    "When this is on, a manager can publish a private Channel in this Workspace, and "
                    "its messages will be copied into other Workspaces. Anyone who can see the synced "
                    "Channel elsewhere will be able to read that content."
                ),
            ),
        ),
    ]

    if helpers.is_primary_workspace(team_id):
        blocks.extend(
            [
                orm.InputBlock(
                    label="Enable Federation",
                    action=actions.CONFIG_SETTINGS_FEDERATION_ENABLED,
                    element=orm.RadioButtonsElement(
                        initial_value=_BOOL_YES if helpers.federation_enabled() else _BOOL_NO,
                        options=[
                            orm.SelectorOption(name="No — external connections disabled", value=_BOOL_NO),
                            orm.SelectorOption(name="Yes — allow external connections", value=_BOOL_YES),
                        ],
                    ),
                    optional=False,
                ),
                orm.ContextBlock(
                    element=orm.ContextElement(
                        initial_value=(
                            "When this is off, External Connections are hidden and other instances cannot "
                            "reach this one (except ping). Turning it off does not remove existing peers; "
                            "turn it back on to resume."
                        ),
                    ),
                ),
                orm.InputBlock(
                    label="Days to retain a removed Workspace",
                    action=actions.CONFIG_SETTINGS_RETENTION_DAYS,
                    element=orm.NumberInputElement(
                        initial_value=helpers.soft_delete_retention_days(),
                        min_value=1,
                        max_value=3650,
                        is_decimal_allowed=False,
                    ),
                    optional=False,
                ),
                orm.ContextBlock(
                    element=orm.ContextElement(
                        initial_value=(
                            "When a Workspace uninstalls SyncBot, its data is kept for this many days so a "
                            "reinstall picks up where it left off. After that it is permanently deleted."
                        ),
                    ),
                ),
                orm.InputBlock(
                    label="Workspace Block List",
                    action=actions.CONFIG_SETTINGS_WORKSPACE_BLOCK_LIST,
                    element=orm.PlainTextInputElement(
                        initial_value=helpers.format_workspace_block_list(helpers.workspace_block_list()),
                        multiline=True,
                        placeholder="T0123456789, T9876543210",
                    ),
                    optional=True,
                ),
                orm.ContextBlock(
                    element=orm.ContextElement(
                        initial_value=(
                            "Slack Team IDs, separated by commas or spaces. A listed Workspace is "
                            "uninstalled and cannot reinstall until you remove the ID. A Workspace that "
                            "owns a group cannot be added until you promote another Owner."
                        ),
                    ),
                ),
            ]
        )

    blocks.extend(_information_blocks(team_id, context))
    return orm.BlockView(blocks=blocks)


def handle_open_settings(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the Settings modal for a workspace admin."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_admin(client, user_id):
        log_warning("authorization_denied", user_id=user_id, action="open_settings")
        return

    if not helpers.is_settings_visible_for_workspace(team_id):
        log_warning("authorization_denied", action="open_settings", team_id=team_id)
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    _build_settings_form(team_id, context).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_SETTINGS_SUBMIT,
        title_text="SyncBot Settings",
        submit_button_text="Save",
        close_button_text="Cancel",
        body=body,
    )


def _block_list_field_error(raw: str | None) -> str | None:
    """Ack-phase validation for the Workspace Block List. DB only."""
    ids, err = helpers.parse_workspace_block_list(raw)
    if err:
        return err
    primary = (os.environ.get(constants.PRIMARY_WORKSPACE) or "").strip().upper()
    if primary and primary in ids:
        return "The primary Workspace cannot be blocked."
    for team_id in ids:
        workspace = DbManager.get_record(schemas.Workspace, team_id)
        if not workspace or workspace.deleted_at is not None:
            continue
        for group in helpers.get_groups_for_workspace(workspace.id):
            if helpers.is_workspace_owner(group.id, workspace.id):
                name = helpers.resolve_workspace_name(workspace)
                return f"{name} is a group Owner. Promote another Workspace to Owner first, then add this Team ID."
    return None


def handle_settings_submit_ack(body: dict, client: WebClient, context: dict) -> dict | None:
    """Ack Settings: field errors for the block list only."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_admin(client, user_id):
        return None
    if not helpers.is_primary_workspace(team_id):
        return None
    selected = _build_settings_form(team_id).get_selected_values(body)
    raw = selected.get(actions.CONFIG_SETTINGS_WORKSPACE_BLOCK_LIST)
    if raw is None:
        raw = ""
    error = _block_list_field_error(str(raw))
    if error:
        return {
            "response_action": "errors",
            "errors": {actions.CONFIG_SETTINGS_WORKSPACE_BLOCK_LIST: error},
        }
    return None


def handle_settings_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Persist workspace and (when primary) instance settings."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = helpers.get_team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_admin(client, user_id):
        log_warning("authorization_denied", user_id=user_id, action="settings_submit")
        return

    if not helpers.is_settings_visible_for_workspace(team_id):
        log_warning("authorization_denied", action="settings_submit", team_id=team_id)
        return

    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return

    values = helpers.safe_get(body, "view", "state", "values") or {}
    selected = _build_settings_form(team_id).get_selected_values(body)

    allow_private = selected.get(actions.CONFIG_SETTINGS_ALLOW_PRIVATE_CHANNELS)
    if allow_private in (_BOOL_YES, _BOOL_NO):
        helpers.set_workspace_setting(
            workspace_record.id,
            constants.SETTING_ALLOW_PRIVATE_CHANNELS,
            allow_private,
            team_id=team_id,
        )

    if actions.CONFIG_SETTINGS_EXTRA_MANAGERS in values:
        extra = selected.get(actions.CONFIG_SETTINGS_EXTRA_MANAGERS) or []
        helpers.set_extra_manager_user_ids(team_id, extra)

    if helpers.is_primary_workspace(team_id):
        federation = selected.get(actions.CONFIG_SETTINGS_FEDERATION_ENABLED)
        if federation in (_BOOL_YES, _BOOL_NO):
            helpers.set_setting(constants.SETTING_FEDERATION_ENABLED, federation)

        retention = selected.get(actions.CONFIG_SETTINGS_RETENTION_DAYS)
        if retention:
            try:
                days = int(float(retention))
            except (TypeError, ValueError):
                log_warning("settings_retention_unparseable")
            else:
                if days >= 1:
                    helpers.set_setting(constants.SETTING_SOFT_DELETE_RETENTION_DAYS, str(days))

        raw_block = selected.get(actions.CONFIG_SETTINGS_WORKSPACE_BLOCK_LIST)
        if raw_block is not None:
            field_error = _block_list_field_error(str(raw_block))
            if field_error:
                log_warning("settings_block_list_rejected", reason=field_error)
            else:
                new_ids, err = helpers.parse_workspace_block_list(str(raw_block))
                if err:
                    log_warning("settings_block_list_unparseable")
                else:
                    previous = set(helpers.workspace_block_list())
                    helpers.set_setting(
                        constants.SETTING_WORKSPACE_BLOCK_LIST,
                        helpers.format_workspace_block_list(new_ids) or None,
                    )
                    for team in new_ids:
                        if team in previous:
                            continue
                        installed = DbManager.get_record(schemas.Workspace, team)
                        if not installed or installed.deleted_at is not None:
                            continue
                        token = helpers.get_bot_token(installed)
                        helpers.slack_apps_uninstall(token)
                        helpers.uninstall_workspace(team)

    log_info("settings_updated", team_id=team_id)

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
