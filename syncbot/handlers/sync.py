"""Sync management handlers — Home tab, auth, membership leave, DB reset."""

import logging
import time
from logging import Logger

from slack_sdk.web import WebClient

import builders
import constants
import helpers
from db import DbManager, schemas
from slack import actions, orm

_logger = logging.getLogger(__name__)


def handle_app_home_opened(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle the ``app_home_opened`` event by publishing the Home tab."""
    helpers.purge_stale_soft_deletes()
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
    _logger.info(
        "authorize_syncbot_clicked",
        extra={
            "team_id": helpers.safe_get(body, "team", "id"),
            "user_id": helpers.safe_get(body, "user", "id"),
        },
    )


def handle_refresh_home(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle the Refresh button on the Home tab.

    Available to everyone so a non-admin can reload Home after revoking
    Authorize SyncBot. Uses content hash and cached blocks: full refresh only
    when data changed. When hash matches and within 60s cooldown, re-publishes
    with a cooldown message. Rebuilds this user's Home from the DB; workspace
    names refresh at most daily when a workspace is loaded, not via an
    instance-wide ``team_info`` sweep.
    """
    team_id = helpers.safe_get(body, "view", "team_id") or helpers.safe_get(body, "team", "id")
    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
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
    refresh_at_key = f"refresh_at:home:{team_id}:{user_id}"

    action, cached_blocks, remaining = helpers.refresh_cooldown_check(
        current_hash, hash_key, blocks_key, refresh_at_key
    )
    cooldown_sec = getattr(constants, "REFRESH_COOLDOWN_SECONDS", 60)

    if action == "cooldown" and cached_blocks is not None and remaining is not None:
        refresh_idx = helpers.index_of_block_with_action(cached_blocks, actions.CONFIG_REFRESH_HOME)
        blocks_with_message = helpers.inject_cooldown_message(cached_blocks, refresh_idx, remaining)
        client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks_with_message})
        return
    if action == "cached" and cached_blocks is not None:
        client.views_publish(user_id=user_id, view={"type": "home", "blocks": cached_blocks})
        helpers._cache_set(refresh_at_key, time.monotonic(), ttl=cooldown_sec * 2)
        return

    # Names refresh at most daily in get_workspace_record / _maybe_refresh_workspace_name.
    # Do not team_info() every installed workspace on Refresh.
    block_dicts = builders.build_home_tab(body, client, logger, context, user_id=user_id, return_blocks=True)
    if block_dicts is None:
        return
    client.views_publish(user_id=user_id, view={"type": "home", "blocks": block_dicts})
    helpers.refresh_after_full(hash_key, blocks_key, refresh_at_key, current_hash, block_dicts)


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
    team_id = helpers.safe_get(body, "team_id") or event.get("team")

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
        _logger.warning(f"Failed to notify and leave untracked channel {channel_id}: {e}")


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
    team_id = helpers.safe_get(body, "team", "id") or helpers.safe_get(body, "view", "team_id")
    if not helpers.is_db_reset_visible_for_workspace(team_id):
        return

    user_id = helpers.safe_get(body, "user", "id") or helpers.get_user_id_from_body(body)
    if not user_id or not helpers.is_workspace_admin(client, user_id):
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    orm.open_or_push_view(
        client,
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
        body=body,
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
    team_id = helpers.safe_get(body, "team", "id") or helpers.safe_get(body, "view", "team_id")
    if not helpers.is_db_reset_visible_for_workspace(team_id):
        return

    user_id = helpers.get_user_id_from_body(body)
    if not user_id or not helpers.is_workspace_admin(client, user_id):
        return

    # Update the modal to a "done" state so the user can close it (Slack only allows
    # closing modals via view_submission, not block_actions, so we replace the view).
    view_id = helpers.safe_get(body, "view", "id")
    if view_id:
        try:
            client.views_update(
                view_id=view_id,
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Reset Complete"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": ":skull_and_crossbones: You can close this now.",
                            },
                        },
                    ],
                },
            )
        except Exception as e:
            _logger.warning("Failed to update modal after DB reset: %s", e)

    _logger.critical(
        "DB_RESET triggered by user %s — dropping database and reinitializing via Alembic",
        user_id,
    )

    from db import drop_and_init_db

    drop_and_init_db()

    helpers.clear_all_caches()

    if team_id and user_id:
        try:
            client.views_publish(
                user_id=user_id,
                view={
                    "type": "home",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "*Database Has Been Reset!*\nPlease reinstall SyncBot in your Workspace.",
                            },
                        }
                    ],
                },
            )
        except Exception as e:
            _logger.warning("Failed to publish post-reset Home tab: %s", e)
