"""Focused unit tests for channel sync handler branches."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from handlers.channel_sync import (  # noqa: E402
    handle_create_sync_submit_ack,
    handle_join_sync_submit,
    handle_leave_sync_confirm,
)
from slack import actions  # noqa: E402


class TestLeaveSyncConfirm:
    """Leave removes this workspace; the last publisher ends the sync for everyone."""

    SYNC_ID = 31

    def _channel(self, cid, ws, *, publishes=True):
        return SimpleNamespace(
            id=cid, workspace_id=ws, channel_id=f"C_{cid}", status="active", publishes=publishes, subscribes=True
        )

    def _run(self, *, acting_ws, all_channels, purge_side_effect=None):
        workspace = SimpleNamespace(id=acting_ws, team_id="T1", bot_token=None, deleted_at=None)
        sync = SimpleNamespace(id=self.SYNC_ID, publisher_workspace_id=1, group_id=None)
        client = MagicMock()

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": self.SYNC_ID}),
            patch("handlers.channel_sync.DbManager.get_record", return_value=sync),
            patch("handlers.channel_sync.DbManager.find_records", return_value=all_channels),
            patch("handlers.channel_sync.helpers.format_admin_label", return_value=("Admin", "Admin (WS)")),
            patch("handlers.channel_sync.helpers.get_workspace_by_id", return_value=SimpleNamespace(bot_token=None)),
            patch("handlers.channel_sync.helpers.purge_sync_channels") as purge_channels,
            patch("handlers.channel_sync.helpers.purge_sync", side_effect=purge_side_effect) as purge_sync,
            patch("handlers.channel_sync.helpers.notify_admins_dm") as notify,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace") as refresh,
            patch("handlers.channel_sync._close_modal_done"),
            patch("handlers.channel_sync._logger.error") as error_log,
        ):
            handle_leave_sync_confirm({}, client, MagicMock(), context={})

        return purge_channels, purge_sync, notify, refresh, error_log

    def test_last_publisher_purges_the_sync(self):
        mine = self._channel(10, 10, publishes=True)
        other = self._channel(11, 2, publishes=False)
        purge_channels, purge_sync, _, refresh, error_log = self._run(acting_ws=10, all_channels=[mine, other])

        assert not purge_channels.called
        purge_sync.assert_called_once_with(self.SYNC_ID)
        assert refresh.called
        assert not error_log.called

    def test_last_publisher_purge_failure_is_logged_and_reported(self):
        mine = self._channel(10, 10, publishes=True)
        _, purge_sync, notify, refresh, error_log = self._run(
            acting_ws=10, all_channels=[mine], purge_side_effect=RuntimeError("fk violation")
        )

        assert purge_sync.called
        assert notify.called
        assert not refresh.called
        assert error_log.call_args.args[0] == "leave_sync_failed"
        dm_text = notify.call_args.args[1]
        assert "fk violation" in dm_text

    def test_peer_publisher_leave_keeps_the_sync(self):
        mine = self._channel(10, 10, publishes=True)
        other = self._channel(11, 2, publishes=True)
        purge_channels, purge_sync, _, _, _ = self._run(acting_ws=10, all_channels=[mine, other])

        assert purge_channels.call_args.args[0] == [mine]
        assert not purge_sync.called

    def test_subscriber_leave_keeps_the_sync_when_others_remain(self):
        mine = self._channel(10, 2, publishes=False)
        other = self._channel(11, 1, publishes=True)
        purge_channels, purge_sync, _, _, _ = self._run(acting_ws=2, all_channels=[mine, other])

        assert purge_channels.call_args.args[0] == [mine]
        assert not purge_sync.called

    def test_last_member_leave_purges_the_empty_sync(self):
        mine = self._channel(10, 2, publishes=False)
        purge_channels, purge_sync, _, _, _ = self._run(acting_ws=2, all_channels=[mine])

        assert purge_channels.call_args.args[0] == [mine]
        purge_sync.assert_called_once_with(self.SYNC_ID)


class TestCreateSyncSubmitAck:
    def test_missing_group_id_exits_early(self):
        client = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={}),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            result = handle_create_sync_submit_ack({}, client, context)

        assert result is None
        create_record.assert_not_called()

    def test_missing_channel_selection_returns_ack_error(self):
        client = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"group_id": 7}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="__none__"),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            result = handle_create_sync_submit_ack({}, client, context)

        assert result is not None
        assert result["response_action"] == "errors"
        assert "Select a Channel." in result["errors"].values()
        create_record.assert_not_called()

    def test_existing_sync_channel_can_publish_again(self):
        client = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"group_id": 7}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="C123"),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=True),
            patch("handlers.channel_sync.helpers.has_user_token", return_value=True),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            result = handle_create_sync_submit_ack({}, client, context)

        assert result is None
        create_record.assert_not_called()


class TestJoinSyncSubmit:
    def test_missing_sync_id_exits_early(self):
        client = MagicMock()
        logger = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={}),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            handle_join_sync_submit({}, client, logger, context)

        create_record.assert_not_called()

    def test_missing_channel_selection_exits_early(self):
        client = MagicMock()
        logger = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": 55}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="__none__"),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
        ):
            handle_join_sync_submit({}, client, logger, context)

        create_record.assert_not_called()

    def test_duplicate_source_validation_stops_work(self):
        client = MagicMock()
        logger = MagicMock()
        context = {}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": 55}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="Cdup"),
            patch("handlers.channel_sync._validate_channel_selection", return_value={"response_action": "errors"}),
            patch("handlers.channel_sync.DbManager.create_record") as create_record,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace") as refresh_home,
        ):
            handle_join_sync_submit({"user": {"id": "U1"}}, client, logger, context)

        create_record.assert_not_called()
        client.conversations_join.assert_not_called()
        refresh_home.assert_not_called()


class TestJoinSyncModal:
    def test_opens_one_screen_with_source_group_picker_and_reaction_type(self):
        from handlers.channel_sync import handle_join_sync

        workspace = SimpleNamespace(id=10, team_id="T1")
        pub = SimpleNamespace(id=1, channel_id="C_PUB", workspace_id=99, publishes=True)
        captured = {}

        def capture(self, **kwargs):
            captured["blocks"] = list(self.blocks)
            captured["kwargs"] = kwargs

        body = {
            "trigger_id": "tr",
            "actions": [{"value": "55", "action_id": "join_sync_55"}],
        }
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync.DbManager.get_record", return_value=SimpleNamespace(id=55, group_id=5)),
            patch("handlers.channel_sync._publisher_sync_channel", return_value=pub),
            patch("handlers.channel_sync.helpers.get_workspace_by_id", return_value=SimpleNamespace(id=99)),
            patch("handlers.channel_sync._group_name", return_value="HQ"),
            patch("handlers.channel_sync._format_channel_ref", return_value="#source (Other)"),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
            patch("handlers.channel_sync.orm.BlockView.post_modal", capture),
        ):
            handle_join_sync(body, MagicMock(), MagicMock(), {})

        ids = [getattr(block, "action", None) for block in captured["blocks"]]
        texts = [getattr(getattr(block, "element", None), "initial_value", None) for block in captured["blocks"]]
        participation = next(
            block for block in captured["blocks"] if getattr(block, "action", None) == actions.CONFIG_SYNC_PARTICIPATION
        )
        option_values = [opt.value for opt in participation.element.options]
        assert option_values == ["publish_only", "subscribe_only", "publish_and_subscribe"]
        assert actions.CONFIG_JOIN_SYNC_SELECT in ids
        assert actions.CONFIG_SYNC_REACTION_STYLE in ids
        assert captured["kwargs"]["callback_id"] == actions.CONFIG_JOIN_SYNC_SUBMIT
        assert captured["kwargs"]["submit_button_text"] == "Join Sync"
        assert captured["kwargs"]["title_text"] == "Join Sync"
        assert any(text and "HQ" in text and "#source (Other)" in text for text in texts)


class TestChannelSyncNotices:
    def test_create_sync_notice_two_way_and_one_way(self):
        from handlers.channel_sync import _create_sync_notice

        both = _create_sync_notice("Ada", publishes=True, subscribes=True)
        assert both.startswith(":arrows_counterclockwise:")
        assert "created a Sync for this Channel" in both
        assert "send and receive" in both

        one = _create_sync_notice("Ada", publishes=True, subscribes=False)
        assert "will be sent to Workspaces that join" in one
        assert "receive" not in one

    def test_join_notice_names_admin_channel_and_direction(self):
        from handlers.channel_sync import _join_notice

        peer = _join_notice(
            admin_label="Other Admin (Other Workspace)",
            other_ref="#other-channel (Other Workspace)",
            here_publishes=True,
            here_subscribes=True,
            there_publishes=False,
            there_subscribes=True,
            joined=True,
        )
        assert peer.startswith(":arrows_counterclockwise:")
        assert "Other Admin (Other Workspace)" in peer
        assert "subscribed *#other-channel (Other Workspace)* to this Channel" in peer
        assert "Messages from here will be sent to the Channel in the other Workspace" in peer

        local = _join_notice(
            admin_label="Ada",
            other_ref="#source (HQ)",
            here_publishes=False,
            here_subscribes=True,
            there_publishes=True,
            there_subscribes=True,
            joined=False,
        )
        assert "subscribed this Channel to *#source (HQ)*" in local
        assert "Messages from that Channel will appear here" in local
