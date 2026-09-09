"""Application constants and startup configuration validation.

This module defines:
1) environment-variable *name* constants, and
2) derived runtime flags computed from ``os.environ``.

It also provides :func:`validate_config` to fail fast on missing
configuration at startup.
"""

import logging
import os

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-variable name constants
#
# Each value is the *name* of the env var, not its value.  The actual values
# are read from os.environ at runtime.
# ---------------------------------------------------------------------------

SLACK_BOT_TOKEN = "SLACK_BOT_TOKEN"
SLACK_CLIENT_ID = "SLACK_CLIENT_ID"
SLACK_CLIENT_SECRET = "SLACK_CLIENT_SECRET"
SLACK_BOT_SCOPES = "SLACK_BOT_SCOPES"
SLACK_USER_SCOPES = "SLACK_USER_SCOPES"
SLACK_SIGNING_SECRET = "SLACK_SIGNING_SECRET"
DATA_ENCRYPTION_KEY = "DATA_ENCRYPTION_KEY"
_DATA_ENCRYPTION_KEY_LEGACY = "TOKEN_ENCRYPTION_KEY"
REQUIRE_ADMIN = "REQUIRE_ADMIN"

# Database: backend-agnostic (postgresql, mysql, or sqlite)
DATABASE_BACKEND = "DATABASE_BACKEND"
DATABASE_URL = "DATABASE_URL"

# Network SQL backends (used when DATABASE_URL is unset)
DATABASE_HOST = "DATABASE_HOST"
DATABASE_PORT = "DATABASE_PORT"
DATABASE_USER = "DATABASE_USER"
DATABASE_PASSWORD = "DATABASE_PASSWORD"
DATABASE_SCHEMA = "DATABASE_SCHEMA"
DATABASE_SSL_CA_PATH = "DATABASE_SSL_CA_PATH"
DATABASE_TLS_ENABLED = "DATABASE_TLS_ENABLED"

# Slack Team ID of the primary workspace (backup/restore and DB reset when enabled).
PRIMARY_WORKSPACE = "PRIMARY_WORKSPACE"

# When "true"/"1"/"yes" and PRIMARY_WORKSPACE matches, show Reset Database on Home.
# Deliberately env-only: a destructive, irreversible action guarded by a two-key
# check (env var plus PRIMARY_WORKSPACE). A UI toggle would defeat that.
ENABLE_DB_RESET = "ENABLE_DB_RESET"

# ---------------------------------------------------------------------------
# Operational policy — stored in the Settings modal (instance_settings table)
#
# These are not environment variables. If a leftover env var with the matching
# name is still set, helpers.settings logs a warning and ignores it.
# ---------------------------------------------------------------------------

# Setting keys as stored in the instance_settings table.
SETTING_ALLOW_PRIVATE_CHANNELS = "allow_private_channels"
SETTING_EXTRA_MANAGER_USER_IDS = "extra_manager_user_ids"
SETTING_BROADCAST_ALLOWED_WORKSPACES = "broadcast_allowed_workspaces"
SETTING_SOFT_DELETE_RETENTION_DAYS = "soft_delete_retention_days"
SETTING_FEDERATION_ENABLED = "federation_enabled"
# Internal: last public origin from an incoming request Host. Not a Settings field.
SETTING_PUBLIC_BASE_URL = "public_base_url"

# Names that used to be env vars. Kept so leftover deploy config can be warned
# about, not so they are read.
ALLOW_PRIVATE_CHANNELS = "ALLOW_PRIVATE_CHANNELS"
BROADCAST_ALLOWED_WORKSPACES = "BROADCAST_ALLOWED_WORKSPACES"
SOFT_DELETE_RETENTION_DAYS_VAR = "SOFT_DELETE_RETENTION_DAYS"
SYNCBOT_FEDERATION_ENABLED = "SYNCBOT_FEDERATION_ENABLED"

DEFAULT_ALLOW_PRIVATE_CHANNELS = False
DEFAULT_BROADCAST_ALLOWED_WORKSPACES: list[str] = []
DEFAULT_SOFT_DELETE_RETENTION_DAYS = 30
DEFAULT_FEDERATION_ENABLED = False

# Leftover column on sync_channels.reaction_direction. Runtime ignores it;
# export/import still round-trips values for database restores.
REACTION_DIRECTION_BOTH = "both"
REACTION_DIRECTION_SEND = "send"
REACTION_DIRECTION_RECEIVE = "receive"
REACTION_DIRECTION_OFF = "off"

# Per-channel reaction type while the channel subscribes.
REACTION_STYLE_DIRECT_ONLY = "direct_only"
REACTION_STYLE_THREADED_AND_DIRECT = "threaded_and_direct"
REACTION_STYLE_OFF = "off"

DEFAULT_REACTION_DIRECTION = REACTION_DIRECTION_BOTH
DEFAULT_REACTION_STYLE_EXISTING = REACTION_STYLE_THREADED_AND_DIRECT
DEFAULT_REACTION_STYLE_NEW_RECEIVE = REACTION_STYLE_THREADED_AND_DIRECT

POST_META_KIND_MESSAGE = "message"
POST_META_KIND_REACTION_NOTICE = "reaction_notice"
NOTICE_TREE_MAX_DEPTH = 10

# ---------------------------------------------------------------------------
# Derived runtime flags / computed values
# ---------------------------------------------------------------------------

LOCAL_DEVELOPMENT = os.environ.get("LOCAL_DEVELOPMENT", "false").lower() == "true"

_BOT_TOKEN_PLACEHOLDER = "xoxb-0-0"


def _has_real_bot_token() -> bool:
    """Return *True* if SLACK_BOT_TOKEN looks like a genuine Slack token."""
    token = os.environ.get(SLACK_BOT_TOKEN, "").strip()
    return token.startswith("xoxb-") and token != _BOT_TOKEN_PLACEHOLDER


HAS_REAL_BOT_TOKEN: bool = _has_real_bot_token()

WARNING_BLOCK = "WARNING_BLOCK"

# ---------------------------------------------------------------------------
# User-mapping TTLs (seconds)
#
# How long a cached mapping is considered "fresh" before re-checking.
# Manual mappings never expire and can only be removed via the admin UI.
# ---------------------------------------------------------------------------

USER_MAP_TTL_EMAIL = 30 * 24 * 3600  # 30 days for email-confirmed mappings
USER_MAP_TTL_NAME = 14 * 24 * 3600  # 14 days for name-based mappings
USER_MAP_TTL_NONE = 90 * 24 * 3600  # 90 days for no-map (team_join handles re-checks)
USER_DIR_REFRESH_TTL = 24 * 3600  # 24 hours per workspace directory refresh
USER_MAPPING_PAGE_SIZE = 20  # max mapping rows per modal page (Slack 100-block cap)

# Refresh button cooldown (seconds) when content hash unchanged
REFRESH_COOLDOWN_SECONDS = 60

# ---------------------------------------------------------------------------
# Federation
# ---------------------------------------------------------------------------

# Leftover: ignored. Instance id is SHA-256 of the raw Ed25519 public key.
SYNCBOT_INSTANCE_ID = "SYNCBOT_INSTANCE_ID"
# Leftover: ignored. Public origin comes from incoming Slack request Host.
SYNCBOT_PUBLIC_URL = "SYNCBOT_PUBLIC_URL"

# This instance's federation HTTP mount point. The connection code advertises
# <public origin> + this path as the peer's webhook_url; peers append resource
# subpaths (for example /message, /pair) to whatever URL the code carried. Only
# this instance's own routing and code generation reference the mount path — the
# outbound client never assumes it, so a future instance can serve elsewhere.
FEDERATION_API_BASE_PATH = "/api/federation"


# ---------------------------------------------------------------------------
# Startup configuration validation
#
# Validates that all required environment variables are set before the app
# handles any requests.  Fails fast in production; warns in local dev.
# ---------------------------------------------------------------------------


def get_database_backend() -> str:
    """Return ``postgresql``, ``mysql``, or ``sqlite``.

    Defaults to ``mysql`` when unset.
    """
    return os.environ.get(DATABASE_BACKEND, "mysql").lower().strip() or "mysql"


def _env_bool(name: str, default: bool) -> bool:
    """Parse common boolean env values with a safe default."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def database_tls_enabled() -> bool:
    """Return True when MySQL/PostgreSQL TLS should be used.

    Defaults:
    - local dev: disabled
    - non-local: enabled
    Can be overridden with DATABASE_TLS_ENABLED=true/false.
    """
    default = not LOCAL_DEVELOPMENT
    return _env_bool(DATABASE_TLS_ENABLED, default)


def database_ssl_ca_path() -> str:
    """Return CA bundle path for DB TLS verification, or empty string for system defaults.

    If :envvar:`DATABASE_SSL_CA_PATH` is set, that path is returned as-is (caller may
    verify it exists). Otherwise the first existing file among common OS locations
    is used (Amazon Linux, Debian, Alpine).
    """
    explicit = os.environ.get(DATABASE_SSL_CA_PATH, "").strip()
    if explicit:
        return explicit
    for candidate in (
        "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL / Amazon Linux / Lambda
        "/etc/ssl/certs/ca-certificates.crt",  # Debian / Ubuntu / Cloud Run image
        "/etc/ssl/cert.pem",  # Alpine / macOS
    ):
        if os.path.isfile(candidate):
            return candidate
    return ""


def get_required_db_vars() -> list:
    """Return list of required env var names for the current database backend."""
    backend = get_database_backend()
    if backend == "sqlite":
        return [DATABASE_URL]
    # mysql / postgresql: require URL or host/user/password/schema
    if os.environ.get(DATABASE_URL):
        return []  # URL is enough
    return [
        DATABASE_HOST,
        DATABASE_USER,
        DATABASE_PASSWORD,
        DATABASE_SCHEMA,
    ]


# Required in all environments (non-DB vars; DB vars are backend-dependent)
_REQUIRED_ALWAYS_NON_DB: list = []

# Required only in production (non-local deployments).
_REQUIRED_PRODUCTION = [
    SLACK_SIGNING_SECRET,
    SLACK_CLIENT_ID,
    SLACK_CLIENT_SECRET,
    SLACK_BOT_SCOPES,
    DATA_ENCRYPTION_KEY,
]


# Minimum length for DATA_ENCRYPTION_KEY in production (reject weak/placeholder values).
_DATA_ENCRYPTION_KEY_MIN_LEN = 16
_DATA_ENCRYPTION_KEY_PLACEHOLDERS = frozenset({"123", "changeme", "secret", "password"})
_TOKEN_ENCRYPTION_KEY_WARNED = False


def _warn_token_encryption_key_leftover() -> None:
    """Warn once per process when the leftover TOKEN_ENCRYPTION_KEY env is set."""
    global _TOKEN_ENCRYPTION_KEY_WARNED
    if _TOKEN_ENCRYPTION_KEY_WARNED:
        return
    raw = os.environ.get(_DATA_ENCRYPTION_KEY_LEGACY)
    if raw is None or str(raw).strip() == "":
        return
    _TOKEN_ENCRYPTION_KEY_WARNED = True
    _logger.warning(
        "%s is deprecated; set %s instead (still used when %s is unset)",
        _DATA_ENCRYPTION_KEY_LEGACY,
        DATA_ENCRYPTION_KEY,
        DATA_ENCRYPTION_KEY,
    )


def _encryption_active() -> bool:
    """Return True if data encryption is configured with a strong key.

    Checks DATA_ENCRYPTION_KEY first, then legacy TOKEN_ENCRYPTION_KEY.
    In non-local environments the key must be set, at least _DATA_ENCRYPTION_KEY_MIN_LEN
    characters, and not a known placeholder. Local dev can use any value or leave unset.
    """
    _warn_token_encryption_key_leftover()
    key = (os.environ.get(DATA_ENCRYPTION_KEY) or os.environ.get(_DATA_ENCRYPTION_KEY_LEGACY) or "").strip()
    if not key or len(key) < _DATA_ENCRYPTION_KEY_MIN_LEN:
        return False
    return key.lower() not in _DATA_ENCRYPTION_KEY_PLACEHOLDERS


def validate_config() -> None:
    """Check that required environment variables are present.

    In production this raises immediately so the Lambda fails on cold-start
    rather than silently misbehaving.  In local development it only warns.
    DB requirements depend on DATABASE_BACKEND (postgresql, mysql, or sqlite).
    """
    _warn_token_encryption_key_leftover()
    required = list(_REQUIRED_ALWAYS_NON_DB) + list(get_required_db_vars())
    if not LOCAL_DEVELOPMENT:
        required.extend(_REQUIRED_PRODUCTION)

    missing = [var for var in required if not os.environ.get(var)]

    if missing:
        msg = "Missing required environment variable(s): " + ", ".join(missing)
        if LOCAL_DEVELOPMENT:
            _logger.warning(msg + " (continuing in local-dev mode)")
        else:
            _logger.critical(msg)
            raise OSError(msg)

    if not LOCAL_DEVELOPMENT and not _encryption_active():
        msg = (
            "DATA_ENCRYPTION_KEY is required in production and must be a secure, random value "
            f"(at least {_DATA_ENCRYPTION_KEY_MIN_LEN} characters). "
            "Use your provider's secret manager; the deploy script auto-generates it. "
            "Back up the key after first deploy. In local dev you may set it manually or leave unset."
        )
        _logger.critical(msg)
        raise OSError(msg)
