"""Sync management handlers — Home tab, auth, membership leave, DB reset."""

from logging import Logger

from slack_sdk.web import WebClient

import builders
import helpers
from db import DbManager, schemas
from handlers._common import _update_wait_modal
from logger import log_critical, log_info, log_warning
from slack import actions, orm


def _pulse_after_home() -> None:
    """Federation allowlist/snapshot after Home is already published."""
    from federation.core import refresh_instance

    refresh_instance()


def handle_app_home_opened(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle the ``app_home_opened`` event by publishing the Home tab."""
    helpers.purge_stale_soft_deletes()
    team_id = helpers.get_team_id_from_body(body)
    user_id = helpers.get_user_id_from_body(body)
    if team_id and user_id:
        workspace_record = helpers.get_workspace_record(team_id, body, context, client)
        if workspace_record:
            is_admin = helpers.is_workspace_admin(client, user_id)
            is_manager = helpers.is_workspace_manager(client, user_id, team_id)
            extra_manager_ids = tuple(sorted(helpers.extra_manager_user_ids(team_id)))
            current_hash = builders._home_tab_content_hash(
                workspace_record,
                user_id,
                is_manager=is_manager,
                is_admin=is_admin,
                extra_manager_ids=extra_manager_ids,
            )
            hash_key = builders.home_tab_hash_key(team_id, user_id)
            blocks_key = f"home_tab_blocks:{team_id}:{user_id}"
            if helpers._cache_get(hash_key) == current_hash:
                cached_blocks = helpers._cache_get(blocks_key)
                if cached_blocks is not None:
                    client.views_publish(user_id=user_id, view={"type": "home", "blocks": cached_blocks})
                    helpers.remember_home_viewer(team_id, user_id)
                    return
            builders.build_home_tab(
                body,
                client,
                logger,
                context,
                user_id=user_id,
                workspace=workspace_record,
                content_hash=current_hash,
            )
            return
    builders.build_home_tab(body, client, logger, context)


def handle_authorize_syncbot(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Acknowledge the Authorize SyncBot button.

    The button is a link: Slack opens the OAuth install from its ``url`` and this
    payload arrives only as a notification. Registering it keeps the click out of
    the ``no_handler`` error log. The Home tab drops the section on the next
    ``app_home_opened`` once the install writes a user token.
    """
    log_info(
        "authorize_syncbot_clicked",
        team_id=helpers.get_team_id_from_body(body),
        user_id=helpers.get_user_id_from_body(body),
    )


def handle_refresh_home(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle the Refresh button on the Home tab.

    Available to everyone so a non-admin can reload Home after revoking
    Authorize SyncBot. When the content hash matches the cached Home, Refresh
    does nothing. Otherwise it rebuilds this user's Home from the DB.
    Workspace names refresh at most daily when a workspace is loaded, not via
    an instance-wide ``team_info`` sweep. After Home is published, pulses
    External Connections (allowlist/snapshot) so Refresh is not Home-only.
    """
    team_id = helpers.get_team_id_from_body(body)
    user_id = helpers.get_user_id_from_body(body)
    if not team_id or not user_id:
        return

    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return

    is_admin = helpers.is_workspace_admin(client, user_id)
    is_manager = helpers.is_workspace_manager(client, user_id, team_id)
    extra_manager_ids = tuple(sorted(helpers.extra_manager_user_ids(team_id)))
    current_hash = builders._home_tab_content_hash(
        workspace_record,
        user_id,
        is_manager=is_manager,
        is_admin=is_admin,
        extra_manager_ids=extra_manager_ids,
    )
    hash_key = builders.home_tab_hash_key(team_id, user_id)
    blocks_key = f"home_tab_blocks:{team_id}:{user_id}"

    if helpers.cached_home_blocks(current_hash, hash_key, blocks_key) is not None:
        return

    # Names refresh at most daily in get_workspace_record / _maybe_refresh_workspace_name.
    # Do not team_info() every installed workspace on Refresh.
    block_dicts = builders.build_home_tab(
        body,
        client,
        logger,
        context,
        user_id=user_id,
        return_blocks=True,
        workspace=workspace_record,
        content_hash=current_hash,
    )
    if block_dicts is None:
        return
    client.views_publish(user_id=user_id, view={"type": "home", "blocks": block_dicts})
    helpers.refresh_after_full(hash_key, blocks_key, current_hash, block_dicts)
    _pulse_after_home()


def handle_member_joined_channel(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle member_joined_channel: check if SyncBot was added to an untracked channel."""
    event = body.get("event", {})
    user_id = event.get("user")
    channel_id = event.get("channel")
    team_id = helpers.get_team_id_from_body(body)

    if not user_id or not channel_id or not team_id:
        return

    own_user_id = helpers.get_own_bot_user_id(client, context)
    if user_id != own_user_id:
        return

    any_sync_channel = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.channel_id == channel_id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    if any_sync_channel:
        return

    try:
        client.chat_postMessage(
            channel=channel_id,
            text=":wave: Hello! I'm SyncBot. I was added to this Channel, but this Channel "
            "doesn't seem to be part of a Channel Sync. I'm leaving now. Please open the SyncBot Home "
            "tab to Create Sync or Join Sync.",
        )
        client.conversations_leave(channel=channel_id)
    except Exception as e:
        log_warning("failed_to_notify_and_leave_untracked_channel", channel_id=channel_id, error=str(e))


# ---------------------------------------------------------------------------
# Database Reset (gated by PRIMARY_WORKSPACE + ENABLE_DB_RESET)
# ---------------------------------------------------------------------------


def handle_db_reset(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Open a confirmation modal warning the user before a full DB reset.

    Only when PRIMARY_WORKSPACE matches and ENABLE_DB_RESET is truthy (see helpers.core).
    """
    team_id = helpers.get_team_id_from_body(body)
    if not helpers.is_db_reset_visible_for_workspace(team_id):
        return

    user_id = helpers.get_user_id_from_body(body)
    if not user_id or not helpers.is_workspace_admin(client, user_id):
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    orm.update_opened_view(
        client,
        body,
        trigger_id,
        {
            "type": "modal",
            "title": {"type": "plain_text", "text": "Yikes! Reset Database?"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":rotating_light: *This Will Permanently Delete ALL Data!* :rotating_light:\n\n"
                            "Every Slack Install, Workspace Group, Channel Sync, and User Mapping, "
                            "in this database will be erased and the schema will be reinitialized.\n\n"
                            "*NOTE:* _All Slack Workspaces will need to reinstall the SyncBot app to get started again._\n\n"
                            "*This action cannot be undone! MAKE A BACKUP FIRST!*"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Confirm, Erase Everything!"},
                            "style": "danger",
                            "action_id": actions.CONFIG_DB_RESET_PROCEED,
                        },
                    ],
                },
            ],
        },
    )


def handle_db_reset_proceed(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Execute the database reset after user confirmed via modal.

    Same gating as handle_db_reset (PRIMARY_WORKSPACE + ENABLE_DB_RESET).
    """
    team_id = helpers.get_team_id_from_body(body)
    if not helpers.is_db_reset_visible_for_workspace(team_id):
        return

    user_id = helpers.get_user_id_from_body(body)
    if not user_id or not helpers.is_workspace_admin(client, user_id):
        return

    _update_wait_modal(client, body, title="Yikes! Reset Database?", dm=False)

    log_critical("db_reset_triggered", user_id=user_id)

    from db import drop_and_init_db

    drop_and_init_db()

    helpers.clear_all_caches()

    reset_done = "*Database Has Been Reset!*\nPlease reinstall SyncBot in your Workspace."
    view_id = helpers.safe_get(body, "view", "id")
    if view_id:
        try:
            client.views_update(
                view_id=view_id,
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Reset Complete"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": reset_done}}],
                },
            )
        except Exception as e:
            log_warning("db_reset_modal_update_failed", error=str(e))

    if team_id and user_id:
        try:
            client.views_publish(
                user_id=user_id,
                view={
                    "type": "home",
                    "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": reset_done}}],
                },
            )
        except Exception as e:
            log_warning("db_reset_home_publish_failed", error=str(e))
