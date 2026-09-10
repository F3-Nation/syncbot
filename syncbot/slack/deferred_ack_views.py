"""View submission callback IDs whose handlers control the Slack interaction HTTP ack.

Kept separate from :mod:`app` so tests can import it without initializing the database.
"""

from slack.actions import (
    CONFIG_BACKUP_RESTORE_SUBMIT,
    CONFIG_CREATE_SYNC_SUBMIT,
    CONFIG_DATA_MIGRATION_SUBMIT,
    CONFIG_EDIT_SYNC_SUBMIT,
    CONFIG_JOIN_SYNC_SUBMIT,
)

DEFERRED_ACK_VIEW_CALLBACK_IDS = frozenset(
    {
        CONFIG_CREATE_SYNC_SUBMIT,
        CONFIG_JOIN_SYNC_SUBMIT,
        CONFIG_EDIT_SYNC_SUBMIT,
        CONFIG_BACKUP_RESTORE_SUBMIT,
        CONFIG_DATA_MIGRATION_SUBMIT,
    }
)
