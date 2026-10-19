"""Application constants and startup configuration validation.

This module defines:
1) environment-variable *name* constants, and
2) derived runtime flags computed from ``os.environ``.

It also provides :func:`validate_config` to fail fast on missing
configuration at startup.
"""

import os

from logger import log_critical, log_warning

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
# Internal: last Home viewers for post-deploy views.publish. Not a Settings field.
SETTING_HOME_VIEWER_USER_IDS = "home_viewer_user_ids"
HOME_VIEWER_USER_IDS_CAP = 20
SETTING_SOFT_DELETE_RETENTION_DAYS = "soft_delete_retention_days"
SETTING_FEDERATION_ENABLED = "federation_enabled"
SETTING_WORKSPACE_BLOCK_LIST = "workspace_block_list"
# Internal: last public origin from an incoming request Host. Not a Settings field.
SETTING_PUBLIC_BASE_URL = "public_base_url"

# Names that used to be env vars. Kept so leftover deploy config can be warned
# about, not so they are read.
ALLOW_PRIVATE_CHANNELS = "ALLOW_PRIVATE_CHANNELS"
SOFT_DELETE_RETENTION_DAYS_VAR = "SOFT_DELETE_RETENTION_DAYS"
SYNCBOT_FEDERATION_ENABLED = "SYNCBOT_FEDERATION_ENABLED"

DEFAULT_ALLOW_PRIVATE_CHANNELS = False
DEFAULT_SOFT_DELETE_RETENTION_DAYS = 30
DEFAULT_FEDERATION_ENABLED = False

# Per-channel reaction type while the channel subscribes.
REACTION_STYLE_DIRECT_ONLY = "direct_only"
REACTION_STYLE_THREADED_AND_DIRECT = "threaded_and_direct"
REACTION_STYLE_OFF = "off"

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
USER_MAPPING_PAGE_SIZE = 20  # max mapping rows per modal page (Slack 100-block cap)

# ---------------------------------------------------------------------------
# Federation
# ---------------------------------------------------------------------------

# Leftover: ignored. Instance id is SHA-256 of the raw Ed25519 public key.
SYNCBOT_INSTANCE_ID = "SYNCBOT_INSTANCE_ID"
# Leftover: ignored. Public origin comes from incoming Slack request Host.
SYNCBOT_PUBLIC_URL = "SYNCBOT_PUBLIC_URL"

# This instance's federation HTTP mount point. The connection code includes
# <public origin> + this path as the peer's webhook_url; peers append resource
# subpaths (for example /message, /pair) to whatever URL the code carried. Only
# this instance's own routing and code generation reference the mount path — the
# outbound client never assumes it, so a future instance can serve elsewhere.
FEDERATION_API_BASE_PATH = "/api/federation"

# Leftover: ignored. Infra injects FEDERATION_HTTP_MAX_MB instead.
FILE_CHUNK_MB = "FILE_CHUNK_MB"
# Infra-injected hop HTTP receive cap in MiB (SAM/Terraform). Unset or 0: no app split.
FEDERATION_HTTP_MAX_MB = "FEDERATION_HTTP_MAX_MB"
# Mixed-version peers still enforce the old 1 MB app JSON cap.
LEGACY_FEDERATION_JSON_CHUNK_MB = 1

_FILE_CHUNK_MB_WARNED = False


def _warn_file_chunk_mb_leftover() -> None:
    """Warn once per process when leftover FILE_CHUNK_MB env is set."""
    global _FILE_CHUNK_MB_WARNED
    if _FILE_CHUNK_MB_WARNED:
        return
    raw = os.environ.get(FILE_CHUNK_MB)
    if raw is None or str(raw).strip() == "":
        return
    _FILE_CHUNK_MB_WARNED = True
    log_warning("legacy_env_ignored", env=FILE_CHUNK_MB)


def get_file_chunk_mb() -> int:
    """This hop's inbound federation HTTP cap in MiB.

    Reads ``FEDERATION_HTTP_MAX_MB`` from infra. ``0`` or unset means this
    process does not split. Slack files stay 1 GB. Leftover ``FILE_CHUNK_MB``
    is ignored.
    """
    _warn_file_chunk_mb_leftover()
    raw = os.environ.get(FEDERATION_HTTP_MAX_MB)
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        mb = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return mb if mb > 0 else 0


def file_chunk_bytes() -> int | None:
    """Return max part size in bytes, or ``None`` when unlimited."""
    mb = get_file_chunk_mb()
    if mb == 0:
        return None
    return mb * 1024 * 1024


def json_chunk_mb() -> int:
    """Advertise this hop's JSON receive split. ``0`` means no app split."""
    return get_file_chunk_mb()


def federation_json_max_bytes() -> int | None:
    """Max federation JSON body this process will accept, or ``None`` when unlimited."""
    mb = get_file_chunk_mb()
    if mb <= 0:
        return None
    return mb * 1024 * 1024


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


# Keep in lockstep with [tool.poetry] version. python-semantic-release updates both.
__version__ = "1.7.1"


def app_version() -> str:
    """Package version from this module (deployed copies of source do not install the wheel)."""
    return __version__


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
    log_warning("legacy_env_ignored", env=_DATA_ENCRYPTION_KEY_LEGACY, use=DATA_ENCRYPTION_KEY)


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

    In production this raises immediately so the process fails at start
    rather than silently misbehaving.  In local development it only warns.
    DB requirements depend on DATABASE_BACKEND (postgresql, mysql, or sqlite).
    """
    _warn_token_encryption_key_leftover()
    _warn_file_chunk_mb_leftover()
    required = list(_REQUIRED_ALWAYS_NON_DB) + list(get_required_db_vars())
    if not LOCAL_DEVELOPMENT:
        required.extend(_REQUIRED_PRODUCTION)

    missing = [var for var in required if not os.environ.get(var)]

    if missing:
        msg = "Missing required environment variable(s): " + ", ".join(missing)
        if LOCAL_DEVELOPMENT:
            log_warning("config_missing_env", missing=", ".join(missing))
        else:
            log_critical("config_missing_env", missing=", ".join(missing))
            raise OSError(msg)

    if not LOCAL_DEVELOPMENT and not _encryption_active():
        msg = (
            "DATA_ENCRYPTION_KEY is required in production and must be a secure, random value "
            f"(at least {_DATA_ENCRYPTION_KEY_MIN_LEN} characters). "
            "Use your provider's secret manager; the deploy script auto-generates it. "
            "Back up the key after first deploy. In local dev you may set it manually or leave unset."
        )
        log_critical("config_encryption_required")
        raise OSError(msg)
