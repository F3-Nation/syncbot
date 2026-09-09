"""Cross-workspace user mapping and mention resolution."""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from sqlalchemy import func

import constants
from db import DbManager, schemas
from helpers._cache import _CACHE, _USER_INFO_CACHE_TTL, _cache_get, _cache_set
from helpers.core import code_ticked_display_name, safe_get
from helpers.encryption import decrypt_bot_token
from helpers.slack_api import _users_info, get_user_info, slack_retry
from helpers.workspace import (
    get_workspace_by_id,
    resolve_workspace_name,
)

_logger = logging.getLogger(__name__)


def _get_user_profile(client: WebClient, user_id: str) -> dict[str, Any] | None:
    """Fetch a single user's profile with caching and retry."""
    from helpers.slack_api import _token_fingerprint

    fingerprint = _token_fingerprint(client)
    cache_key = f"user_profile:{fingerprint}:{user_id}" if fingerprint else f"user_profile:{user_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        res = _users_info(client, user_id)
    except SlackApiError as exc:
        _logger.warning(f"Failed to look up user {user_id}: {exc}")
        return None

    profile = safe_get(res, "user", "profile") or {}
    user_name = profile.get("display_name") or profile.get("real_name") or user_id
    email = profile.get("email")

    result: dict[str, Any] = {"user_name": user_name, "email": email}
    _cache_set(cache_key, result, ttl=_USER_INFO_CACHE_TTL)
    return result


def _normalize_name(display_name: str) -> str:
    """Trim trailing title/qualifier from a display name (e.g. drop text in parens or after dash)."""
    name = re.split(r"\s+[\(\-]", display_name or "")[0]
    return name.strip()


def normalize_display_name(name: str | None) -> str:
    """Return display name with trailing paren/dash qualifiers stripped; fallback to original if empty."""
    if not name:
        return name or ""
    n = _normalize_name(name)
    return n if n else name


def _map_ttl(method: str) -> int:
    """Return the TTL in seconds for a given mapping method."""
    if method == "manual":
        return 0
    if method == "email":
        return constants.USER_MAP_TTL_EMAIL
    if method == "name":
        return constants.USER_MAP_TTL_NAME
    return constants.USER_MAP_TTL_NONE


def _is_mapping_fresh(mapping: schemas.UserMapping) -> bool:
    """Return True if a cached mapping is still within its TTL."""
    if mapping.map_method == "manual":
        return True
    ttl = _map_ttl(mapping.map_method)
    age = (datetime.now(UTC) - mapping.mapped_at.replace(tzinfo=UTC)).total_seconds()
    return age < ttl


@slack_retry
def _users_list_page(client: WebClient, cursor: str = "") -> dict:
    """Fetch one page of users.list (with retry on rate-limit)."""
    return client.users_list(limit=200, cursor=cursor)


def _upsert_single_user_to_directory(member: dict, workspace_id: int) -> None:
    """Insert or update a single user in the directory and propagate name changes.

    If the user is deactivated (``member["deleted"] == True``), their
    directory entry is soft-deleted and all associated user mappings are
    removed.
    """
    profile = member.get("profile", {})
    display_name = profile.get("display_name") or ""
    real_name = profile.get("real_name") or ""
    email = profile.get("email")
    now = datetime.now(UTC)
    current_name = display_name or real_name
    is_deleted = member.get("deleted", False)

    existing = DbManager.find_records(
        schemas.UserDirectory,
        [
            schemas.UserDirectory.workspace_id == workspace_id,
            schemas.UserDirectory.slack_user_id == member["id"],
        ],
    )

    if is_deleted:
        if existing:
            DbManager.update_records(
                schemas.UserDirectory,
                [schemas.UserDirectory.id == existing[0].id],
                {schemas.UserDirectory.deleted_at: now, schemas.UserDirectory.updated_at: now},
            )
        _purge_mappings_for_user(member["id"], workspace_id)
        _CACHE.pop(f"user_info:{member['id']}", None)
        return

    if existing:
        DbManager.update_records(
            schemas.UserDirectory,
            [schemas.UserDirectory.id == existing[0].id],
            {
                schemas.UserDirectory.email: email,
                schemas.UserDirectory.real_name: real_name,
                schemas.UserDirectory.display_name: display_name,
                schemas.UserDirectory.normalized_name: _normalize_name(display_name)
                if display_name
                else _normalize_name(real_name),
                schemas.UserDirectory.updated_at: now,
                schemas.UserDirectory.deleted_at: None,
            },
        )
    else:
        DbManager.create_record(
            schemas.UserDirectory(
                workspace_id=workspace_id,
                slack_user_id=member["id"],
                email=email,
                real_name=real_name,
                display_name=display_name,
                normalized_name=_normalize_name(display_name) if display_name else _normalize_name(real_name),
                updated_at=now,
            )
        )

    if current_name:
        mappings = DbManager.find_records(
            schemas.UserMapping,
            [
                schemas.UserMapping.source_workspace_id == workspace_id,
                schemas.UserMapping.source_user_id == member["id"],
            ],
        )
        for m in mappings:
            if m.source_display_name != current_name:
                DbManager.update_records(
                    schemas.UserMapping,
                    [schemas.UserMapping.id == m.id],
                    {schemas.UserMapping.source_display_name: current_name},
                )

    _CACHE.pop(f"user_info:{member['id']}", None)


def _purge_mappings_for_user(slack_user_id: str, workspace_id: int) -> None:
    """Hard-delete all user mappings where this user is source or target."""
    DbManager.delete_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.source_workspace_id == workspace_id,
            schemas.UserMapping.source_user_id == slack_user_id,
        ],
    )
    DbManager.delete_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.target_workspace_id == workspace_id,
            schemas.UserMapping.target_user_id == slack_user_id,
        ],
    )


@slack_retry
def _lookup_user_by_email(client: WebClient, email: str) -> str | None:
    """Resolve a user ID from an email address in the target workspace."""
    res = client.users_lookupByEmail(email=email)
    return safe_get(res, "user", "id")


def _map_from_directory(
    source_profile: dict[str, Any],
    target_candidates: list[schemas.UserDirectory],
    target_by_email: dict[str, list[schemas.UserDirectory]],
) -> tuple[str | None, str]:
    """Map using existing user_directory rows only (no Slack API)."""
    email = (source_profile.get("email") or "").strip()
    if email:
        hits = target_by_email.get(email.lower()) or []
        if len(hits) == 1:
            return hits[0].slack_user_id, "email"

    source_real = source_profile.get("real_name", "") or ""
    source_display = source_profile.get("display_name", "") or ""
    source_normalized = _normalize_name(source_display) if source_display else _normalize_name(source_real)
    if not source_normalized:
        return None, "none"

    name_matches = [
        c
        for c in target_candidates
        if c.normalized_name
        and c.normalized_name.lower() == source_normalized.lower()
        and c.real_name
        and source_real
        and c.real_name.lower() == source_real.lower()
    ]
    if len(name_matches) == 1:
        return name_matches[0].slack_user_id, "name"

    if source_real:
        real_only = [c for c in target_candidates if c.real_name and c.real_name.lower() == source_real.lower()]
        if len(real_only) == 1:
            return real_only[0].slack_user_id, "name"

    return None, "none"


def _find_user_map(
    source_user_id: str,
    source_profile: dict[str, Any],
    target_client: WebClient | None,
    target_workspace_id: int,
    *,
    target_candidates: list[schemas.UserDirectory] | None = None,
    target_by_email: dict[str, list[schemas.UserDirectory]] | None = None,
    allow_slack_email_lookup: bool = True,
    email_lookup_denied: list[bool] | None = None,
) -> tuple[str | None, str]:
    """Match one source user against one target workspace.

    Prefer ``user_directory`` email, then name uniqueness. Optional Slack
    ``users.lookupByEmail`` is a fallback only when the directory has no row
    for that email.
    """
    if target_candidates is None:
        target_candidates = DbManager.find_records(
            schemas.UserDirectory,
            [
                schemas.UserDirectory.workspace_id == target_workspace_id,
                schemas.UserDirectory.deleted_at.is_(None),
            ],
        )
    if target_by_email is None:
        target_by_email = {}
        for entry in target_candidates:
            if not entry.email:
                continue
            key = entry.email.strip().lower()
            if key:
                target_by_email.setdefault(key, []).append(entry)

    target_uid, method = _map_from_directory(source_profile, target_candidates, target_by_email)
    if target_uid:
        return target_uid, method

    email = (source_profile.get("email") or "").strip()
    denied = bool(email_lookup_denied and email_lookup_denied[0])
    if (
        allow_slack_email_lookup
        and not denied
        and email
        and target_client is not None
        and email.lower() not in target_by_email
    ):
        try:
            looked_up = _lookup_user_by_email(target_client, email)
            if looked_up:
                return looked_up, "email"
        except SlackApiError as exc:
            err = ""
            try:
                err = str(exc.response.get("error") if exc.response is not None else exc)
            except Exception:
                err = str(exc)
            if err in ("missing_scope", "invalid_auth", "not_allowed"):
                _logger.warning(
                    "map_user_email_lookup_denied",
                    extra={"workspace_id": target_workspace_id, "error": err},
                )
                if email_lookup_denied is not None:
                    email_lookup_denied[0] = True
            elif err == "users_not_found":
                _logger.debug(
                    "map_user_email_lookup_users_not_found",
                    extra={"workspace_id": target_workspace_id},
                )
            else:
                _logger.debug(
                    "map_user_email_lookup_failed",
                    extra={"workspace_id": target_workspace_id, "error": err},
                )

    return None, "none"


def _source_profile_from_directory(workspace_id: int, slack_user_id: str) -> dict[str, Any] | None:
    """Build a map profile from user_directory when present."""
    rows = DbManager.find_records(
        schemas.UserDirectory,
        [
            schemas.UserDirectory.workspace_id == workspace_id,
            schemas.UserDirectory.slack_user_id == slack_user_id,
            schemas.UserDirectory.deleted_at.is_(None),
        ],
    )
    if not rows:
        return None
    entry = rows[0]
    return {
        "display_name": entry.display_name or "",
        "real_name": entry.real_name or "",
        "email": entry.email,
    }


def _get_source_profile_full(client: WebClient, user_id: str) -> dict[str, Any] | None:
    """Fetch full profile fields needed for mapping."""
    from helpers.slack_api import _token_fingerprint

    fingerprint = _token_fingerprint(client)
    cache_key = f"user_profile_full:{fingerprint}:{user_id}" if fingerprint else f"user_profile_full:{user_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        res = _users_info(client, user_id)
    except SlackApiError as exc:
        _logger.warning(f"Failed to look up user {user_id}: {exc}")
        return None

    profile = safe_get(res, "user", "profile") or {}
    result: dict[str, Any] = {
        "display_name": profile.get("display_name") or "",
        "real_name": profile.get("real_name") or "",
        "email": profile.get("email"),
    }
    _cache_set(cache_key, result, ttl=_USER_INFO_CACHE_TTL)
    return result


def get_mapped_target_user_id(
    source_user_id: str,
    source_workspace_id: int,
    target_workspace_id: int,
) -> str | None:
    """Return the mapped target user ID, or *None* if unmapped."""
    mapping = _mapping_row_for_pair(source_user_id, source_workspace_id, target_workspace_id)
    if mapping and mapping.target_user_id and mapping.map_method != "none":
        return mapping.target_user_id
    return None


def _mapping_row_for_pair(
    source_user_id: str,
    source_workspace_id: int,
    target_workspace_id: int,
) -> schemas.UserMapping | None:
    """Return the mapping row for this pair, or *None*."""
    from helpers._cache import request_scope_get, request_scope_set

    cache_key = f"mapping_row:{source_workspace_id}:{source_user_id}:{target_workspace_id}"
    cached = request_scope_get(cache_key)
    if cached is not None:
        return cached or None
    mappings = DbManager.find_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.source_workspace_id == source_workspace_id,
            schemas.UserMapping.source_user_id == source_user_id,
            schemas.UserMapping.target_workspace_id == target_workspace_id,
        ],
    )
    row = mappings[0] if mappings else False
    request_scope_set(cache_key, row)
    return row or None


def _persist_mapping_row(
    *,
    source_user_id: str,
    source_workspace_id: int,
    target_workspace_id: int,
    target_user_id: str | None,
    method: str,
    display_name: str,
    existing: schemas.UserMapping | None = None,
) -> None:
    """Write or update a mapping row (including ``none`` stubs)."""
    now = datetime.now(UTC)
    row = existing or _mapping_row_for_pair(source_user_id, source_workspace_id, target_workspace_id)
    if row:
        DbManager.update_records(
            schemas.UserMapping,
            [schemas.UserMapping.id == row.id],
            {
                schemas.UserMapping.target_user_id: target_user_id,
                schemas.UserMapping.map_method: method,
                schemas.UserMapping.source_display_name: display_name,
                schemas.UserMapping.mapped_at: now,
            },
        )
    else:
        DbManager.create_record(
            schemas.UserMapping(
                source_workspace_id=source_workspace_id,
                source_user_id=source_user_id,
                target_workspace_id=target_workspace_id,
                target_user_id=target_user_id,
                map_method=method,
                source_display_name=display_name,
                mapped_at=now,
                group_id=None,
            )
        )
    if method != "none" and target_user_id:
        try:
            target_ws = get_workspace_by_id(target_workspace_id)
            if target_ws and target_ws.team_id:
                from helpers.export_import import invalidate_home_tab_caches_for_team

                invalidate_home_tab_caches_for_team(target_ws.team_id)
        except Exception:
            _logger.warning(
                "user_mapping_home_hash_invalidate_failed",
                extra={"target_workspace_id": target_workspace_id},
            )
    from helpers._cache import request_scope_delete

    request_scope_delete(f"mapping_row:{source_workspace_id}:{source_user_id}:{target_workspace_id}")


def _persist_email_mapping(
    *,
    source_user_id: str,
    source_workspace_id: int,
    target_workspace_id: int,
    target_user_id: str,
    display_name: str,
) -> None:
    """Write or upgrade a mapping row to ``map_method=email``."""
    _persist_mapping_row(
        source_user_id=source_user_id,
        source_workspace_id=source_workspace_id,
        target_workspace_id=target_workspace_id,
        target_user_id=target_user_id,
        method="email",
        display_name=display_name,
    )


def ensure_mapped_target_user_id(
    source_user_id: str,
    source_workspace_id: int,
    target_workspace_id: int,
    *,
    source_client: WebClient | None = None,
    target_client: WebClient | None = None,
) -> str | None:
    """Return a mapped target user ID, creating an email mapping for this author if needed.

    Email only: unique target ``user_directory`` hit (case-insensitive), else one
    ``users.lookupByEmail``. Never crawls ``users.list`` or maps by name. On a
    miss, persists a ``none`` stub so later posts skip Slack until TTL expires.
    """
    if not source_user_id or not source_workspace_id or not target_workspace_id:
        return None

    existing = _mapping_row_for_pair(source_user_id, source_workspace_id, target_workspace_id)
    if existing and _is_mapping_fresh(existing):
        if existing.map_method != "none" and existing.target_user_id:
            return existing.target_user_id
        if existing.map_method == "none":
            return None

    try:
        profile = _source_profile_from_directory(source_workspace_id, source_user_id)
        if not (profile and (profile.get("email") or "").strip()) and source_client is not None:
            profile = _get_source_profile_full(source_client, source_user_id)
        email = ((profile.get("email") if profile else None) or "").strip()
        display = (
            (profile.get("display_name") if profile else None)
            or (profile.get("real_name") if profile else None)
            or source_user_id
        )
        if not email:
            _persist_mapping_row(
                source_user_id=source_user_id,
                source_workspace_id=source_workspace_id,
                target_workspace_id=target_workspace_id,
                target_user_id=None,
                method="none",
                display_name=display,
                existing=existing,
            )
            return None

        target_hits = DbManager.find_records(
            schemas.UserDirectory,
            [
                schemas.UserDirectory.workspace_id == target_workspace_id,
                schemas.UserDirectory.deleted_at.is_(None),
                func.lower(schemas.UserDirectory.email) == email.lower(),
            ],
        )
        target_uid: str | None = None
        if len(target_hits) == 1:
            target_uid = target_hits[0].slack_user_id
        elif len(target_hits) == 0 and target_client is not None:
            try:
                target_uid = _lookup_user_by_email(target_client, email)
            except SlackApiError as exc:
                err = ""
                try:
                    err = str(exc.response.get("error") if exc.response is not None else exc)
                except Exception:
                    err = str(exc)
                if err == "users_not_found":
                    _logger.debug(
                        "map_user_email_lookup_users_not_found",
                        extra={"workspace_id": target_workspace_id},
                    )
                else:
                    _logger.debug(
                        "map_user_email_lookup_failed",
                        extra={"workspace_id": target_workspace_id, "error": err},
                    )
                target_uid = None

        if target_uid:
            _persist_mapping_row(
                source_user_id=source_user_id,
                source_workspace_id=source_workspace_id,
                target_workspace_id=target_workspace_id,
                target_user_id=target_uid,
                method="email",
                display_name=display,
                existing=existing,
            )
            _logger.info(
                "user_mapping_on_the_fly",
                extra={
                    "source_workspace_id": source_workspace_id,
                    "target_workspace_id": target_workspace_id,
                    "source_user_id": source_user_id,
                    "target_user_id": target_uid,
                },
            )
            return target_uid

        _persist_mapping_row(
            source_user_id=source_user_id,
            source_workspace_id=source_workspace_id,
            target_workspace_id=target_workspace_id,
            target_user_id=None,
            method="none",
            display_name=display,
            existing=existing,
        )
        return None
    except Exception as exc:
        _logger.warning(
            "user_mapping_on_the_fly_failed",
            extra={
                "source_workspace_id": source_workspace_id,
                "target_workspace_id": target_workspace_id,
                "source_user_id": source_user_id,
                "error": str(exc),
            },
        )
        return None


def get_display_name_and_icon_for_synced_message(
    source_user_id: str,
    source_workspace_id: int,
    source_display_name: str | None,
    source_icon_url: str | None,
    target_client: WebClient,
    target_workspace_id: int,
    *,
    source_client: WebClient | None = None,
) -> tuple[str | None, str | None, bool, str | None]:
    """Return (display_name, icon_url, is_mapped, mapped_user_id) when syncing into a target.

    If the source user is mapped to a user in the target workspace, returns that
    local user's display name and profile image (third element ``True``). Otherwise
    returns the source display name and icon (``False``). Display names are shown
    as Slack provides them — normalization is only for backend user mapping, not
    for the posted username or mention labels. Callers omit the remote workspace
    suffix in the posted username when ``is_mapped`` is true.

    When unmapped, may create a one-author email mapping via
    :func:`ensure_mapped_target_user_id` before falling back to the remote name.
    """
    mapped_id = ensure_mapped_target_user_id(
        source_user_id,
        source_workspace_id,
        target_workspace_id,
        source_client=source_client,
        target_client=target_client,
    )
    if mapped_id:
        local_name, local_icon = get_user_info(target_client, mapped_id)
        if not local_name:
            target_profile = _source_profile_from_directory(target_workspace_id, mapped_id)
            if target_profile:
                local_name = target_profile.get("display_name") or target_profile.get("real_name")
        if local_name:
            return local_name, local_icon or source_icon_url, True, mapped_id
        return source_display_name, source_icon_url, True, mapped_id
    return source_display_name, source_icon_url, False, None


def unmapped_author_label(display_name: str | None, source_workspace_name: str | None) -> str:
    """Code-ticked label for an unmapped author in synced message text."""
    return code_ticked_display_name(display_name, source_workspace_name)


def resolve_mention_for_workspace(
    source_client: WebClient,
    source_user_id: str,
    source_workspace_id: int,
    target_client: WebClient,
    target_workspace_id: int,
) -> str:
    """Resolve a single @mention from source workspace to target workspace."""
    source_ws = get_workspace_by_id(source_workspace_id)
    source_ws_name = resolve_workspace_name(source_ws) if source_ws else None

    def _unmapped_label(name: str) -> str:
        return unmapped_author_label(name, source_ws_name)

    mapping = _mapping_row_for_pair(source_user_id, source_workspace_id, target_workspace_id)
    if mapping and _is_mapping_fresh(mapping):
        if mapping.target_user_id:
            return f"<@{mapping.target_user_id}>"
        return _unmapped_label(mapping.source_display_name or source_user_id)

    source_profile = _get_source_profile_full(source_client, source_user_id)
    if not source_profile:
        return _unmapped_label(source_user_id)

    target_uid, method = _find_user_map(source_user_id, source_profile, target_client, target_workspace_id)
    display = source_profile.get("display_name") or source_profile.get("real_name") or source_user_id
    _persist_mapping_row(
        source_user_id=source_user_id,
        source_workspace_id=source_workspace_id,
        target_workspace_id=target_workspace_id,
        target_user_id=target_uid,
        method=method,
        display_name=display,
        existing=mapping,
    )
    if target_uid:
        return f"<@{target_uid}>"
    return _unmapped_label(display)


_MAX_MENTIONS = 50


def parse_mentioned_users(msg_text: str, client: WebClient) -> list[dict[str, Any]]:
    """Extract mentioned user IDs from a message and resolve their profiles."""
    user_ids = re.findall(r"<@(\w+)>", msg_text or "")[:_MAX_MENTIONS]
    if not user_ids:
        return []

    results: list[dict[str, Any]] = []
    for uid in user_ids:
        profile = _get_user_profile(client, uid)
        if profile:
            results.append({"user_id": uid, **profile})
        else:
            results.append({"user_id": uid, "user_name": uid, "email": None})
    return results


def apply_mentioned_users(
    msg_text: str,
    source_client: WebClient,
    target_client: WebClient,
    mentioned_user_info: list[dict[str, Any]],
    source_workspace_id: int,
    target_workspace_id: int,
) -> str:
    """Re-map @mentions from the source workspace to the target workspace."""
    msg_text = msg_text or ""
    if not mentioned_user_info:
        return msg_text

    replace_list: list[str] = []
    for user_info in mentioned_user_info:
        uid = user_info.get("user_id", "")
        try:
            resolved = resolve_mention_for_workspace(
                source_client=source_client,
                source_user_id=uid,
                source_workspace_id=source_workspace_id,
                target_client=target_client,
                target_workspace_id=target_workspace_id,
            )
            replace_list.append(resolved)
        except Exception as exc:
            _logger.error(f"Failed to resolve mention for user {uid}: {exc}")
            fallback = user_info.get("user_name") or uid
            source_ws = get_workspace_by_id(source_workspace_id) if source_workspace_id else None
            ws_label = resolve_workspace_name(source_ws) if source_ws else None
            replace_list.append(unmapped_author_label(fallback, ws_label))

    replace_iter = iter(replace_list)

    def _replace(_match: re.Match) -> str:
        try:
            return next(replace_iter)
        except StopIteration:
            # parse_mentioned_users caps at 50; leave any leftover tags unchanged.
            return _match.group(0)

    return re.sub(r"<@\w+>", _replace, msg_text)


_CHANNEL_MENTION = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]*))?>")
# Keep pasted app.slack.com/client message URLs too; never *emit* that scheme for #channel.
_MESSAGE_PERMALINK = re.compile(
    r"<?(https://(?:app\.slack\.com/client/T[A-Z0-9]+/(C[A-Z0-9]+)/[\d.]+|"
    r"([a-z0-9][a-z0-9-]*)\.slack\.com/archives/(C[A-Z0-9]+)/p\d+(?:\?[^\s>]*)?))"
    r"(?:\|[^>]*)?>?"
)
# Lookahead so greedy C[A-Z0-9]+ does not backtrack into /p permalinks.
_CHANNEL_ARCHIVE_URL = re.compile(
    r"<?https://[a-z0-9][a-z0-9-]*\.slack\.com/archives/(C[A-Z0-9]+)(?=[|>\s]|$)(?:\|[^>]*)?>?"
)


def _lookup_channel_name(
    source_client: WebClient | None,
    channel_id: str,
    inline_label: str | None = None,
) -> str:
    """Best-effort source channel name; *channel_id* when it cannot be resolved."""
    if source_client:
        try:
            info = source_client.conversations_info(channel=channel_id)
            name = safe_get(info, "channel", "name")
            if name:
                return name
        except Exception as exc:
            _logger.debug(
                "resolve_channel_reference_failed",
                extra={"channel_id": channel_id, "error": str(exc)},
            )
    return inline_label or channel_id


def _hash_channel_label(ch_name: str, channel_id: str) -> str | None:
    """``#name`` when Slack returned a name; *None* when we only have the id."""
    return f"#{ch_name}" if ch_name != channel_id else None


def _channel_tick_markup(ch_name: str, channel_id: str, ws_name: str | None) -> str:
    place = _hash_channel_label(ch_name, channel_id)
    if not place:
        return f"#{channel_id}"
    return code_ticked_display_name(place, ws_name)


def _message_permalink_label(
    channel_id: str,
    domain: str | None,
    source_client: WebClient | None,
    ws_name: str | None,
) -> str:
    place = _hash_channel_label(_lookup_channel_name(source_client, channel_id), channel_id)
    where = ws_name or domain
    if place and where:
        return f"Message in {place} ({where})"
    if place:
        return f"Message in {place}"
    if where:
        return f"Message in {where}"
    return "Source message"


def _rewrite_message_permalink(match: re.Match, source_client: WebClient | None, ws_name: str | None) -> str:
    url = match.group(1)
    cid = match.group(2) or match.group(4)
    return f"<{url}|{_message_permalink_label(cid, match.group(3), source_client, ws_name)}>"


def _rewrite_channel_archive_url(match: re.Match, source_client: WebClient | None, ws_name: str | None) -> str:
    cid = match.group(1)
    return _channel_tick_markup(_lookup_channel_name(source_client, cid), cid, ws_name)


def resolve_channel_references(
    msg_text: str,
    source_client: WebClient | None,
    source_workspace: "schemas.Workspace | None" = None,
) -> str:
    """Rewrite source ``#channel`` mentions and Slack permalinks for the target.

    Target twins are never used. ``<#C>`` and channel-only archive URLs become a
    code-ticked ``#name (Workspace)`` (same-instance and federation). Do not
    emit target ``<#C>``, ``slack://``, ``app.slack.com/client``, or channel-only
    ``archives/C`` URLs.

    Message permalinks (``/archives/C…/p…``) stay as labeled source URLs.
    Those open the source message in the Slack **mobile** app. Slack **web**
    treats the same URL as a message in the current target workspace and shows
    a Private chip; that is accepted — do not chase other URL schemes to fix
    the desktop browser.
    """
    if not msg_text:
        return msg_text

    ws_name = resolve_workspace_name(source_workspace) if source_workspace else None
    msg_text = _MESSAGE_PERMALINK.sub(lambda m: _rewrite_message_permalink(m, source_client, ws_name), msg_text)
    msg_text = _CHANNEL_ARCHIVE_URL.sub(lambda m: _rewrite_channel_archive_url(m, source_client, ws_name), msg_text)

    pair_tuples = _CHANNEL_MENTION.findall(msg_text)
    if not pair_tuples:
        return msg_text

    by_channel_id: dict[str, str | None] = {}
    for cid, pipe in pair_tuples:
        if cid not in by_channel_id:
            by_channel_id[cid] = pipe.strip() if pipe and pipe.strip() else None

    for ch_id, inline_label in by_channel_id.items():
        ch_name = _lookup_channel_name(source_client, ch_id, inline_label)
        replacement = _channel_tick_markup(ch_name, ch_id, ws_name)
        msg_text = _CHANNEL_MENTION.sub(
            lambda m, _cid=ch_id, _rep=replacement: _rep if m.group(1) == _cid else m.group(0),
            msg_text,
        )

    return msg_text


def seed_user_mappings(source_workspace_id: int, target_workspace_id: int, group_id: int | None = None) -> int:
    """Create stub UserMapping records for all active users in the source directory."""
    directory = DbManager.find_records(
        schemas.UserDirectory,
        [schemas.UserDirectory.workspace_id == source_workspace_id, schemas.UserDirectory.deleted_at.is_(None)],
    )

    existing = DbManager.find_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.source_workspace_id == source_workspace_id,
            schemas.UserMapping.target_workspace_id == target_workspace_id,
        ],
    )
    existing_by_uid = {m.source_user_id: m for m in existing}

    now = datetime.now(UTC)
    to_create: list[schemas.UserMapping] = []
    for entry in directory:
        current_name = entry.display_name or entry.real_name
        if entry.slack_user_id in existing_by_uid:
            mapping = existing_by_uid[entry.slack_user_id]
            if mapping.source_display_name != current_name:
                DbManager.update_records(
                    schemas.UserMapping,
                    [schemas.UserMapping.id == mapping.id],
                    {schemas.UserMapping.source_display_name: current_name},
                )
            continue
        to_create.append(
            schemas.UserMapping(
                source_workspace_id=source_workspace_id,
                source_user_id=entry.slack_user_id,
                target_workspace_id=target_workspace_id,
                target_user_id=None,
                map_method="none",
                source_display_name=current_name,
                mapped_at=now,
                group_id=group_id,
            )
        )

    if to_create:
        DbManager.create_records(to_create)
    return len(to_create)


def run_auto_map_for_workspace(
    target_client: WebClient | None,
    target_workspace_id: int,
    *,
    allow_slack_email_lookup: bool = True,
    seeded: int | None = None,
) -> tuple[int, int]:
    """Re-run auto-map for unmatched mappings targeting a workspace.

    Uses existing ``user_directory`` rows. Does **not** start a ``users.list``
    crawl. Returns ``(newly_matched, still_unmatched)``.
    """
    unmatched = DbManager.find_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.target_workspace_id == target_workspace_id,
            schemas.UserMapping.map_method == "none",
        ],
    )

    target_candidates = DbManager.find_records(
        schemas.UserDirectory,
        [
            schemas.UserDirectory.workspace_id == target_workspace_id,
            schemas.UserDirectory.deleted_at.is_(None),
        ],
    )
    target_by_email: dict[str, list[schemas.UserDirectory]] = {}
    for entry in target_candidates:
        if not entry.email:
            continue
        key = entry.email.strip().lower()
        if key:
            target_by_email.setdefault(key, []).append(entry)
    emails_present = len(target_by_email)

    newly_matched = 0
    still_unmatched = 0
    by_email = 0
    by_name = 0
    email_lookup_denied = [False]

    for mapping in unmatched:
        source_profile = _source_profile_from_directory(mapping.source_workspace_id, mapping.source_user_id)
        if not source_profile and allow_slack_email_lookup:
            # Slack users.info only when the caller allowed per-user lookups
            # (not Auto Map Now, which must stay directory-only).
            source_workspace = get_workspace_by_id(mapping.source_workspace_id)
            if source_workspace and source_workspace.bot_token:
                source_client = WebClient(token=decrypt_bot_token(source_workspace.bot_token))
                source_profile = _get_source_profile_full(source_client, mapping.source_user_id)
        if not source_profile:
            still_unmatched += 1
            continue

        target_uid, method = _find_user_map(
            mapping.source_user_id,
            source_profile,
            target_client,
            target_workspace_id,
            target_candidates=target_candidates,
            target_by_email=target_by_email,
            allow_slack_email_lookup=allow_slack_email_lookup,
            email_lookup_denied=email_lookup_denied,
        )

        if target_uid:
            display = source_profile.get("display_name") or source_profile.get("real_name") or mapping.source_user_id
            DbManager.update_records(
                schemas.UserMapping,
                [schemas.UserMapping.id == mapping.id],
                {
                    schemas.UserMapping.target_user_id: target_uid,
                    schemas.UserMapping.map_method: method,
                    schemas.UserMapping.source_display_name: display,
                    schemas.UserMapping.mapped_at: datetime.now(UTC),
                },
            )
            newly_matched += 1
            if method == "email":
                by_email += 1
            elif method == "name":
                by_name += 1
        else:
            still_unmatched += 1

    extras: dict[str, Any] = {
        "workspace_id": target_workspace_id,
        "newly_matched": newly_matched,
        "still_unmatched": still_unmatched,
        "by_email": by_email,
        "by_name": by_name,
        "emails_present": emails_present,
    }
    if seeded is not None:
        extras["seeded"] = seeded
    _logger.info("user_auto_map_complete", extra=extras)
    return newly_matched, still_unmatched


_LAST_AUTO_MAP_KEY = "last_auto_map"
_AUTO_MAP_RUNNING_TTL = 45


def auto_map_running_key(workspace_id: int) -> str:
    return f"auto_map_running:{workspace_id}"


def get_last_auto_map(workspace_id: int) -> dict[str, Any] | None:
    """Return the last Auto Map Now summary for *workspace_id*, or None."""
    from helpers.workspace_settings import get_raw_workspace_setting

    raw = get_raw_workspace_setting(workspace_id, _LAST_AUTO_MAP_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def set_last_auto_map(
    workspace_id: int,
    *,
    newly_matched: int,
    still_unmatched: int,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Persist last Auto Map Now summary without Settings-modal logging."""
    now = at or datetime.now(UTC)
    payload = {
        "at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "newly_matched": int(newly_matched),
        "still_unmatched": int(still_unmatched),
    }
    value = json.dumps(payload)
    existing = DbManager.find_records(
        schemas.WorkspaceSetting,
        [
            schemas.WorkspaceSetting.workspace_id == workspace_id,
            schemas.WorkspaceSetting.key == _LAST_AUTO_MAP_KEY,
        ],
    )
    if existing:
        DbManager.update_records(
            schemas.WorkspaceSetting,
            [
                schemas.WorkspaceSetting.workspace_id == workspace_id,
                schemas.WorkspaceSetting.key == _LAST_AUTO_MAP_KEY,
            ],
            {
                schemas.WorkspaceSetting.value: value,
                schemas.WorkspaceSetting.updated_at: now,
            },
        )
    else:
        DbManager.create_record(
            schemas.WorkspaceSetting(
                workspace_id=workspace_id,
                key=_LAST_AUTO_MAP_KEY,
                value=value,
                updated_at=now,
            )
        )
    from helpers._cache import _cache_delete

    _cache_delete(f"workspace_setting:{workspace_id}:{_LAST_AUTO_MAP_KEY}")
    return payload


def format_last_auto_map_line(last: dict[str, Any] | None) -> str:
    """One-line status under Auto Map Now (UTC date)."""
    if not last:
        return "_No auto map yet. Incomplete lists usually mean the directory is still filling in._"
    newly = int(last.get("newly_matched") or 0)
    at_raw = last.get("at") or ""
    date_label = "an earlier date"
    try:
        parsed = datetime.fromisoformat(str(at_raw).replace("Z", "+00:00")).astimezone(UTC)
        date_label = f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    except Exception:
        pass
    return f"Last run on {date_label} with {newly} new found."
