"""Per-workspace policy stored in the workspace_settings table.

Workspace fields (extra managers, private channels) are edited from the Settings
modal on any workspace where the acting user is a Slack admin or owner.
Instance-wide fields stay in ``helpers.settings``.
"""

import json
import logging
import os
from datetime import UTC, datetime

import constants
from db import DbManager, schemas
from helpers._cache import _cache_delete, _cache_get, _cache_set
from helpers.export_import import invalidate_home_tab_caches_for_team

_logger = logging.getLogger(__name__)

_SENTINEL_MISSING = "\x00__missing__"


def _cache_key(workspace_id: int, key: str) -> str:
    return f"workspace_setting:{workspace_id}:{key}"


def _workspace_record_for_team(team_id: str | None) -> schemas.Workspace | None:
    if not team_id:
        return None
    rows = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.team_id == team_id,
            schemas.Workspace.deleted_at.is_(None),
        ],
    )
    for workspace in rows:
        if workspace.bot_token:
            return workspace
    return rows[0] if rows else None


def _workspace_id_for_team(team_id: str | None) -> int | None:
    workspace = _workspace_record_for_team(team_id)
    return workspace.id if workspace else None


def get_raw_workspace_setting(workspace_id: int, key: str) -> str | None:
    """Return the stored value for *workspace_id*/*key*, or None if there is no row."""
    cached = _cache_get(_cache_key(workspace_id, key))
    if cached is not None:
        return None if cached == _SENTINEL_MISSING else cached

    rows = DbManager.find_records(
        schemas.WorkspaceSetting,
        [
            schemas.WorkspaceSetting.workspace_id == workspace_id,
            schemas.WorkspaceSetting.key == key,
        ],
    )
    value = rows[0].value if rows else None
    _cache_set(_cache_key(workspace_id, key), _SENTINEL_MISSING if value is None else value)
    return value


def set_workspace_setting(workspace_id: int, key: str, value: str | None, *, team_id: str | None = None) -> None:
    """Write *key* for *workspace_id* and invalidate its cache entry."""
    now = datetime.now(UTC)
    existing = DbManager.find_records(
        schemas.WorkspaceSetting,
        [
            schemas.WorkspaceSetting.workspace_id == workspace_id,
            schemas.WorkspaceSetting.key == key,
        ],
    )
    if existing:
        DbManager.update_records(
            schemas.WorkspaceSetting,
            [
                schemas.WorkspaceSetting.workspace_id == workspace_id,
                schemas.WorkspaceSetting.key == key,
            ],
            {schemas.WorkspaceSetting.value: value, schemas.WorkspaceSetting.updated_at: now},
        )
    else:
        DbManager.create_record(
            schemas.WorkspaceSetting(
                workspace_id=workspace_id,
                key=key,
                value=value,
                updated_at=now,
            )
        )

    _cache_delete(_cache_key(workspace_id, key))
    if team_id:
        invalidate_home_tab_caches_for_team(team_id)
    _logger.info("workspace_setting_saved", extra={"workspace_id": workspace_id, "setting_key": key})


_ALLOW_PRIVATE_ENV_WARNED = False


def _warn_leftover_allow_private_env() -> None:
    global _ALLOW_PRIVATE_ENV_WARNED
    if _ALLOW_PRIVATE_ENV_WARNED:
        return
    raw = os.environ.get(constants.ALLOW_PRIVATE_CHANNELS)
    if raw is None or raw.strip() == "":
        return
    _ALLOW_PRIVATE_ENV_WARNED = True
    _logger.warning(
        "%s is ignored; set Allow private Channels in the SyncBot Settings modal instead",
        constants.ALLOW_PRIVATE_CHANNELS,
    )


def allow_private_channels(team_id: str | None) -> bool:
    """Whether private channels may be selected in *team_id*'s workspace.

    Defaults to false for workspaces with no saved row.
    """
    _warn_leftover_allow_private_env()
    workspace_id = _workspace_id_for_team(team_id)
    if workspace_id is None:
        return constants.DEFAULT_ALLOW_PRIVATE_CHANNELS
    raw = get_raw_workspace_setting(workspace_id, constants.SETTING_ALLOW_PRIVATE_CHANNELS)
    if raw is None:
        return constants.DEFAULT_ALLOW_PRIVATE_CHANNELS
    normalized = raw.strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    _logger.warning(
        "workspace_setting_unparseable",
        extra={"workspace_id": workspace_id, "setting_key": constants.SETTING_ALLOW_PRIVATE_CHANNELS},
    )
    return constants.DEFAULT_ALLOW_PRIVATE_CHANNELS


def extra_manager_user_ids(team_id: str | None) -> list[str]:
    """Return extra manager user IDs configured for *team_id*'s workspace."""
    workspace_id = _workspace_id_for_team(team_id)
    if workspace_id is None:
        return []
    raw = get_raw_workspace_setting(workspace_id, constants.SETTING_EXTRA_MANAGER_USER_IDS)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning(
            "workspace_setting_unparseable",
            extra={"workspace_id": workspace_id, "setting_key": constants.SETTING_EXTRA_MANAGER_USER_IDS},
        )
        return []
    if not isinstance(parsed, list):
        return []
    return [user_id for user_id in parsed if isinstance(user_id, str) and user_id.startswith("U")]


def set_extra_manager_user_ids(team_id: str, user_ids: list[str]) -> None:
    """Persist the extra-manager list for *team_id*'s workspace."""
    workspace_id = _workspace_id_for_team(team_id)
    if workspace_id is None:
        return
    filtered = [user_id for user_id in user_ids if isinstance(user_id, str) and user_id.startswith("U")]
    set_workspace_setting(
        workspace_id,
        constants.SETTING_EXTRA_MANAGER_USER_IDS,
        json.dumps(sorted(set(filtered))),
        team_id=team_id,
    )
