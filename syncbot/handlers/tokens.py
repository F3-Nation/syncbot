"""Token revocation handler."""

from logging import Logger

from slack_sdk.errors import SlackApiError
from slack_sdk.web import WebClient

import builders
import helpers
from db import DbManager, schemas
from logger import log_error, log_info, log_warning


def handle_tokens_revoked(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle ``tokens_revoked`` using Bolt's installation store.

    Slack sends ``event.tokens.oauth`` (user IDs whose *user* tokens died) and
    ``event.tokens.bot``. A personal **Authorize SyncBot** revoke is
    ``InstallationStore.delete_installation`` for that user_id, then republish
    Home. Slack sometimes also fills ``tokens.bot`` on that event; only run the
    uninstall path (``delete_all`` + workspace pause) when the stored bot token
    fails ``auth.test``. Bolt's ``enable_token_revocation_listeners()`` would
    delete the bot whenever ``tokens.bot`` is present, which blanks Home.
    """
    team_id = helpers.get_team_id_from_body(body)
    if not team_id:
        log_warning("handle_tokens_revoked", reason="missing team_id")
        return

    event = helpers.safe_get(body, "event") or {}
    tokens = event.get("tokens") if isinstance(event, dict) else None
    if not isinstance(tokens, dict):
        tokens = {}
    oauth_users = [str(uid) for uid in (tokens.get("oauth") or []) if uid]
    bot_users = [str(uid) for uid in (tokens.get("bot") or []) if uid]

    if bot_users:
        workspace_record = DbManager.get_record(schemas.Workspace, team_id)
        if workspace_record and _workspace_bot_is_alive(workspace_record):
            log_info("tokens_revoked_ignored_bot_array", team_id=team_id, oauth_users=oauth_users, bot_users=bot_users)
        else:
            helpers.uninstall_workspace(team_id)
            return

    for user_id in oauth_users:
        helpers.clear_user_authorization(team_id, user_id)
        _republish_home_after_user_revoke(team_id, user_id, logger)


def handle_app_uninstalled(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle ``app_uninstalled``: Bolt ``delete_all`` plus SyncBot workspace pause.

    This is the event Bolt's built-in listener uses for a workspace uninstall.
    We call the same store method, then pause groups and channel syncs.
    """
    team_id = helpers.get_team_id_from_body(body)
    if not team_id:
        log_warning("handle_app_uninstalled", reason="missing team_id")
        return
    helpers.uninstall_workspace(team_id)


def _workspace_bot_is_alive(workspace_record) -> bool:
    """True when this workspace's stored bot token still authenticates.

    Slack sometimes includes ``tokens.bot`` on a personal user-token revoke.
    Uninstalling in that case pauses every sync. If ``auth.test`` still works,
    treat the event as user-token-only.
    """
    if not workspace_record or not helpers.get_bot_token(workspace_record):
        return False
    try:
        WebClient(token=helpers.get_bot_token(workspace_record)).auth_test()
        return True
    except Exception:
        return False


def _republish_home_after_user_revoke(team_id: str, user_id: str, logger: Logger) -> None:
    """Rebuild Home so Authorize SyncBot reappears without waiting for Refresh."""
    workspace_record = DbManager.get_record(schemas.Workspace, team_id)
    if not workspace_record or workspace_record.deleted_at or not helpers.get_bot_token(workspace_record):
        return
    try:
        client = WebClient(token=helpers.get_bot_token(workspace_record))
        builders.build_home_tab(
            {"team": {"id": team_id}, "user": {"id": user_id}},
            client,
            logger,
            {},
            user_id=user_id,
        )
    except SlackApiError as exc:
        error = None
        try:
            error = exc.response["error"]
        except Exception:
            error = None
        if error == "account_inactive":
            log_info("tokens_revoked_home_skipped_inactive_user", team_id=team_id, user_id=user_id)
            return
        log_error("tokens_revoked_home_refresh_failed", team_id=team_id, user_id=user_id, exc_info=True)
    except Exception:
        log_error("tokens_revoked_home_refresh_failed", team_id=team_id, user_id=user_id, exc_info=True)
