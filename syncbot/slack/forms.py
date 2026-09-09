"""Pre-built Slack Block Kit forms for SyncBot configuration modals.

Defines reusable form templates that are deep-copied and customised at
runtime before being sent to Slack:

* :data:`ENTER_GROUP_CODE_FORM` — Modal for entering a group invite code.
* :data:`CREATE_SYNC_FORM` — Channel picker template for Create Sync.
* :data:`JOIN_SYNC_FORM` — Channel picker template for Join Sync.

Any form containing a :class:`~slack.orm.ConversationsSelectElement` defaults to
public channels only. Because these are module-level constants, the
``allow_private_channels`` policy cannot be baked in here without going stale, so
callers must apply it to their deep copy via
:meth:`~slack.orm.BlockView.set_conversations_include_private`.
"""

from slack import actions, orm

ENTER_GROUP_CODE_FORM = orm.BlockView(
    blocks=[
        orm.InputBlock(
            label="Group Invite Code",
            action=actions.CONFIG_JOIN_GROUP_CODE,
            element=orm.PlainTextInputElement(placeholder="Enter the code (e.g. A7X-K9M)"),
            optional=False,
        ),
        orm.ContextBlock(
            element=orm.ContextElement(
                initial_value="Enter the invite code shared by an Admin from another Workspace in the Group.",
            ),
        ),
    ]
)


CREATE_SYNC_FORM = orm.BlockView(
    blocks=[
        orm.InputBlock(
            label="Channel",
            action=actions.CONFIG_CREATE_SYNC_SELECT,
            element=orm.ConversationsSelectElement(placeholder="Search for a Channel"),
            optional=False,
        ),
        orm.ContextBlock(
            element=orm.ContextElement(
                initial_value="Select a Channel from your Workspace to create a Sync.",
            ),
        ),
    ]
)


JOIN_SYNC_FORM = orm.BlockView(
    blocks=[
        orm.InputBlock(
            label="Channel",
            action=actions.CONFIG_JOIN_SYNC_SELECT,
            element=orm.ConversationsSelectElement(placeholder="Search for a Channel"),
            optional=False,
        ),
        orm.ContextBlock(
            element=orm.ContextElement(
                initial_value="Select a Channel in your Workspace to join this Sync.",
            ),
        ),
    ]
)
