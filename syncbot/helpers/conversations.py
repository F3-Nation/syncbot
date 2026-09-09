"""Getting SyncBot into a channel it is supposed to sync.

Slack draws a hard line between the two channel types:

* **Public** — the bot token can add the bot itself with ``conversations.join``.
* **Private** — ``conversations.join`` is rejected outright
  (``method_not_supported_for_channel_type``). Only a human who is already in
  the channel can add an app, with ``conversations.invite`` called using **their**
  user token (``xoxp``, ``groups:write``).

The person publishing or subscribing is by definition a member of the channel
they just picked, because Slack's channel picker only offers a private channel
to someone who belongs to it. So their user token is the one that works, and it
is the **only** token this module will use: never another member's, never the
installer's. Acting as one person to reach a private channel they chose is
authorization the picker already established; borrowing someone else's token
would let a publish reach a channel the publisher cannot see.

Those tokens are already written to ``slack_installations`` by Bolt for anyone
who completed the OAuth install; this module is the only place that reads them.

Imports submodules only (``constants``, ``helpers._cache``, ``helpers.core``,
``helpers.oauth``, ``helpers.settings``, ``helpers.slack_api``) and never the
``helpers`` package, per the import-direction constraint documented in
``helpers/sync_cleanup.py``.
"""

import logging
import os
import urllib.parse

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import constants
from helpers.core import safe_get
from helpers.slack_api import get_own_bot_user_id
from helpers.workspace_settings import allow_private_channels
from slack_manifest_scopes import USER_PERMISSION_GROUPS

_logger = logging.getLogger(__name__)

# Slack errors that mean "this bot cannot see the channel", which for a private
# channel is the expected answer rather than a failure.
_NOT_VISIBLE_ERRORS = frozenset({"channel_not_found", "missing_scope", "not_in_channel"})

# Errors that mean the bot is already where we want it.
_ALREADY_THERE_ERRORS = frozenset({"already_in_channel", "cant_invite_self", "method_post_only"})

AUTHORIZE_HINT = (
    "SyncBot needs your permission before it can add itself to a private Channel. "
    'Open the SyncBot Home tab, click "Authorize SyncBot", then try again.'
)


class ConversationAccessError(Exception):
    """SyncBot could not become a member of a channel.

    The message is written for the admin who clicked Create Sync or Join Sync, so it
    can be shown as a modal field error or sent as a DM without rewording.
    """


def _installation_store():
    """Return Bolt's installation store, or ``None`` in single-workspace mode.

    Built from the same OAuth settings the app serves ``/slack/install`` with, so
    there is no second source of truth for where installs are recorded.
    """
    from helpers.oauth import get_oauth_flow

    try:
        oauth_flow = get_oauth_flow()
    except Exception as exc:
        _logger.warning(f"installation store unavailable: {exc}")
        return None
    if oauth_flow is None:
        return None
    return oauth_flow.settings.installation_store


def _find_user_installation(team_id: str | None, user_id: str | None):
    """Return Bolt's installation row for this person, or ``None``."""
    if not team_id or not user_id:
        return None
    store = _installation_store()
    if store is None:
        return None
    try:
        return store.find_installation(enterprise_id=None, team_id=team_id, user_id=user_id)
    except Exception as exc:
        _logger.warning(f"find_installation failed for team {team_id}: {exc}")
        return None


def get_user_token(team_id: str | None, user_id: str | None) -> str | None:
    """Return *user_id*'s own Slack user token, or *None*.

    Only users who completed the OAuth install have one. Never log the return
    value.
    """
    from helpers._cache import request_scope_get, request_scope_set

    if not team_id or not user_id:
        return None
    cache_key = f"user_token:{team_id}:{user_id}"
    cached = request_scope_get(cache_key)
    if cached is not None:
        return cached or None
    installation = _find_user_installation(team_id, user_id)
    token = _clean_token(getattr(installation, "user_token", None) if installation else None)
    request_scope_set(cache_key, token or "")
    return token


def clear_user_authorization(team_id: str | None, user_id: str | None) -> bool:
    """Remove this person's installation row so Authorize SyncBot can return.

    Slack's Configuration → Revoke invalidates the token but does not edit our
    ``slack_installations`` row. This is Bolt's ``InstallationStore.delete_installation``
    with ``user_id`` set (not the team bot in ``slack_bots``), so authorize can
    fall back to the workspace bot token. Nulling columns and leaving an empty
    row makes authorize fail, and Home never publishes.
    """
    if not team_id or not user_id:
        return False
    store = _installation_store()
    if store is None:
        return False
    try:
        store.delete_installation(enterprise_id=None, team_id=team_id, user_id=user_id)
        return True
    except Exception as exc:
        _logger.warning(f"clear_user_authorization failed for team {team_id}: {exc}")
        return False


def clear_workspace_installations(team_id: str | None) -> bool:
    """Drop Bolt's bot and user install rows for this workspace.

    Same call as Slack Bolt's ``app_uninstalled`` listener:
    ``InstallationStore.delete_all``.
    """
    if not team_id:
        return False
    store = _installation_store()
    if store is None:
        return False
    try:
        store.delete_all(enterprise_id=None, team_id=team_id)
        return True
    except Exception as exc:
        _logger.warning(f"clear_workspace_installations failed for team {team_id}: {exc}")
        return False


def granted_user_scopes(team_id: str | None, user_id: str | None) -> frozenset[str]:
    """Return the user scopes stored for this person, or empty.

    Used to split the Home tab permission lists into already-allowed vs still
    needed. A missing row, a blank ``user_scopes`` column, or a token with no
    scopes all mean "none yet" so the already-allowed list stays hidden.
    """
    installation = _find_user_installation(team_id, user_id)
    if installation is None:
        return frozenset()
    raw = getattr(installation, "user_scopes", None)
    if not raw:
        return frozenset()
    if isinstance(raw, str):
        return frozenset(s.strip() for s in raw.split(",") if s.strip())
    return frozenset(str(s).strip() for s in raw if str(s).strip())


def user_permission_lists(team_id: str | None, user_id: str | None) -> tuple[list[str], list[str]]:
    """Return ``(already_allowed_labels, needed_labels)`` for the Home tab.

    A group is already allowed only when every scope in it is on the stored
    token. That is what makes a later scope change look like an addition rather
    than a redo: people see what they already granted, then what is new.
    """
    granted = granted_user_scopes(team_id, user_id)
    already: list[str] = []
    needed: list[str] = []
    for label, scopes in USER_PERMISSION_GROUPS:
        if all(scope in granted for scope in scopes):
            already.append(label)
        else:
            needed.append(label)
    return already, needed


def needs_user_authorization(team_id: str | None, user_id: str | None) -> bool:
    """Whether the Home tab should show *Authorize SyncBot* for this person.

    True when any current user-scope group is not fully granted, including a
    first-time visitor with no token and someone whose token predates a scope
    we added later.
    """
    _already, needed = user_permission_lists(team_id, user_id)
    return bool(needed)


def _clean_token(token) -> str | None:
    """Normalize a stored token: blank or non-string means "no token"."""
    if not isinstance(token, str):
        return None
    return token.strip() or None


def has_user_token(team_id: str | None, user_id: str | None) -> bool:
    """Whether this specific user has authorized SyncBot to act as them.

    Drives whether a private channel pick can succeed. There is deliberately no
    team-level variant: a private channel is reachable only through the membership
    of the person who picked it. The Home tab Authorize section uses
    :func:`needs_user_authorization` instead, so it can reappear when we add scopes.
    """
    return bool(get_user_token(team_id, user_id))


def _slack_error(exc: SlackApiError) -> str:
    return (safe_get(exc.response, "error") or "") if exc.response is not None else ""


def _is_user_not_found(exc: SlackApiError) -> bool:
    """True when Slack rejected the invitee as unknown in this workspace.

    Top-level ``user_not_found`` is the usual shape. Slack also wraps it as
    ``cant_invite`` with a per-user ``errors`` list when the ID is not a member
    of this workspace (for example a ``B…`` bot_id, or another workspace's bot).
    """
    if _slack_error(exc) == "user_not_found":
        return True
    nested = safe_get(exc.response, "errors") if exc.response is not None else None
    if not isinstance(nested, list):
        return False
    return any(isinstance(item, dict) and item.get("error") == "user_not_found" for item in nested)


def _channel_visibility(client: WebClient, channel_id: str, *, team_id: str | None) -> tuple[bool, bool]:
    """Return ``(is_private, is_member)`` as seen by the bot token.

    A private channel the bot has never been in is invisible to the bot token, so
    ``channel_not_found`` is treated as "private, not a member" when the
    private-channel policy is on, and as a hard failure when it is off. This is
    the one decision that must not reuse the picker's skip-the-lookup shortcut:
    join and invite are different API calls and we have to pick the right one.
    """
    try:
        response = client.conversations_info(channel=channel_id)
    except SlackApiError as exc:
        error = _slack_error(exc)
        if error in _NOT_VISIBLE_ERRORS and allow_private_channels(team_id):
            return True, False
        raise ConversationAccessError(
            f"SyncBot could not read that Channel (`{error or 'unknown error'}`). Pick a Channel it can reach."
        ) from exc
    except Exception as exc:
        if allow_private_channels(team_id):
            return True, False
        raise ConversationAccessError(
            "SyncBot could not read that Channel. Pick a public Channel it can join."
        ) from exc

    channel = safe_get(response, "channel") or {}
    return bool(channel.get("is_private")), bool(channel.get("is_member"))


def _is_member_id(value) -> bool:
    """True for a Slack member ID (``U…``). A ``B…`` bot_id is not invitable."""
    return isinstance(value, str) and value.startswith("U")


def _bot_member_id(
    client: WebClient,
    *,
    bot_user_id: str | None = None,
    context: dict | None = None,
    bypass_cache: bool = False,
) -> str | None:
    """This workspace's bot member ID, never another workspace's and never a bot_id."""
    if bypass_cache:
        looked_up = get_own_bot_user_id(client, bypass_cache=True)
        return looked_up if _is_member_id(looked_up) else None
    for candidate in (bot_user_id, (context or {}).get("bot_user_id")):
        if _is_member_id(candidate):
            return candidate
    looked_up = get_own_bot_user_id(client, context=context)
    return looked_up if _is_member_id(looked_up) else None


def ensure_bot_in_conversation(
    client: WebClient,
    channel_id: str,
    *,
    team_id: str | None,
    acting_user_id: str | None,
    bot_user_id: str | None = None,
    context: dict | None = None,
) -> None:
    """Make SyncBot a member of *channel_id*, whichever type it is.

    Public channels are joined with the bot token. Private channels are joined by
    inviting the bot as *acting_user_id* and no one else, because a bot cannot
    add itself to one and because that person's own membership is the only thing
    that makes reaching the channel legitimate.

    Raises :class:`ConversationAccessError` with admin-facing text on failure. The
    caller is expected to undo whatever it wrote before calling, so a channel is
    never listed as synced while the bot cannot read it.
    """
    if not channel_id:
        raise ConversationAccessError("No Channel was selected.")

    is_private, is_member = _channel_visibility(client, channel_id, team_id=team_id)
    if is_member:
        return

    if not is_private:
        try:
            client.conversations_join(channel=channel_id)
        except SlackApiError as exc:
            error = _slack_error(exc)
            if error in _ALREADY_THERE_ERRORS:
                return
            raise ConversationAccessError(
                f"SyncBot could not join that Channel (`{error or 'unknown error'}`)."
            ) from exc
        return

    if not allow_private_channels(team_id):
        raise ConversationAccessError("Private Channels cannot be synced. Pick a public Channel.")

    invite_user_id = _bot_member_id(client, bot_user_id=bot_user_id, context=context)
    if not invite_user_id:
        raise ConversationAccessError("SyncBot could not determine its own identity. Please try again.")

    token = get_user_token(team_id, acting_user_id)
    if not token:
        raise ConversationAccessError(AUTHORIZE_HINT)

    try:
        WebClient(token=token).conversations_invite(channel=channel_id, users=invite_user_id)
    except SlackApiError as exc:
        error = _slack_error(exc)
        if error in _ALREADY_THERE_ERRORS:
            return
        if error == "not_in_channel":
            # The picker only offers private channels the user belongs to, so
            # this means a stale or hand-built payload rather than a normal pick.
            raise ConversationAccessError(
                "SyncBot could not be added to that private Channel, because you are not a "
                "member of it. Open the Channel, then publish or subscribe it again."
            ) from exc
        if _is_user_not_found(exc):
            # Warm containers used to cache one bot member ID for every
            # workspace. Inviting that ID into a different workspace is exactly
            # ``user_not_found``. Retry once with a fresh ``auth.test``.
            fresh_id = _bot_member_id(client, bypass_cache=True)
            if fresh_id and fresh_id != invite_user_id:
                try:
                    WebClient(token=token).conversations_invite(channel=channel_id, users=fresh_id)
                    return
                except SlackApiError as retry_exc:
                    retry_error = _slack_error(retry_exc)
                    if retry_error in _ALREADY_THERE_ERRORS:
                        return
                    exc = retry_exc
            raise ConversationAccessError(
                "SyncBot could not be added to that private Channel because Slack did not "
                "recognise this app in the Channel's Workspace. Click Refresh on the Home tab, "
                "then try again."
            ) from exc
        raise ConversationAccessError(
            f"SyncBot could not be added to that private Channel (`{error or 'unknown error'}`)."
        ) from exc


def authorize_url(team_id: str | None = None, context: dict | None = None) -> str | None:
    """Return this instance's ``/slack/install`` URL for the current user.

    Bolt's callback checks a browser cookie that only ``/slack/install`` sets.
    Linking straight at Slack's authorize URL (even with a ``state`` from the
    database) comes back as ``invalid_browser`` after Allow. Returns ``None``
    when there is no public origin to point at, so the Home tab hides the
    button rather than rendering a dead link.

    The origin comes from :func:`helpers.oauth.get_public_base_url` (this
    request's Host, or one remembered from an earlier Slack request).
    *team_id* is passed through as ``team`` so Slack pre-selects this
    workspace; most people belong to several and the screen otherwise
    defaults to whichever one the browser used last.
    """
    client_id = os.environ.get(constants.SLACK_CLIENT_ID, "").strip()
    if not client_id:
        return None

    from helpers.oauth import get_public_base_url

    base = get_public_base_url(context)
    if not base:
        return None

    install_url = f"{base}/slack/install"
    if team_id:
        install_url += "?" + urllib.parse.urlencode({"team": team_id})
    return install_url
