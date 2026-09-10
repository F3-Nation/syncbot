"""Database-backed instance settings.

Resolution is **database, then a hardcoded default**. Environment variables for
these keys are ignored: they belong in the Settings modal, and a leftover env
value must not quietly override what an operator saved (or the default). When
one is still set, a warning is logged once per process.

Only operational policy belongs here — things that change over a deployment's
life and benefit from a UI. Secrets, connection details, and break-glass
switches stay in environment variables, because a UI toggle would either leak
them or defeat the guard they provide.

Imports submodules only (``db``, ``db.schemas``, ``helpers._cache``), per the
import-direction constraint documented in ``helpers/sync_cleanup.py``.
"""

import logging
import os
from datetime import UTC, datetime

import constants
from db import DbManager, schemas
from helpers._cache import _cache_delete, _cache_get, _cache_set

_logger = logging.getLogger(__name__)

_TRUTHY = ("true", "1", "yes", "on")
_FALSY = ("false", "0", "no", "off")

_SENTINEL_MISSING = "\x00__missing__"

# Env vars that used to seed these settings. Still recognized so a leftover
# deploy config logs a warning instead of silently changing behavior.
_IGNORED_ENV_BY_SETTING = {
    constants.SETTING_BROADCAST_ALLOWED_WORKSPACES: constants.BROADCAST_ALLOWED_WORKSPACES,
    constants.SETTING_SOFT_DELETE_RETENTION_DAYS: constants.SOFT_DELETE_RETENTION_DAYS_VAR,
    constants.SETTING_FEDERATION_ENABLED: constants.SYNCBOT_FEDERATION_ENABLED,
}
_IGNORED_ENV_WARNED: set[str] = set()


def _cache_key(key: str) -> str:
    return f"setting:{key}"


def _warn_ignored_env(setting_key: str) -> None:
    env_var = _IGNORED_ENV_BY_SETTING.get(setting_key)
    if not env_var or env_var in _IGNORED_ENV_WARNED:
        return
    raw = os.environ.get(env_var)
    if raw is None or raw.strip() == "":
        return
    _IGNORED_ENV_WARNED.add(env_var)
    _logger.warning(
        "%s is ignored; set this in the SyncBot Settings modal instead",
        env_var,
    )


def get_raw_setting(key: str) -> str | None:
    """Return the stored database value for *key*, or None if there is no row.

    Cached per process, like ``sync_list``. On Lambda each warm container holds
    its own copy, so the TTL bounds staleness rather than removing it; the
    Settings modal invalidates on save within its own container.
    """
    cached = _cache_get(_cache_key(key))
    if cached is not None:
        return None if cached == _SENTINEL_MISSING else cached

    rows = DbManager.find_records(schemas.InstanceSetting, [schemas.InstanceSetting.key == key])
    value = rows[0].value if rows else None
    _cache_set(_cache_key(key), _SENTINEL_MISSING if value is None else value)
    return value


def set_setting(key: str, value: str | None) -> None:
    """Write *key* and invalidate its cache entry."""
    now = datetime.now(UTC)
    existing = DbManager.find_records(schemas.InstanceSetting, [schemas.InstanceSetting.key == key])
    if existing:
        DbManager.update_records(
            schemas.InstanceSetting,
            [schemas.InstanceSetting.key == key],
            {schemas.InstanceSetting.value: value, schemas.InstanceSetting.updated_at: now},
        )
    else:
        DbManager.create_record(schemas.InstanceSetting(key=key, value=value, updated_at=now))

    _cache_delete(_cache_key(key))
    _logger.info("instance_setting_saved", extra={"setting_key": key})


def _resolve(key: str) -> str | None:
    """Return the database value for *key*, warning if a leftover env var is set."""
    _warn_ignored_env(key)
    return get_raw_setting(key)


def get_bool_setting(key: str, default: bool) -> bool:
    """Resolve *key* as a boolean."""
    raw = _resolve(key)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    _logger.warning("instance_setting_unparseable", extra={"setting_key": key, "expected": "bool"})
    return default


def get_int_setting(key: str, default: int) -> int:
    """Resolve *key* as an integer."""
    raw = _resolve(key)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        _logger.warning("instance_setting_unparseable", extra={"setting_key": key, "expected": "int"})
        return default


def get_list_setting(key: str, default: list[str] | None = None) -> list[str]:
    """Resolve *key* as a comma-separated list, matching the SLACK_BOT_SCOPES idiom."""
    raw = _resolve(key)
    if raw is None:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Typed accessors for instance settings
# ---------------------------------------------------------------------------


def broadcast_allowed_workspaces() -> list[str]:
    """Slack team IDs permitted to publish a broadcast. Empty means any installed workspace."""
    return get_list_setting(
        constants.SETTING_BROADCAST_ALLOWED_WORKSPACES,
        constants.DEFAULT_BROADCAST_ALLOWED_WORKSPACES,
    )


def soft_delete_retention_days() -> int:
    """Days a soft-deleted workspace is retained before the purge removes it permanently."""
    return get_int_setting(
        constants.SETTING_SOFT_DELETE_RETENTION_DAYS,
        constants.DEFAULT_SOFT_DELETE_RETENTION_DAYS,
    )


def may_publish_broadcast(team_id: str | None) -> bool:
    """Whether *team_id* may publish a broadcast under the current allow-list."""
    allowed = broadcast_allowed_workspaces()
    if not allowed:
        return True
    return (team_id or "") in allowed


def federation_enabled() -> bool:
    """Whether external federation connections are enabled for this instance.

    Defaults to false on a new install. When there is no database row yet, a
    leftover ``SYNCBOT_FEDERATION_ENABLED=true`` env value is seeded once so
    upgrades keep federation on; after that the Settings modal is authoritative.
    """
    _warn_ignored_env(constants.SETTING_FEDERATION_ENABLED)
    raw = get_raw_setting(constants.SETTING_FEDERATION_ENABLED)
    if raw is not None:
        return get_bool_setting(
            constants.SETTING_FEDERATION_ENABLED,
            constants.DEFAULT_FEDERATION_ENABLED,
        )
    env_val = (os.environ.get(constants.SYNCBOT_FEDERATION_ENABLED) or "").strip().lower()
    if env_val == "true":
        set_setting(constants.SETTING_FEDERATION_ENABLED, "true")
        return True
    return constants.DEFAULT_FEDERATION_ENABLED
