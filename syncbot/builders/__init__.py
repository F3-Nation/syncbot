"""Builders package – Slack modal and home-tab UI constructors.

Re-exports every public symbol so that ``import builders`` /
``from builders import X`` continues to work after the split.
"""

from builders._common import (
    _format_channel_ref,
    _get_group_members,
    _get_groups_for_workspace,
    _get_workspace_info,
)
from builders.channel_sync import (
    _build_inline_channel_sync,
)
from builders.home import (
    _build_authorize_section,
    _home_tab_content_hash,
    build_home_tab,
    home_tab_hash_key,
    refresh_home_tab_for_workspace,
)
from builders.user_mapping import (
    build_user_mapping_edit_modal,
    build_user_mapping_entry,
    seed_mappings_for_workspace,
    update_user_mapping_modal,
)

__all__ = [
    "_build_authorize_section",
    "_build_inline_channel_sync",
    "home_tab_hash_key",
    "_format_channel_ref",
    "_get_group_members",
    "_get_groups_for_workspace",
    "_get_workspace_info",
    "_home_tab_content_hash",
    "build_home_tab",
    "build_user_mapping_edit_modal",
    "build_user_mapping_entry",
    "refresh_home_tab_for_workspace",
    "seed_mappings_for_workspace",
    "update_user_mapping_modal",
]
