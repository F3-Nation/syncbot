"""Slack Block Kit action ID constants.

These string constants are used as ``action_id`` / ``callback_id`` values
throughout the UI forms and handler routing tables.  Keeping them in one
place avoids typos and makes refactoring easier.
"""

LOADING_MODAL_CALLBACK = "loading"
"""Callback on the ack Loading view. Close only; not a view submission."""

MODAL_DENIED_CALLBACK = "modal_denied"
"""Callback when an opener does not fill the Loading view. Close only; not a view submission."""

# ---------------------------------------------------------------------------
# User Mapping actions
# ---------------------------------------------------------------------------

CONFIG_MANAGE_USER_MAPPING = "manage_user_mapping"
"""Action: user clicked "User Mapping" button on the Home tab."""

CONFIG_USER_MAPPING_MODAL = "user_mapping_modal"
"""Callback: User Mapping list modal (Close only; no submit)."""

CONFIG_USER_MAPPING_EDIT = "user_mapping_edit"
"""Action: user clicked "Edit" on a user row in the mapping modal (prefix-matched with mapping ID)."""

CONFIG_USER_MAPPING_EDIT_SUBMIT = "user_mapping_edit_submit"
"""Callback: per-user edit mapping modal submitted."""

CONFIG_USER_MAPPING_EDIT_SELECT = "user_mapping_edit_select"
"""Input: users_select picker in the edit mapping modal."""

CONFIG_USER_MAPPING_EDIT_REMOVE = "user_mapping_edit_remove"
"""Input: optional radio to remove an existing mapping."""

CONFIG_USER_MAPPING_REFRESH = "user_mapping_refresh"
"""Action: user clicked "Refresh List" in the User Mapping modal (reload from DB)."""

CONFIG_USER_MAPPING_AUTO_MAP = "user_mapping_auto_map"
"""Action: run directory auto-map for this workspace. Must not share the edit prefix."""

CONFIG_USER_MAPPING_PAGE_PREV = "user_mapping_page_prev"
"""Action: previous page in the User Mapping modal. Must not share the edit prefix."""

CONFIG_USER_MAPPING_PAGE_NEXT = "user_mapping_page_next"
"""Action: next page in the User Mapping modal. Must not share the edit prefix."""

# ---------------------------------------------------------------------------
# Workspace Group actions
# ---------------------------------------------------------------------------

CONFIG_CREATE_GROUP = "create_group"
"""Action: user clicked "Create Group" on the Home tab."""

CONFIG_CREATE_GROUP_SUBMIT = "create_group_submit"
"""Callback: create-group modal submitted."""

CONFIG_CREATE_GROUP_NAME = "create_group_name"
"""Input: text field for the group name."""

CONFIG_JOIN_GROUP = "join_group"
"""Action: user clicked "Join Group" on the Home tab."""

CONFIG_JOIN_GROUP_SUBMIT = "join_group_submit"
"""Callback: join-group modal submitted."""

CONFIG_JOIN_GROUP_CODE = "join_group_code"
"""Input: text field for the group invite code."""

CONFIG_LEAVE_GROUP = "leave_group"
"""Action: user clicked "Leave Group" (prefix-matched with group_id)."""

CONFIG_LEAVE_GROUP_CONFIRM = "confirm_leave_group"
"""Action (block): red confirm button inside the leave-group modal.

Not ``leave_group_confirm``: that string is prefix-matched onto
``CONFIG_LEAVE_GROUP`` in ``helpers.core._PREFIXED_ACTIONS`` and would misroute
to the modal-opening handler. Destructive confirmations are red in-modal buttons
(a modal submit button cannot be coloured), so this is a block action."""

CONFIG_ACCEPT_GROUP_INVITE = "accept_group_invite"
"""Action: user clicked "Accept" on an incoming group invite (prefix-matched with member_id)."""

CONFIG_CANCEL_GROUP_INVITE = "cancel_group_invite"
"""Action: user clicked "Cancel Invite" on an outgoing group invite (prefix-matched with member_id)."""

CONFIG_INVITE_WORKSPACE = "invite_workspace"
"""Action: user clicked "Invite Workspace" button on a group (value carries group_id)."""

CONFIG_INVITE_WORKSPACE_SUBMIT = "invite_workspace_submit"
"""Callback: invite-workspace modal submitted (sends DM invite to selected workspace)."""

CONFIG_INVITE_WORKSPACE_SELECT = "invite_workspace_select"
"""Input: workspace picker dropdown in the invite workspace modal."""

CONFIG_DECLINE_GROUP_INVITE = "decline_group_invite"
"""Action: user clicked "Decline" on an incoming group invite DM (prefix-matched with member_id)."""

CONFIG_PROMOTE_TO_OWNER = "promote_to_owner"
"""Action: an owner promoted another member to owner (prefix-matched with member_id)."""

CONFIG_DEMOTE_SELF = "demote_self"
"""Action: an owner gave up its own ownership (prefix-matched with member_id). Self-demotion only."""

CONFIG_DISBAND_GROUP = "disband_group"
"""Action: sole owner clicked "Disband Group" (prefix-matched with group_id)."""

CONFIG_DISBAND_GROUP_CONFIRM = "confirm_disband_group"
"""Action (block): red confirm button inside the disband-group modal.

Not ``disband_group_confirm``: that string is prefix-matched onto
``CONFIG_DISBAND_GROUP`` and would misroute to the modal-opening handler."""

# ---------------------------------------------------------------------------
# Instance settings (PRIMARY_WORKSPACE only)
# ---------------------------------------------------------------------------

CONFIG_OPEN_SETTINGS = "open_settings"
"""Action: operator clicked "Settings" in the SyncBot Configuration row."""

CONFIG_SETTINGS_SUBMIT = "settings_submit"
"""Callback: instance settings modal submitted."""

CONFIG_SETTINGS_ALLOW_PRIVATE_CHANNELS = "settings_allow_private_channels"
"""Input: whether private channels may be selected in this workspace."""

CONFIG_SETTINGS_EXTRA_MANAGERS = "settings_extra_managers"
"""Input: extra user IDs who may configure groups and syncs in this workspace."""

CONFIG_SETTINGS_RETENTION_DAYS = "settings_retention_days"
"""Input: days a soft-deleted Workspace is retained before permanent removal."""

CONFIG_SETTINGS_FEDERATION_ENABLED = "settings_federation_enabled"
"""Input: whether External Connections (federation) are enabled."""

CONFIG_SETTINGS_WORKSPACE_BLOCK_LIST = "settings_workspace_block_list"
"""Input: Slack Team IDs that may not install or reinstall on this instance."""

# ---------------------------------------------------------------------------
# Channel Sync actions
# ---------------------------------------------------------------------------

CONFIG_CREATE_SYNC = "create_sync"
"""Action: user clicked "Create Sync" (value carries group_id)."""

CONFIG_CREATE_SYNC_SELECT = "create_sync_select"
"""Input: channel picker in the Create Sync modal."""

CONFIG_CREATE_SYNC_SUBMIT = "create_sync_submit"
"""Callback: Create Sync modal submitted."""

CONFIG_JOIN_SYNC = "join_sync"
"""Action: user clicked "Join Sync" on an available relationship (prefix-matched with sync_id)."""

CONFIG_JOIN_SYNC_SELECT = "select_join_sync"
"""Input: channel picker in the Join Sync modal.

Not ``join_sync_select``: that string is prefix-matched onto ``CONFIG_JOIN_SYNC``
in ``helpers.core._PREFIXED_ACTIONS`` and would re-open Join Sync when the user
picks a Channel."""

CONFIG_JOIN_SYNC_SUBMIT = "join_sync_submit"
"""Callback: Join Sync modal submitted."""

CONFIG_SYNC_PARTICIPATION = "sync_participation"
"""Input: Publish only / Subscribe only / Publish and Subscribe."""

CONFIG_SYNC_REACTION_STYLE = "sync_reaction_style"
"""Input: Hybrid, Direct, or Off on Create, Join, and Edit Sync."""

CONFIG_EDIT_SYNC = "edit_sync"
"""Action: user clicked Edit Sync on a synced Channel row (prefix-matched; value encodes channel or sync)."""

CONFIG_EDIT_SYNC_SUBMIT = "edit_sync_submit"
"""Callback: Edit Sync modal submitted (participation and/or reactions)."""

CONFIG_LEAVE_SYNC = "leave_sync"
"""Action: user clicked "Leave Sync" (prefix-matched with sync_id)."""

CONFIG_LEAVE_SYNC_CONFIRM = "confirm_leave_sync"
"""Action (block): red confirm button inside the Leave Sync modal.

Not ``leave_sync_confirm``: that string is prefix-matched onto
``CONFIG_LEAVE_SYNC`` and would misroute to the modal-opening handler."""

CONFIG_PAUSE_SYNC = "pause_sync"
"""Action: user clicked "Pause Sync" (prefix-matched with sync_id). Opens confirm."""

CONFIG_PAUSE_SYNC_CONFIRM = "confirm_pause_sync"
"""Action (block): confirm button inside the Pause Sync modal. Not red."""

CONFIG_RESUME_SYNC = "resume_sync"
"""Action: user clicked "Resume Sync" (prefix-matched with sync_id). Opens confirm."""

CONFIG_RESUME_SYNC_CONFIRM = "confirm_resume_sync"
"""Action (block): confirm button inside the Resume Sync modal. Not red."""

# ---------------------------------------------------------------------------
# Home Tab actions
# ---------------------------------------------------------------------------

CONFIG_REFRESH_HOME = "refresh_home"
"""Action: user clicked the "Refresh" button on the Home tab."""

CONFIG_AUTHORIZE_SYNCBOT = "authorize_syncbot"
"""Action: user clicked "Authorize SyncBot" on the Home tab.

The button carries a ``url``, so Slack opens the OAuth install itself. Slack
still delivers a ``block_actions`` payload for it, which is why this needs a
registered (no-op) handler.
"""

CONFIG_BACKUP_RESTORE = "backup_restore"
"""Action: user clicked "Backup/Restore" on the Home tab (opens modal)."""

CONFIG_BACKUP_RESTORE_SUBMIT = "backup_restore_submit"
"""Callback: Backup/Restore modal submitted (restore from backup)."""

CONFIG_BACKUP_RESTORE_PROCEED = "backup_restore_proceed"
"""Action: danger button to proceed with restore despite warnings."""

CONFIG_BACKUP_DOWNLOAD = "backup_download"
"""Action: user clicked Download backup in Backup/Restore modal."""

CONFIG_BACKUP_RESTORE_JSON_INPUT = "backup_restore_json_input"
"""Input: uploaded JSON file in Backup/Restore modal."""

CONFIG_DATA_MIGRATION = "data_migration"
"""Action: user clicked "Data Migration" in SyncBot Configuration (opens modal)."""

CONFIG_DATA_MIGRATION_SUBMIT = "data_migration_submit"
"""Callback: Data Migration modal submitted (import migration file)."""

CONFIG_DATA_MIGRATION_REVIEW = "data_migration_review"
"""Callback: Data Migration review modal submitted (connect + import)."""

CONFIG_DATA_MIGRATION_PROCEED = "data_migration_proceed"
"""Action: danger button to proceed with import despite warnings."""

CONFIG_DATA_MIGRATION_EXPORT = "data_migration_export"
"""Action: user clicked Export in Data Migration modal."""

CONFIG_DATA_MIGRATION_REQUEST = "data_migration_request"
"""Action: workspace admin clicked Export and Request Connection."""

CONFIG_PAIRING_REQUEST_APPROVE = "pairing_request_approve"
"""Action: primary admin approved a migration pairing request (prefix-matched with request ID)."""

CONFIG_PAIRING_REQUEST_DECLINE = "pairing_request_decline"
"""Action: primary admin declined a migration pairing request (prefix-matched with request ID)."""

CONFIG_DATA_MIGRATION_JSON_INPUT = "data_migration_json_input"
"""Input: uploaded JSON file in Data Migration modal."""

# ---------------------------------------------------------------------------
# External Connections (federation) actions
# ---------------------------------------------------------------------------

CONFIG_CREATE_EXTERNAL_CONNECTION = "create_external_connection"
"""Action: user clicked "Create External Connection" on the Home tab."""

CONFIG_SHOW_EXTERNAL_CONNECTION_CODE = "show_external_connection_code"
"""Action: Show Connection Code on a waiting connection (prefix-matched with pairing id)."""

CONFIG_EDIT_PENDING_EXTERNAL_CONNECTION = "edit_pending_external_connection"
"""Action: Edit local Workspaces on a waiting connection (prefix-matched with pairing id)."""

CONFIG_CANCEL_PENDING_EXTERNAL_CONNECTION = "cancel_pending_external_connection"
"""Action: Cancel a waiting connection (prefix-matched with pairing id)."""

CONFIG_CANCEL_PENDING_EXTERNAL_CONNECTION_CONFIRM = "confirm_cancel_pending_external_connection"
"""Action: red in-modal confirm for Cancel on a waiting connection."""

CONFIG_CREATE_EXTERNAL_CONNECTION_SUBMIT = "create_external_connection_submit"
"""Callback: Create External Connection modal submitted."""

CONFIG_CREATE_EXTERNAL_CONNECTION_NAME = "create_external_connection_name"
"""Input: connection name on the Create External Connection modal."""

CONFIG_CREATE_EXTERNAL_WORKSPACES = "select_create_external_workspaces"
"""Input: allowed local workspaces on the Create External Connection modal."""

CONFIG_JOIN_EXTERNAL_CONNECTION = "join_external_connection"
"""Action: user clicked "Join External Connection" on the Home tab."""

CONFIG_JOIN_EXTERNAL_CONNECTION_SUBMIT = "join_external_connection_submit"
"""Callback: Join External Connection paste-code modal submitted (ack updates to review)."""

CONFIG_JOIN_EXTERNAL_CONNECTION_CODE = "join_external_connection_code"
"""Input: pasted connection code on the Join External Connection modal."""

CONFIG_JOIN_EXTERNAL_CONNECTION_REVIEW = "join_external_connection_review"
"""Callback: Join External Connection review modal submitted."""

CONFIG_JOIN_EXTERNAL_WORKSPACES = "select_join_external_workspaces"
"""Input: allowed local workspaces on the Join External Connection review modal."""

CONFIG_EDIT_EXTERNAL_CONNECTION = "edit_external_connection"
"""Action: user clicked "Edit Connection" (prefix-matched with instance_id)."""

CONFIG_EDIT_EXTERNAL_CONNECTION_SUBMIT = "edit_external_connection_submit"
"""Callback: Edit Connection modal submitted."""

CONFIG_EDIT_EXTERNAL_WORKSPACES = "select_edit_external_workspaces"
"""Input: allowed local workspaces on the Edit Connection modal."""

CONFIG_EDIT_EXTERNAL_CONNECTION_NAME = "edit_external_connection_name"
"""Input: connection name on the Edit Connection modal."""

CONFIG_VERIFY_EXTERNAL_CONNECTION = "verify_external_connection"
"""Action: user clicked "Verify Trust" (prefix-matched with instance_id)."""

CONFIG_VERIFY_EXTERNAL_CONNECTION_SUBMIT = "verify_external_connection_submit"
"""Callback: Verify Trust modal submitted."""

CONFIG_LEAVE_EXTERNAL_CONNECTION = "leave_external_connection"
"""Action: user clicked "Leave Connection" (prefix-matched with instance_id)."""

CONFIG_LEAVE_EXTERNAL_CONNECTION_CONFIRM = "confirm_leave_external_connection"
"""Action: red in-modal confirm for Leave Connection."""

# ---------------------------------------------------------------------------
# Database Reset (dev/admin tool, gated by PRIMARY_WORKSPACE + ENABLE_DB_RESET)
# ---------------------------------------------------------------------------

CONFIG_DB_RESET = "db_reset"
"""Action: user clicked "Reset Database" on the Home tab."""

CONFIG_DB_RESET_PROCEED = "db_reset_proceed"
"""Action: danger button to proceed with database reset."""
