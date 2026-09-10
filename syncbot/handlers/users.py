"""User event handlers — team join, profile changes, user mapping management."""

import contextlib
import logging
from datetime import UTC, datetime
from logging import Logger

from slack_sdk.web import WebClient

import builders
import helpers
from builders._common import _get_group_members, _get_groups_for_workspace
from builders.user_mapping import (
    seed_mappings_for_workspace,
    update_user_mapping_modal,
)
from db import DbManager, schemas
from handlers._common import _get_authorized_workspace, _parse_private_metadata
from helpers.user_map import (
    _AUTO_MAP_RUNNING_TTL,
    auto_map_running_key,
    set_last_auto_map,
)
from slack import actions

_logger = logging.getLogger(__name__)


def handle_team_join(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle a team_join event: a new user joined a connected workspace.

    1. Upsert the new user into ``user_directory`` for this workspace.
    2. Re-check all ``map_method='none'`` mappings targeting this workspace.
    """
    event = body.get("event", {})
    user_data = event.get("user", {})
    team_id = helpers.safe_get(body, "team_id")

    if not user_data or not team_id:
        return

    if user_data.get("is_bot") or user_data.get("id") == "USLACKBOT":
        return

    workspace_record = DbManager.get_record(schemas.Workspace, id=team_id)
    if not workspace_record:
        _logger.warning(f"team_join: unknown team_id {team_id}")
        return

    _logger.info(
        "team_join_received",
        extra={"team_id": team_id, "user_id": user_data.get("id")},
    )

    helpers._upsert_single_user_to_directory(user_data, workspace_record.id)
    # ``user_auto_map_complete`` is the single INFO summary (do not also log team_join mapping).
    helpers.run_auto_map_for_workspace(client, workspace_record.id)


def handle_user_profile_changed(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Handle a user_profile_changed event: update directory and notify group members."""
    event = body.get("event", {})
    user_data = event.get("user", {})
    team_id = helpers.safe_get(body, "team_id")

    if not user_data or not team_id:
        return

    if user_data.get("is_bot") or user_data.get("id") == "USLACKBOT":
        return

    workspace_record = DbManager.get_record(schemas.Workspace, id=team_id)
    if not workspace_record:
        return

    helpers._upsert_single_user_to_directory(user_data, workspace_record.id)

    my_groups = _get_groups_for_workspace(workspace_record.id)
    notified_ws: set[int] = set()
    for group, _ in my_groups:
        members = _get_group_members(group.id)
        for member in members:
            if (
                member.workspace_id
                and member.workspace_id != workspace_record.id
                and member.workspace_id not in notified_ws
            ):
                member_ws = helpers.get_workspace_by_id(member.workspace_id, context=context)
                if member_ws:
                    builders.refresh_home_tab_for_workspace(member_ws, logger, context=None)
                    notified_ws.add(member.workspace_id)

    _logger.info(
        "user_profile_updated",
        extra={"team_id": team_id, "user_id": user_data.get("id")},
    )


def _mapping_modal_view_id(body: dict) -> str | None:
    return helpers.safe_get(body, "view", "id")


def _group_and_page_from_body(body: dict) -> tuple[int | None, int]:
    """Parse group_id and page from button value and/or private_metadata."""
    group_id = 0
    page = 0
    raw_value = helpers.safe_get(body, "actions", 0, "value") or ""
    if ":" in str(raw_value):
        parts = str(raw_value).split(":")
        with contextlib.suppress(TypeError, ValueError):
            group_id = int(parts[0])
        with contextlib.suppress(TypeError, ValueError):
            page = int(parts[1])
    else:
        with contextlib.suppress(TypeError, ValueError):
            group_id = int(raw_value or 0)
        meta = _parse_private_metadata(body)
        with contextlib.suppress(TypeError, ValueError):
            page = int(meta.get("page") or 0)
        if not group_id:
            with contextlib.suppress(TypeError, ValueError):
                group_id = int(meta.get("group_id") or 0)
    return (group_id or None), page


def handle_user_mapping_refresh(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Reload the User Mapping modal from the DB (no crawl, no map)."""
    auth_result = _get_authorized_workspace(body, client, context, "user_mapping_refresh")
    if not auth_result:
        return
    _user_id, workspace_record = auth_result

    view_id = _mapping_modal_view_id(body)
    if not view_id:
        return

    group_id, page = _group_and_page_from_body(body)
    update_user_mapping_modal(client, view_id, workspace_record, group_id=group_id, page=page, context=context)


def handle_user_mapping_auto_map(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Seed + directory auto-map, then update the open modal with results."""
    auth_result = _get_authorized_workspace(body, client, context, "user_mapping_auto_map")
    if not auth_result:
        return
    _user_id, workspace_record = auth_result

    view_id = _mapping_modal_view_id(body)
    if not view_id:
        return

    group_id, page = _group_and_page_from_body(body)
    running_key = auto_map_running_key(workspace_record.id)
    if helpers._cache_get(running_key):
        update_user_mapping_modal(
            client,
            view_id,
            workspace_record,
            group_id=group_id,
            page=page,
            context=context,
            mapping_in_progress=True,
        )
        return

    helpers._cache_set(running_key, True, ttl=_AUTO_MAP_RUNNING_TTL)
    update_user_mapping_modal(
        client,
        view_id,
        workspace_record,
        group_id=group_id,
        page=page,
        context=context,
        mapping_in_progress=True,
    )

    newly_matched = 0
    still_unmatched = 0
    try:
        seeded = seed_mappings_for_workspace(workspace_record, group_id, context=context)
        newly_matched, still_unmatched = helpers.run_auto_map_for_workspace(
            client,
            workspace_record.id,
            allow_slack_email_lookup=False,
            seeded=seeded,
        )
        set_last_auto_map(
            workspace_record.id,
            newly_matched=newly_matched,
            still_unmatched=still_unmatched,
        )
        helpers._cache_delete_prefix(f"home_tab_hash:{workspace_record.team_id}")
        helpers._cache_delete_prefix(f"home_tab_blocks:{workspace_record.team_id}")
    except Exception as exc:
        _logger.warning(
            "user_mapping_auto_map_failed",
            extra={"workspace_id": workspace_record.id, "error": str(exc)},
        )
    finally:
        helpers._cache_delete(running_key)

    update_user_mapping_modal(client, view_id, workspace_record, group_id=group_id, page=page, context=context)


def handle_user_mapping_page(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Prev/Next page within the User Mapping modal."""
    auth_result = _get_authorized_workspace(body, client, context, "user_mapping_page")
    if not auth_result:
        return
    _user_id, workspace_record = auth_result

    view_id = _mapping_modal_view_id(body)
    if not view_id:
        return

    action_id = helpers.safe_get(body, "actions", 0, "action_id") or ""
    group_id, page = _group_and_page_from_body(body)
    if action_id == actions.CONFIG_USER_MAPPING_PAGE_PREV:
        page = max(0, page - 1)
    elif action_id == actions.CONFIG_USER_MAPPING_PAGE_NEXT:
        page = page + 1

    update_user_mapping_modal(client, view_id, workspace_record, group_id=group_id, page=page, context=context)


def handle_user_mapping_edit_submit(
    body: dict,
    client: WebClient,
    logger: Logger,
    context: dict,
) -> None:
    """Save the per-user mapping edit and refresh the parent list modal."""
    auth_result = _get_authorized_workspace(body, client, context, "user_mapping_edit_submit")
    if not auth_result:
        return
    user_id, workspace_record = auth_result

    meta = _parse_private_metadata(body)
    mapping_id = meta.get("mapping_id")
    group_id = meta.get("group_id") or 0
    page = int(meta.get("page") or 0)
    parent_view_id = meta.get("parent_view_id")

    if not mapping_id:
        _logger.warning("user_mapping_edit_submit: missing mapping_id")
        return

    mapping = DbManager.get_record(schemas.UserMapping, id=mapping_id)
    if not mapping:
        return

    values = helpers.safe_get(body, "view", "state", "values") or {}
    selected = None
    remove = False
    for block_data in values.values():
        for action_id, action_data in block_data.items():
            if action_id == actions.CONFIG_USER_MAPPING_EDIT_REMOVE:
                sel = action_data.get("selected_option") or {}
                if sel.get("value") == "remove":
                    remove = True
            elif "selected_user" in action_data:
                selected = action_data.get("selected_user")
            elif action_data.get("selected_option"):
                selected = action_data["selected_option"].get("value")

    now = datetime.now(UTC)
    if remove:
        DbManager.update_records(
            schemas.UserMapping,
            [schemas.UserMapping.id == mapping.id],
            {
                schemas.UserMapping.target_user_id: None,
                schemas.UserMapping.map_method: "none",
                schemas.UserMapping.mapped_at: now,
            },
        )
        _logger.info("user_mapping_removed", extra={"mapping_id": mapping.id})
    elif selected:
        existing = DbManager.find_records(
            schemas.UserMapping,
            [
                schemas.UserMapping.source_workspace_id == mapping.source_workspace_id,
                schemas.UserMapping.target_workspace_id == mapping.target_workspace_id,
                schemas.UserMapping.target_user_id == selected,
                schemas.UserMapping.map_method != "none",
                schemas.UserMapping.id != mapping.id,
            ],
        )
        if existing:
            try:
                client.chat_postMessage(
                    channel=user_id,
                    text=(
                        ":warning: That local user is already mapped to someone else in this "
                        "pair of Workspaces. Pick a different user or remove the other mapping first."
                    ),
                )
            except Exception as exc:
                _logger.warning("user_mapping_duplicate_dm_failed", extra={"error": str(exc)})
            return

        DbManager.update_records(
            schemas.UserMapping,
            [schemas.UserMapping.id == mapping.id],
            {
                schemas.UserMapping.target_user_id: selected,
                schemas.UserMapping.map_method: "manual",
                schemas.UserMapping.mapped_at: now,
            },
        )
        _logger.info("user_mapping_updated", extra={"mapping_id": mapping.id, "target_user_id": selected})

    if parent_view_id:
        update_user_mapping_modal(
            client,
            parent_view_id,
            workspace_record,
            group_id=group_id or None,
            page=page,
            context=context,
        )
