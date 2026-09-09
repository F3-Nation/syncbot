"""User Mapping modal builders."""

import contextlib
import json
import logging
from typing import Any

from slack_sdk.web import WebClient

import constants
import helpers
from builders._common import (
    _deny_unauthorized,
    _get_group_members,
    _get_groups_for_workspace,
    _get_team_id,
    _get_user_id,
)
from db import DbManager
from db.schemas import UserMapping, Workspace, WorkspaceGroup
from helpers.user_map import (
    format_last_auto_map_line,
    get_last_auto_map,
)
from slack import actions, orm
from slack.blocks import actions as blocks_actions, button, context as block_context, divider, header, section

_logger = logging.getLogger(__name__)

_PAGE_SIZE = constants.USER_MAPPING_PAGE_SIZE
_INTRO = "_Users with the same email across Workspaces can be found by clicking Auto Map Now._"


def _group_display_name(group_id: int | None) -> str:
    if not group_id:
        return "Group"
    groups = DbManager.find_records(WorkspaceGroup, [WorkspaceGroup.id == group_id])
    return groups[0].name if groups else "Group"


def _workspace_label(ws: Workspace) -> str:
    """Stored name only — no Slack team_info on the modal path."""
    return (ws.workspace_name or "").strip()


def _linked_workspace_ids(workspace_id: int, group_id: int | None) -> set[int]:
    if group_id:
        members = _get_group_members(group_id)
        return {m.workspace_id for m in members if m.workspace_id and m.workspace_id != workspace_id}
    linked: set[int] = set()
    for g, _ in _get_groups_for_workspace(workspace_id):
        for m in _get_group_members(g.id):
            if m.workspace_id and m.workspace_id != workspace_id:
                linked.add(m.workspace_id)
    return linked


def _collect_mappings(workspace_id: int, linked_workspace_ids: set[int]) -> list[UserMapping]:
    all_mappings: list[UserMapping] = []
    for source_ws_id in linked_workspace_ids:
        mappings = DbManager.find_records(
            UserMapping,
            [
                UserMapping.source_workspace_id == source_ws_id,
                UserMapping.target_workspace_id == workspace_id,
            ],
        )
        all_mappings.extend(mappings)
    return all_mappings


def _display_for_mapping(m: UserMapping, ws_lookup: dict[int, str]) -> str:
    display = helpers.normalize_display_name(m.source_display_name or m.source_user_id)
    ws_label = ws_lookup.get(m.source_workspace_id, "")
    return f"{display} ({ws_label})" if ws_label else display


def _ordered_mappings(
    all_mappings: list[UserMapping],
    ws_lookup: dict[int, str],
) -> tuple[list[UserMapping], list[UserMapping], int, int]:
    """Return (page_source ordered unmapped-first, mapped list, mapped_count, unmapped_count)."""
    unmapped = [m for m in all_mappings if m.target_user_id is None or m.map_method == "none"]
    mapped = [m for m in all_mappings if m.target_user_id is not None and m.map_method != "none"]
    unmapped.sort(key=lambda m: _display_for_mapping(m, ws_lookup).lower())
    mapped.sort(key=lambda m: _display_for_mapping(m, ws_lookup).lower())
    return unmapped + mapped, mapped, len(mapped), len(unmapped)


def seed_mappings_for_workspace(
    workspace_record: Workspace,
    group_id: int | None,
    *,
    context: dict | None = None,
) -> int:
    """Create stub mappings both directions from existing directory rows only."""
    linked = _linked_workspace_ids(workspace_record.id, group_id)
    seeded = 0
    for source_ws_id in linked:
        try:
            seeded += helpers.seed_user_mappings(source_ws_id, workspace_record.id, group_id=group_id)
            seeded += helpers.seed_user_mappings(workspace_record.id, source_ws_id, group_id=group_id)
        except Exception as exc:
            _logger.warning(
                "user_mapping_seed_failed",
                extra={
                    "workspace_id": workspace_record.id,
                    "partner_workspace_id": source_ws_id,
                    "error": str(exc),
                },
            )
    return seeded


def build_user_mapping_list_blocks(
    workspace_record: Workspace,
    *,
    group_id: int | None = None,
    page: int = 0,
    context: dict | None = None,
    mapping_in_progress: bool = False,
) -> tuple[list[orm.BaseBlock], dict[str, Any]]:
    """Build list-modal blocks and private_metadata for the current page."""
    group_name = _group_display_name(group_id)
    group_val = str(group_id) if group_id else "0"
    meta = {"group_id": group_id or 0, "page": max(0, page)}
    last_line = format_last_auto_map_line(get_last_auto_map(workspace_record.id))

    if mapping_in_progress:
        blocks: list[orm.BaseBlock] = [
            header(f"User Mapping: {group_name}"),
            block_context(_INTRO),
            block_context("*Mapping users...*"),
            blocks_actions(
                button(":arrows_counterclockwise: Refresh List", actions.CONFIG_USER_MAPPING_REFRESH, value=group_val)
            ),
            block_context(last_line),
        ]
        return blocks, meta

    linked = _linked_workspace_ids(workspace_record.id, group_id)
    all_mappings = _collect_mappings(workspace_record.id, linked)

    ws_lookup: dict[int, str] = {}
    for source_ws_id in linked:
        ws = helpers.get_workspace_by_id(source_ws_id, context=context)
        if ws:
            ws_lookup[source_ws_id] = _workspace_label(ws)

    ordered, _mapped, mapped_count, unmapped_count = _ordered_mappings(all_mappings, ws_lookup)
    total = len(ordered)
    page = max(0, page)
    max_page = max(0, (total - 1) // _PAGE_SIZE) if total else 0
    if page > max_page:
        page = max_page
    start = page * _PAGE_SIZE
    page_rows = ordered[start : start + _PAGE_SIZE]
    meta["page"] = page

    blocks = [
        header(f"User Mapping: {group_name}"),
        block_context(_INTRO),
        blocks_actions(
            button(":busts_in_silhouette: Auto Map Now", actions.CONFIG_USER_MAPPING_AUTO_MAP, value=group_val),
            button(":arrows_counterclockwise: Refresh List", actions.CONFIG_USER_MAPPING_REFRESH, value=group_val),
        ),
        block_context(last_line),
        block_context(f"*Mapped: {mapped_count}*  \u00b7  *Unmapped: {unmapped_count}*"),
        divider(),
    ]

    if not page_rows:
        blocks.append(block_context("_No users have been mapped in this Workspace Group yet._"))
    else:
        for m in page_rows:
            label = _display_for_mapping(m, ws_lookup)
            if m.target_user_id and m.map_method != "none":
                row_text = f"*{label}*  \u2192  <@{m.target_user_id}> _[{m.map_method}]_"
            else:
                row_text = f":warning: *{label}*"
            blocks.append(section(row_text))
            blocks.append(blocks_actions(button("Edit", f"{actions.CONFIG_USER_MAPPING_EDIT}_{m.id}", value=group_val)))

    if total > _PAGE_SIZE:
        nav: list = []
        if page > 0:
            nav.append(button("Previous", actions.CONFIG_USER_MAPPING_PAGE_PREV, value=f"{group_val}:{page}"))
        if page < max_page:
            nav.append(button("Next", actions.CONFIG_USER_MAPPING_PAGE_NEXT, value=f"{group_val}:{page}"))
        if nav:
            blocks.append(divider())
            blocks.append(block_context(f"_Page {page + 1} of {max_page + 1}_"))
            blocks.append(blocks_actions(*nav))

    return blocks, meta


def update_user_mapping_modal(
    client: WebClient,
    view_id: str,
    workspace_record: Workspace,
    *,
    group_id: int | None = None,
    page: int = 0,
    context: dict | None = None,
    mapping_in_progress: bool = False,
) -> None:
    """Replace the User Mapping modal contents with the current list page."""
    blocks, meta = build_user_mapping_list_blocks(
        workspace_record,
        group_id=group_id,
        page=page,
        context=context,
        mapping_in_progress=mapping_in_progress,
    )
    try:
        orm.BlockView(blocks=blocks).update_modal(
            client=client,
            view_id=view_id,
            title_text="User Mapping",
            callback_id=actions.CONFIG_USER_MAPPING_MODAL,
            submit_button_text=None,
            close_button_text="Close",
            parent_metadata=meta,
        )
    except Exception as exc:
        _logger.debug(
            "user_mapping_modal_update_failed",
            extra={"view_id": view_id, "workspace_id": workspace_record.id, "error": str(exc)},
        )


def build_user_mapping_entry(
    body: dict,
    client: WebClient,
    logger,
    context: dict,
) -> None:
    """Open the User Mapping modal from the DB only (no seed/map/crawl)."""
    if _deny_unauthorized(body, client, logger):
        return

    raw_value = helpers.safe_get(body, "actions", 0, "value")
    group_id = None
    if raw_value:
        with contextlib.suppress(TypeError, ValueError):
            group_id = int(raw_value)

    user_id = _get_user_id(body)
    team_id = _get_team_id(body)
    trigger_id = helpers.safe_get(body, "trigger_id")
    if not user_id or not team_id or not trigger_id:
        return

    workspace_record = helpers.get_workspace_record(team_id, body, context, client)
    if not workspace_record:
        return

    blocks, meta = build_user_mapping_list_blocks(workspace_record, group_id=group_id, page=0, context=context)
    orm.BlockView(blocks=blocks).post_modal(
        client=client,
        trigger_id=trigger_id,
        title_text="User Mapping",
        callback_id=actions.CONFIG_USER_MAPPING_MODAL,
        submit_button_text=None,
        close_button_text="Close",
        parent_metadata=meta,
        body=body,
    )


def build_user_mapping_edit_modal(
    body: dict,
    client: WebClient,
    logger,
    context: dict,
) -> None:
    """Push a nested modal to edit a single user mapping (native users_select)."""
    if _deny_unauthorized(body, client, logger):
        return

    trigger_id = helpers.safe_get(body, "trigger_id")
    if not trigger_id:
        return

    action_id = helpers.safe_get(body, "actions", 0, "action_id") or ""
    mapping_id_str = action_id.replace(actions.CONFIG_USER_MAPPING_EDIT + "_", "")
    try:
        mapping_id = int(mapping_id_str)
    except (TypeError, ValueError):
        _logger.warning(f"build_user_mapping_edit_modal: invalid mapping_id: {mapping_id_str}")
        return

    raw_group = helpers.safe_get(body, "actions", 0, "value") or "0"
    try:
        group_id = int(raw_group)
    except (TypeError, ValueError):
        group_id = 0

    mapping = DbManager.get_record(UserMapping, id=mapping_id)
    if not mapping:
        _logger.warning(f"build_user_mapping_edit_modal: mapping {mapping_id} not found")
        return

    team_id = _get_team_id(body)
    workspace_record = helpers.get_workspace_record(team_id, body, context, client) if team_id else None
    if not workspace_record:
        return

    source_ws = helpers.get_workspace_by_id(mapping.source_workspace_id)
    source_ws_name = helpers.resolve_workspace_name(source_ws) if source_ws else "Partner"
    display = helpers.normalize_display_name(mapping.source_display_name or mapping.source_user_id)

    parent_view_id = helpers.safe_get(body, "view", "id")
    page = 0
    with contextlib.suppress(Exception):
        raw_meta = helpers.safe_get(body, "view", "private_metadata")
        if raw_meta:
            parsed = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
            page = int(parsed.get("page") or 0)

    avatar_accessory = None
    if source_ws and source_ws.bot_token:
        with contextlib.suppress(Exception):
            member_client = WebClient(token=helpers.decrypt_bot_token(source_ws.bot_token))
            _, avatar_url = helpers.get_user_info(member_client, mapping.source_user_id)
            if avatar_url:
                avatar_accessory = orm.ImageAccessoryElement(image_url=avatar_url, alt_text=display)

    has_mapping = mapping.target_user_id is not None and mapping.map_method != "none"
    blocks: list[orm.BaseBlock] = [
        orm.SectionBlock(label=f"*{display}*\n_{source_ws_name}_", element=avatar_accessory),
    ]
    if has_mapping:
        blocks.append(block_context(f"Currently mapped to <@{mapping.target_user_id}> _[{mapping.map_method}]_"))
    blocks.append(divider())
    blocks.append(
        orm.InputBlock(
            label="Map to user in this Workspace",
            action=actions.CONFIG_USER_MAPPING_EDIT_SELECT,
            element=orm.UsersSelectElement(
                placeholder="Select a user...",
                initial_value=mapping.target_user_id if has_mapping else None,
            ),
            optional=True,
        )
    )
    if has_mapping:
        blocks.append(
            orm.InputBlock(
                label="Or remove",
                action=actions.CONFIG_USER_MAPPING_EDIT_REMOVE,
                element=orm.RadioButtonsElement(
                    initial_value="keep",
                    options=[
                        orm.SelectorOption(name="Keep / update mapping", value="keep"),
                        orm.SelectorOption(name="Remove this mapping", value="remove"),
                    ],
                ),
                optional=False,
            )
        )

    meta = {
        "mapping_id": mapping_id,
        "group_id": group_id or 0,
        "page": page,
        "parent_view_id": parent_view_id,
    }
    modal_form = orm.BlockView(blocks=blocks)
    modal_form.post_modal(
        client=client,
        trigger_id=trigger_id,
        callback_id=actions.CONFIG_USER_MAPPING_EDIT_SUBMIT,
        title_text="Edit Mapping",
        submit_button_text="Save",
        close_button_text="Cancel",
        parent_metadata=meta,
        new_or_add="add",
        body=body,
    )
