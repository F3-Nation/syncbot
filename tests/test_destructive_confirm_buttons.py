"""Destructive confirmations use a red in-modal button, not a modal submit button.

A Slack modal's submit button is rendered in the workspace theme colour and
cannot be styled, so every destructive confirmation (leave group, disband group,
Leave Sync) presents its affirmative action as a ``danger`` (red) button inside
the modal body and runs the work as a *block action*. These tests lock in that
contract and the routing/prefix wiring that makes it reachable.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from helpers.core import get_request_type  # noqa: E402
from routing import ACTION_MAPPER, VIEW_MAPPER  # noqa: E402
from slack import actions  # noqa: E402

_CONFIRM_ACTIONS = (
    actions.CONFIG_LEAVE_GROUP_CONFIRM,
    actions.CONFIG_DISBAND_GROUP_CONFIRM,
    actions.CONFIG_LEAVE_SYNC_CONFIRM,
)


def _danger_buttons(view: dict) -> list[dict]:
    """Return every ``danger``-styled button element in a modal view."""
    found = []
    for block in view.get("blocks", []):
        if block.get("type") != "actions":
            continue
        for element in block.get("elements", []):
            if element.get("type") == "button" and element.get("style") == "danger":
                found.append(element)
    return found


class TestConfirmActionRouting:
    def test_confirmations_are_block_actions_not_view_submissions(self):
        for action_id in _CONFIRM_ACTIONS:
            assert action_id in ACTION_MAPPER, f"{action_id!r} must be a block action"
            assert action_id not in VIEW_MAPPER, f"{action_id!r} must not be a view submission"

    def test_confirm_ids_do_not_collapse_onto_a_trigger_prefix(self):
        """The prefix matcher must not fold e.g. ``confirm_leave_group`` into ``leave_group``."""
        for action_id in _CONFIRM_ACTIONS:
            body = {"type": "block_actions", "actions": [{"action_id": action_id}]}
            category, resolved = get_request_type(body)
            assert category == "block_actions"
            assert resolved == action_id


class TestLeaveSyncConfirmModal:
    def _view(self, *, last_publisher=False):
        from handlers.channel_sync import handle_leave_sync

        client = MagicMock()
        body = {"actions": [{"action_id": f"{actions.CONFIG_LEAVE_SYNC}_42", "value": "42"}], "trigger_id": "tr"}
        workspace = SimpleNamespace(id=1, team_id="T1")
        mine = SimpleNamespace(id=10, workspace_id=1, channel_id="C1", publishes=True)
        other = SimpleNamespace(id=11, workspace_id=2, channel_id="C2", publishes=not last_publisher)
        channels = [mine] if last_publisher else [mine, other]
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync.DbManager.get_record", return_value=SimpleNamespace(id=42, group_id=5)),
            patch("handlers.channel_sync.DbManager.find_records", return_value=channels),
            patch("handlers.channel_sync._group_name", return_value="HQ"),
            patch("handlers.channel_sync._format_channel_ref", return_value="#c"),
        ):
            handle_leave_sync(body, client, MagicMock(), context={})
        return client.views_open.call_args.kwargs["view"]

    def test_has_a_red_button_and_no_submit_button(self):
        view = self._view()
        assert "submit" not in view
        buttons = _danger_buttons(view)
        assert len(buttons) == 1
        assert buttons[0]["action_id"] == actions.CONFIG_LEAVE_SYNC_CONFIRM

    def test_last_publisher_warns_that_the_sync_will_end(self):
        view = self._view(last_publisher=True)
        texts = [block.get("text", {}).get("text", "") for block in view.get("blocks", [])]
        assert any("last publisher" in text for text in texts)


class TestPauseSyncConfirmModal:
    def test_has_a_non_red_button_and_no_submit_button(self):
        from handlers.channel_sync import handle_pause_sync

        client = MagicMock()
        body = {"actions": [{"action_id": f"{actions.CONFIG_PAUSE_SYNC}_42", "value": "42"}], "trigger_id": "tr"}
        handle_pause_sync(body, client, MagicMock(), context={})
        view = client.views_open.call_args.kwargs["view"]
        assert "submit" not in view
        assert _danger_buttons(view) == []
        buttons = [
            element
            for block in view.get("blocks", [])
            if block.get("type") == "actions"
            for element in block.get("elements", [])
            if element.get("type") == "button"
        ]
        assert len(buttons) == 1
        assert buttons[0]["action_id"] == actions.CONFIG_PAUSE_SYNC_CONFIRM


class TestLeaveGroupConfirmModal:
    def _view(self):
        from handlers import group_manage

        client = MagicMock()
        body = {
            "user": {"id": "U1", "team_id": "T1"},
            "team": {"id": "T1"},
            "actions": [{"action_id": f"{actions.CONFIG_LEAVE_GROUP}_5"}],
            "trigger_id": "tr",
        }
        group = SimpleNamespace(id=5, name="G")
        workspace = SimpleNamespace(id=2, team_id="T1", bot_token=None, deleted_at=None)
        with (
            patch("handlers.group_manage.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
            patch("handlers.group_manage.DbManager.find_records", return_value=[group]),
            patch("handlers.group_manage.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.group_manage.helpers.can_workspace_leave", return_value=(True, "")),
        ):
            group_manage.handle_leave_group(body, client, MagicMock(), context={})
        return client.views_open.call_args.kwargs["view"]

    def test_has_a_red_button_and_no_submit_button(self):
        view = self._view()
        assert "submit" not in view
        buttons = _danger_buttons(view)
        assert len(buttons) == 1
        assert buttons[0]["action_id"] == actions.CONFIG_LEAVE_GROUP_CONFIRM


class TestSoleOwnerBlockedModal:
    """The sole-owner explanation only does one thing (dismiss), so it has one button."""

    def _view(self):
        from handlers import group_manage

        client = MagicMock()
        body = {
            "user": {"id": "U1", "team_id": "T1"},
            "team": {"id": "T1"},
            "actions": [{"action_id": f"{actions.CONFIG_LEAVE_GROUP}_5"}],
            "trigger_id": "tr",
        }
        group = SimpleNamespace(id=5, name="G")
        workspace = SimpleNamespace(id=2, team_id="T1", bot_token=None, deleted_at=None)
        with (
            patch("handlers.group_manage.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
            patch("handlers.group_manage.DbManager.find_records", return_value=[group]),
            patch("handlers.group_manage.helpers.get_workspace_record", return_value=workspace),
            patch("handlers.group_manage.helpers.can_workspace_leave", return_value=(False, "sole_owner")),
        ):
            group_manage.handle_leave_group(body, client, MagicMock(), context={})
        return client.views_open.call_args.kwargs["view"]

    def test_has_exactly_one_dismiss_button(self):
        view = self._view()
        assert "submit" not in view
        assert view["close"]["text"] == "Close"
        assert _danger_buttons(view) == []


class TestDisbandGroupConfirmModal:
    def _view(self):
        from handlers import group_manage

        client = MagicMock()
        body = {
            "user": {"id": "U1", "team_id": "T1"},
            "team": {"id": "T1"},
            "actions": [{"action_id": f"{actions.CONFIG_DISBAND_GROUP}_5"}],
            "trigger_id": "tr",
        }
        group = SimpleNamespace(id=5, name="G")
        workspace = SimpleNamespace(id=2, team_id="T1", bot_token=None, deleted_at=None)
        with (
            patch("handlers._common._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.group_manage.DbManager.find_records", return_value=[group]),
            patch("handlers.group_manage.helpers.can_disband", return_value=(True, "")),
            patch("handlers.group_manage.helpers.get_active_members", return_value=[]),
        ):
            group_manage.handle_disband_group(body, client, MagicMock(), context={})
        return client.views_open.call_args.kwargs["view"]

    def test_has_a_red_button_and_no_submit_button(self):
        view = self._view()
        assert "submit" not in view
        buttons = _danger_buttons(view)
        assert len(buttons) == 1
        assert buttons[0]["action_id"] == actions.CONFIG_DISBAND_GROUP_CONFIRM


class TestCloseModalDone:
    def test_updates_the_view_when_a_view_id_is_present(self):
        from handlers._common import _close_modal_done

        client = MagicMock()
        _close_modal_done(client, {"view": {"id": "V1"}}, "done")
        assert client.views_update.called
        assert client.views_update.call_args.kwargs["view_id"] == "V1"

    def test_is_a_no_op_without_a_view_id(self):
        from handlers._common import _close_modal_done

        client = MagicMock()
        _close_modal_done(client, {}, "done")
        assert not client.views_update.called
