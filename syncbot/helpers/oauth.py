"""Slack OAuth flow construction and this instance's public base URL.

Bot scopes: :envvar:`SLACK_BOT_SCOPES` (``slack_manifest_scopes.BOT_SCOPES`` / manifest bot).
User scopes: :envvar:`SLACK_USER_SCOPES` (defaults to ``USER_SCOPES`` when unset).
Requesting user scopes that do not match the Slack app manifest causes ``invalid_scope`` on install.
"""

import logging
import os

from slack_bolt.oauth import OAuthFlow
from slack_bolt.oauth.callback_options import CallbackOptions, FailureArgs, SuccessArgs
from slack_bolt.oauth.oauth_settings import OAuthSettings
from slack_sdk.oauth.state_store.sqlalchemy import SQLAlchemyOAuthStateStore

import constants
from helpers._cache import _cache_get, _cache_set
from helpers.encryption_installation_store import EncryptedSQLAlchemyInstallationStore
from slack_manifest_scopes import USER_SCOPES

_logger = logging.getLogger(__name__)

_OAUTH_STATE_EXPIRATION_SECONDS = 600
_PUBLIC_BASE_CACHE_KEY = "public_base_url"
_LEGACY_PUBLIC_URL_WARNED = False


def get_oauth_flow():
    """Build the Slack OAuth flow using SQLAlchemy-backed stores.

    Uses the same database engine as the rest of the app. Works for both
    local development and production (Lambda). If OAuth credentials are not
    set and LOCAL_DEVELOPMENT is true, returns None (single-workspace mode).
    """
    client_id = os.environ.get(constants.SLACK_CLIENT_ID, "").strip()
    client_secret = os.environ.get(constants.SLACK_CLIENT_SECRET, "").strip()
    scopes_raw = os.environ.get(constants.SLACK_BOT_SCOPES, "").strip()
    user_scopes_raw = os.environ.get(constants.SLACK_USER_SCOPES, "").strip()

    if constants.LOCAL_DEVELOPMENT and not (client_id and client_secret and scopes_raw):
        _logger.info("OAuth credentials not set — running in single-workspace mode")
        return None

    from db import get_engine

    engine = get_engine()
    installation_store = EncryptedSQLAlchemyInstallationStore(
        client_id=client_id,
        engine=engine,
    )
    _skip_empty_user_installations(installation_store)
    state_store = SQLAlchemyOAuthStateStore(
        expiration_seconds=_OAUTH_STATE_EXPIRATION_SECONDS,
        engine=engine,
    )

    bot_scopes = [s.strip() for s in scopes_raw.split(",") if s.strip()]
    user_scopes = [s.strip() for s in user_scopes_raw.split(",") if s.strip()] if user_scopes_raw else list(USER_SCOPES)

    return OAuthFlow(
        settings=OAuthSettings(
            client_id=client_id,
            client_secret=client_secret,
            scopes=bot_scopes,
            user_scopes=user_scopes,
            installation_store=installation_store,
            state_store=state_store,
            # Immediate 302 to Slack so a Home-tab URL button does not land on
            # Bolt's HTML install page. That page also triggered a favicon GET
            # which Lambda treated as a second /slack/install and overwrote the
            # OAuth state cookie (invalid_browser after Allow).
            install_page_rendering_enabled=False,
            callback_options=CallbackOptions(success=_oauth_success, failure=_oauth_failure),
        ),
    )


def _header_first(headers: dict | None, *names: str) -> str | None:
    """Return the first non-empty header value, case-insensitive."""
    if not headers:
        return None
    lowered = {str(key).lower(): value for key, value in headers.items()}
    for name in names:
        raw = lowered.get(name)
        if raw is None:
            continue
        if isinstance(raw, list | tuple):
            raw = raw[0] if raw else None
        text = str(raw).split(",")[0].strip() if raw else ""
        if text:
            return text
    return None


def _origin_from_host(host: str | None, headers: dict | None) -> str | None:
    """Build ``https://host`` (or http in local dev) from a hostname."""
    host = (host or "").strip()
    if not host:
        return None
    proto = _header_first(headers, "x-forwarded-proto")
    if proto not in ("http", "https"):
        proto = "http" if constants.LOCAL_DEVELOPMENT else "https"
    return f"{proto}://{host}"


def public_base_from_headers(headers: dict | None) -> str | None:
    """Return this request's public origin (Function URL / Cloud Run Host).

    Slack's Event and Interactivity URL is this same origin, so Authorize and
    federation both use it instead of a separate ``SYNCBOT_PUBLIC_URL``.
    """
    host = _header_first(headers, "x-forwarded-host", "host")
    return _origin_from_host(host, headers)


def public_base_from_lambda_event(event: dict | None) -> str | None:
    """Return the public origin from a Lambda Function URL / API Gateway event.

    Prefers Host / X-Forwarded-Host headers, then ``requestContext.domainName``
    (Function URL payload 2.0).
    """
    if not event:
        return None
    headers = event.get("headers") or {}
    from_headers = public_base_from_headers(headers)
    if from_headers:
        return from_headers
    request_context = event.get("requestContext") or {}
    domain = request_context.get("domainName")
    if isinstance(domain, str) and domain.strip():
        return _origin_from_host(domain.strip(), headers)
    return None


def remember_public_base(url: str | None) -> str | None:
    """Store *url* for later :func:`get_public_base_url` calls in this process and the DB."""
    base = (url or "").strip().rstrip("/")
    if not base:
        return None
    cached = _cache_get(_PUBLIC_BASE_CACHE_KEY)
    if cached == base:
        return base
    _cache_set(_PUBLIC_BASE_CACHE_KEY, base, ttl=86400)
    _persist_public_base(base)
    return base


def capture_public_base(headers: dict | None, context: dict | None = None) -> str | None:
    """Derive the public origin from *headers*, remember it, and optionally set *context*."""
    base = remember_public_base(public_base_from_headers(headers))
    if base and context is not None:
        context["public_base_url"] = base
    return base


def capture_public_base_from_lambda_event(event: dict | None, context: dict | None = None) -> str | None:
    """Remember the Function URL origin (Host header or ``requestContext.domainName``)."""
    base = remember_public_base(public_base_from_lambda_event(event))
    if base and context is not None:
        context["public_base_url"] = base
    return base


def get_public_base_url(context: dict | None = None) -> str | None:
    """Return this instance's public HTTPS origin (no trailing slash).

    Prefers the current Slack request (``context["public_base_url"]``), then
    the origin remembered from an earlier request on this warm container, then
    the last Host persisted in ``instance_settings``. ``SYNCBOT_PUBLIC_URL`` is
    ignored leftover deploy config.
    """
    _warn_legacy_public_url_env()
    if context:
        from_context = str(context.get("public_base_url") or "").strip().rstrip("/")
        if from_context:
            return from_context
    cached = _cache_get(_PUBLIC_BASE_CACHE_KEY)
    if isinstance(cached, str) and cached.strip():
        return cached.strip().rstrip("/")
    stored = _load_persisted_public_base()
    if stored:
        _cache_set(_PUBLIC_BASE_CACHE_KEY, stored, ttl=86400)
        return stored
    return None


def _persist_public_base(base: str) -> None:
    """Write the last Host to instance_settings when it changed (internal key)."""
    try:
        from helpers.settings import get_raw_setting, set_setting

        if get_raw_setting(constants.SETTING_PUBLIC_BASE_URL) == base:
            return
        set_setting(constants.SETTING_PUBLIC_BASE_URL, base)
    except Exception:
        _logger.debug("public_base_persist_failed", exc_info=True)


def _load_persisted_public_base() -> str | None:
    try:
        from helpers.settings import get_raw_setting

        raw = get_raw_setting(constants.SETTING_PUBLIC_BASE_URL)
    except Exception:
        return None
    if not isinstance(raw, str):
        return None
    base = raw.strip().rstrip("/")
    return base or None


def _warn_legacy_public_url_env() -> None:
    global _LEGACY_PUBLIC_URL_WARNED
    if _LEGACY_PUBLIC_URL_WARNED:
        return
    raw = os.environ.get(constants.SYNCBOT_PUBLIC_URL)
    if raw is None or raw.strip() == "":
        return
    _LEGACY_PUBLIC_URL_WARNED = True
    _logger.warning(
        "%s is ignored; SyncBot uses the Host of incoming Slack requests for /slack/install and federation instead",
        constants.SYNCBOT_PUBLIC_URL,
    )


def _oauth_success(args: SuccessArgs):
    """Publish Home for the installer so Authorize disappears without a manual Refresh."""
    try:
        refresh_home_after_oauth_install(args.installation)
    except Exception:
        _logger.exception("oauth_home_refresh_failed")
    return args.default.success(args)


def _oauth_failure(args: FailureArgs):
    return args.default.failure(args)


def _skip_empty_user_installations(store) -> None:
    """Make Bolt ignore a per-user row that has no user token left.

    After someone revokes Authorize SyncBot we drop their row. A leftover
    tokenless row still matches ``find_installation(user_id=...)``. Slack's
    store copies the workspace bot token onto that object, so checking
    ``bot_token`` is not enough — look at ``user_token``. Returning ``None``
    lets Bolt keep the workspace bot from ``find_bot`` / the latest install.
    """
    original = store.find_installation

    def find_installation(*args, **kwargs):
        inst = original(*args, **kwargs)
        if inst is None:
            return None
        if kwargs.get("user_id") and not getattr(inst, "user_token", None):
            return None
        return inst

    store.find_installation = find_installation


def refresh_home_after_oauth_install(installation) -> None:
    """Rebuild the authorizing user's Home tab after Bolt stores their install.

    Same ``views.publish`` path as a successful Refresh: the permission lists
    are hashed into the Home payload, so the Authorize section drops off once
    the new user token is in ``slack_installations``.
    """
    team_id = getattr(installation, "team_id", None)
    user_id = getattr(installation, "user_id", None)
    bot_token = getattr(installation, "bot_token", None)
    if not team_id or not user_id or not bot_token:
        _logger.warning(
            "oauth_home_refresh_skipped",
            extra={"has_team": bool(team_id), "has_user": bool(user_id), "has_bot_token": bool(bot_token)},
        )
        return

    from slack_sdk import WebClient

    import builders

    client = WebClient(token=bot_token)
    body = {"team": {"id": team_id}, "user": {"id": user_id}}
    builders.build_home_tab(body, client, _logger, {}, user_id=user_id)
