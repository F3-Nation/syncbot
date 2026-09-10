"""Operator and workspace Settings modal.

Slack admins and owners on any installed workspace may open Settings.
Workspace fields (extra managers, private channels) always apply to that
workspace. Instance fields (federation, broadcast, retention) appear only when
``PRIMARY_WORKSPACE`` matches the acting team.

Secrets, connection details, and the ``ENABLE_DB_RESET`` break-glass switch stay
in environment variables.
"""

import logging
from logging import Logger

from slack_sdk.web import WebClient

import builders
import constants
import helpers
from db import DbManager, schemas
from slack import actions, orm

_logger = logging.getLogger(__name__)

_BOOL_YES = "true"
_BOOL_NO = "false"


def _team_id_from_body(body: dict) -> str | None:
    """Resolve the acting team from either a block action or a view submission."""
    return (
        helpers.safe_get(body, "view", "team_id")
        or helpers.safe_get(body, "team", "id")
        or helpers.safe_get(body, "team_id")
        or helpers.safe_get(body, "user", "team_id")
    )


def _installed_workspace_options() -> list[orm.SelectorOption]:
    """Every installed workspace, as options keyed by Slack team id."""
    workspaces = DbManager.find_records(schemas.Workspace, [schemas.Workspace.deleted_at.is_(None)])
    options = []
    for workspace in workspaces:
        if not workspace.team_id or not workspace.bot_token:
            continue
        options.append(
            orm.SelectorOption(
                name=helpers.resolve_workspace_name(workspace) or workspace.team_id,
                value=workspace.team_id,
            )
        )
    return sorted(options, key=lambda option: option.name.lower())


def _build_settings_form(team_id: str) -> orm.BlockView:
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
                    "Channel elsewhere will be able to read that content. Broadcasts always require a "
                    "public Channel regardless of this setting."
                ),
            ),
        ),
    ]

    if helpers.is_primary_workspace(team_id):
        workspace_options = _installed_workspace_options()
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
                    label="Workspaces allowed to publish a Broadcast",
                    action=actions.CONFIG_SETTINGS_BROADCAST_WORKSPACES,
                    element=orm.MultiStaticSelectElement(
                        placeholder="Leave empty to allow any Workspace",
                        initial_values=helpers.broadcast_allowed_workspaces(),
                        options=workspace_options,
                    ),
                    optional=True,
                ),
                orm.ContextBlock(
                    element=orm.ContextElement(
                        initial_value=(
                            "Leave this empty and any installed Workspace may publish a Broadcast, which is "
                            "the default. Select one or more Workspaces to restrict it to just those."
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
            ]
        )

    return orm.BlockView(blocks=blocks)


def handle_open_settings(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open the Settings modal for a workspace admin."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = _team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_admin(client, user_id):
        _logger.warning("authorization_denied", extra={"user_id": user_id, "action": "open_settings"})
        return

    if not helpers.is_settings_visible_for_workspace(team_id):
        _logger.warning("authorization_denied", extra={"action": "open_settings", "team_id": team_id})
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    _build_settings_form(team_id).post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_SETTINGS_SUBMIT,
        title_text="SyncBot Settings",
        submit_button_text="Save",
        close_button_text="Cancel",
        body=body,
    )


def handle_settings_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Persist workspace and (when primary) instance settings."""
    user_id = helpers.get_user_id_from_body(body)
    team_id = _team_id_from_body(body)
    if not user_id or not team_id or not helpers.is_workspace_admin(client, user_id):
        _logger.warning("authorization_denied", extra={"user_id": user_id, "action": "settings_submit"})
        return

    if not helpers.is_settings_visible_for_workspace(team_id):
        _logger.warning("authorization_denied", extra={"action": "settings_submit", "team_id": team_id})
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

        broadcast = selected.get(actions.CONFIG_SETTINGS_BROADCAST_WORKSPACES)
        if actions.CONFIG_SETTINGS_BROADCAST_WORKSPACES in values:
            helpers.set_setting(
                constants.SETTING_BROADCAST_ALLOWED_WORKSPACES,
                ",".join(broadcast) if broadcast else "",
            )

        retention = selected.get(actions.CONFIG_SETTINGS_RETENTION_DAYS)
        if retention:
            try:
                days = int(float(retention))
            except (TypeError, ValueError):
                _logger.warning("settings_retention_unparseable")
            else:
                if days >= 1:
                    helpers.set_setting(constants.SETTING_SOFT_DELETE_RETENTION_DAYS, str(days))

    _logger.info("settings_updated", extra={"team_id": team_id})

    builders.refresh_home_tab_for_workspace(workspace_record, logger, context=context, user_id=user_id)
