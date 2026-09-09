"""Home-tab builder tests for Create / Join / Leave Sync buttons."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from builders.channel_sync import _available_channel_label, _build_inline_channel_sync  # noqa: E402
from db.schemas import Sync  # noqa: E402
from slack import actions  # noqa: E402

GROUP_ID = 5
SYNC_ID = 42
PUBLISHER_WS = 1
SUBSCRIBER_WS = 2
VIEWER_WS = 99


def _channel(cid, ws, *, publishes=True, subscribes=True):
    return SimpleNamespace(
        id=cid,
        workspace_id=ws,
        channel_id=f"C_{cid}",
        status="active",
        created_at=datetime.now(UTC).replace(tzinfo=None),
        publishes=publishes,
        subscribes=subscribes,
    )


def _buttons(blocks):
    """Flatten every (label, action_id) button across ActionsBlocks."""
    out = []
    for block in blocks:
        for element in getattr(block, "elements", None) or []:
            action = getattr(element, "action", None)
            if action:
                out.append((getattr(element, "label", None), action))
    return out


def _render(
    *,
    viewer_ws,
    publisher_ws,
    channels,
    is_owner: bool = False,
    sync_mode: str = "group",
    duplicate_source: bool = False,
):
    sync = SimpleNamespace(
        id=SYNC_ID,
        group_id=GROUP_ID,
        title="2nd-f",
        sync_mode=sync_mode,
        publisher_workspace_id=publisher_ws,
        target_workspace_id=viewer_ws if sync_mode == "direct" else None,
    )
    group = SimpleNamespace(id=GROUP_ID)
    workspace_record = SimpleNamespace(id=viewer_ws, team_id="T1")

    def find_records(model, _filters):
        return [sync] if model is Sync else channels

    blocks: list = []
    with (
        patch("builders.channel_sync.DbManager.find_records", side_effect=find_records),
        patch("builders.channel_sync.DbManager.count_records", return_value=0),
        patch("builders.channel_sync.helpers.resolve_workspace_name", return_value="WS"),
        patch("builders.channel_sync.helpers.lookup_channel_meta", return_value=("partner-ch", False)),
        patch("builders.channel_sync.helpers.get_workspace_by_id", return_value=SimpleNamespace(bot_token=None)),
        patch("builders.channel_sync.helpers.is_workspace_owner", return_value=is_owner),
        patch(
            "builders.channel_sync.helpers.already_subscribed_to_source",
            return_value=duplicate_source,
        ),
        patch("builders.channel_sync._format_channel_ref", return_value="#c"),
    ):
        _build_inline_channel_sync(blocks, group, workspace_record, other_members=[], context={})
    return blocks


def test_synced_row_members_are_partner_channel_ticks():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS), _channel(11, SUBSCRIBER_WS)],
    )
    texts = "\n".join(_context_texts(blocks))
    assert "Members: `#partner-ch (WS)`" in texts
    assert "Status: `Active (Publish and Subscribe)`" in texts


def test_synced_row_paused_partner_is_tagged():
    paused = _channel(11, SUBSCRIBER_WS)
    paused.status = "paused"
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS), paused],
    )
    texts = "\n".join(_context_texts(blocks))
    assert "Members: `#partner-ch (WS)` `(Paused)`" in texts


def test_active_row_offers_leave_sync():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS), _channel(11, SUBSCRIBER_WS)],
    )
    actions_seen = [a for _, a in _buttons(blocks)]

    assert any(a.startswith(actions.CONFIG_LEAVE_SYNC) for a in actions_seen)
    assert not any(a.startswith("unpublish_channel") for a in actions_seen)
    assert not any(a.startswith("stop_sync") for a in actions_seen)


def test_subscriber_active_row_also_offers_leave_sync():
    blocks = _render(
        viewer_ws=SUBSCRIBER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, SUBSCRIBER_WS, publishes=False), _channel(11, PUBLISHER_WS)],
    )
    actions_seen = [a for _, a in _buttons(blocks)]

    assert any(a.startswith(actions.CONFIG_LEAVE_SYNC) for a in actions_seen)
    assert not any(a.startswith("unpublish_channel") for a in actions_seen)
    assert not any(a.startswith("stop_sync") for a in actions_seen)
    texts = "\n".join(_context_texts(blocks))
    assert "Status: `Active (Subscribe only)`" in texts


def test_publisher_only_row_status_includes_participation():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS, subscribes=False), _channel(11, SUBSCRIBER_WS)],
    )
    texts = "\n".join(_context_texts(blocks))
    assert "Status: `Active (Publish only)`" in texts


def test_paused_local_row_status_includes_participation():
    paused_mine = _channel(10, PUBLISHER_WS)
    paused_mine.status = "paused"
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[paused_mine, _channel(11, SUBSCRIBER_WS)],
    )
    texts = "\n".join(_context_texts(blocks))
    assert "Status: `Paused (Publish and Subscribe)`" in texts


def test_active_row_edit_is_first_button():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS), _channel(11, SUBSCRIBER_WS)],
    )
    labels = [label for label, _ in _buttons(blocks)]
    assert labels[0] == ":pencil2: Edit Sync"
    assert labels == [":pencil2: Edit Sync", ":double_vertical_bar: Pause Sync", ":octagonal_sign: Leave Sync"]
    edit_action = _buttons(blocks)[0][1]
    assert edit_action == f"{actions.CONFIG_EDIT_SYNC}_c_10"


def test_subscriber_active_row_edit_then_pause_then_leave():
    blocks = _render(
        viewer_ws=SUBSCRIBER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, SUBSCRIBER_WS, publishes=False), _channel(11, PUBLISHER_WS)],
    )
    labels = [label for label, _ in _buttons(blocks)]
    assert labels == [":pencil2: Edit Sync", ":double_vertical_bar: Pause Sync", ":octagonal_sign: Leave Sync"]


def test_waiting_publisher_has_edit_then_leave():
    blocks = _render(
        viewer_ws=PUBLISHER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS)],
    )
    labels = [label for label, _ in _buttons(blocks)]
    assert labels == [":pencil2: Edit Sync", ":octagonal_sign: Leave Sync"]


def test_stranded_member_has_leave_without_edit():
    blocks = _render(
        viewer_ws=SUBSCRIBER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, SUBSCRIBER_WS, publishes=False, subscribes=True)],
    )
    labels = [label for label, _ in _buttons(blocks)]
    assert labels == [":octagonal_sign: Leave Sync"]
    assert not any(label == "Edit Sync" for label in labels)


def test_available_row_has_one_join_action():
    owner_blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS)],
        is_owner=True,
    )
    owner_labels = [label for label, _ in _buttons(owner_blocks)]
    assert owner_labels == [":inbox_tray: Join Sync"]
    headers = [
        getattr(block, "label", None) or ""
        for block in owner_blocks
        if "Available Sync Relationships" in (getattr(block, "label", None) or "")
    ]
    assert len(headers) == 1

    member_blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS)],
        is_owner=False,
    )
    member_labels = [label for label, _ in _buttons(member_blocks)]
    assert member_labels == [":inbox_tray: Join Sync"]


def test_duplicate_source_disables_join():
    blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS)],
        duplicate_source=True,
    )
    assert not _buttons(blocks)
    assert any("Already subscribed" in text for text in _context_texts(blocks))


def test_home_does_not_show_one_to_one_type_label():
    group_blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS)],
    )
    direct_blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, PUBLISHER_WS)],
        sync_mode="direct",
    )
    assert not any("Type:" in text for text in _context_texts(group_blocks))
    assert not any("1-to-1" in text for text in _context_texts(direct_blocks))


def test_orphaned_sync_is_not_advertised_as_available():
    """A sync with no remaining publishers must not be offered as Join Sync."""
    blocks = _render(
        viewer_ws=VIEWER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(11, SUBSCRIBER_WS, publishes=False)],
    )
    actions_seen = [a for _, a in _buttons(blocks)]

    assert not any(a.startswith(actions.CONFIG_JOIN_SYNC) for a in actions_seen)
    assert not any(a.startswith("subscribe_channel") for a in actions_seen)


def test_stranded_member_can_leave_the_orphan():
    """The remaining subscriber (no publishers) gets Leave Sync."""
    blocks = _render(
        viewer_ws=SUBSCRIBER_WS,
        publisher_ws=PUBLISHER_WS,
        channels=[_channel(10, SUBSCRIBER_WS, publishes=False)],
    )
    actions_seen = [a for _, a in _buttons(blocks)]

    assert any(a.startswith(actions.CONFIG_LEAVE_SYNC) for a in actions_seen)


def _context_texts(blocks) -> list[str]:
    texts = []
    for block in blocks:
        element = getattr(block, "element", None)
        if element is not None:
            texts.append(getattr(element, "initial_value", "") or "")
    return texts


def test_available_channel_label_uses_live_name_not_stored_id():
    ws = SimpleNamespace(id=1, bot_token="enc")
    with patch("builders.channel_sync.helpers.lookup_channel_meta", return_value=("2nd-f", False)):
        assert _available_channel_label("C123ABC", ws, "C123ABC") == "2nd-f"


def test_available_channel_label_tags_private():
    ws = SimpleNamespace(id=1, bot_token="enc")
    with patch("builders.channel_sync.helpers.lookup_channel_meta", return_value=("leadership", True)):
        assert _available_channel_label("CPRIV", ws, "CPRIV") == "leadership (private)"


def test_available_row_shows_name_in_ticks_and_tags_private():
    """``sync.title`` was the Channel ID when the bot looked up the name before joining."""
    sync = SimpleNamespace(
        id=SYNC_ID,
        group_id=GROUP_ID,
        title="C_10",
        sync_mode="group",
        publisher_workspace_id=PUBLISHER_WS,
        target_workspace_id=None,
    )
    group = SimpleNamespace(id=GROUP_ID)
    workspace_record = SimpleNamespace(id=VIEWER_WS, team_id="T1")
    channels = [_channel(10, PUBLISHER_WS)]

    def find_records(model, _filters):
        return [sync] if model is Sync else channels

    blocks: list = []
    with (
        patch("builders.channel_sync.DbManager.find_records", side_effect=find_records),
        patch("builders.channel_sync.DbManager.count_records", return_value=0),
        patch("builders.channel_sync.helpers.resolve_workspace_name", return_value="WS"),
        patch("builders.channel_sync.helpers.get_workspace_by_id", return_value=SimpleNamespace(bot_token=None)),
        patch("builders.channel_sync.helpers.lookup_channel_meta", return_value=("2nd-f", True)),
        patch("builders.channel_sync.helpers.is_workspace_owner", return_value=False),
        patch("builders.channel_sync.helpers.already_subscribed_to_source", return_value=False),
        patch("builders.channel_sync._format_channel_ref", return_value="#c"),
    ):
        _build_inline_channel_sync(blocks, group, workspace_record, other_members=[], context={})

    joined = "\n".join(_context_texts(blocks))
    assert "Channel: `2nd-f (private)`" in joined
    assert "`C_10`" not in joined
    assert ":lock:" not in joined
    assert "#2nd-f" not in joined
    assert any(a.startswith(actions.CONFIG_JOIN_SYNC) for _, a in _buttons(blocks))
