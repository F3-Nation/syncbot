"""Tests for editing channel participation without changing legacy sync mode."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

import constants  # noqa: E402
from handlers.channel_sync import (  # noqa: E402
    _parse_edit_sync_ref,
    handle_edit_sync,
    handle_edit_sync_submit,
    handle_edit_sync_submit_ack,
)
from slack import actions  # noqa: E402


def _action_body(value: str, action_id: str) -> dict:
    return {
        "trigger_id": "trig",
        "actions": [{"value": value, "action_id": action_id}],
        "user": {"id": "U1"},
        "team": {"id": "T1"},
    }


def _submit_body(participation_action: str, participation: str, *, publish_flow: bool) -> dict:
    return {
        "user": {"id": "U1"},
        "view": {
            "private_metadata": (
                f'{{"sync_id": 42, "sync_channel_id": 10, "publish_flow": {str(publish_flow).lower()}' + "}"
            ),
            "state": {
                "values": {
                    "participation": {
                        participation_action: {
                            "type": "radio_buttons",
                            "selected_option": {"value": participation},
                        }
                    }
                }
            },
        },
    }


class TestParseEditSyncRef:
    def test_channel_and_sync_encodings(self):
        assert _parse_edit_sync_ref(_action_body("c:12", f"{actions.CONFIG_EDIT_SYNC}_c_12")) == (
            "channel",
            12,
        )
        assert _parse_edit_sync_ref(_action_body("s:42", f"{actions.CONFIG_EDIT_SYNC}_s_42")) == (
            "sync",
            42,
        )


class TestHandleEditSync:
    WORKSPACE = SimpleNamespace(id=1, team_id="T1")
    SYNC = SimpleNamespace(
        id=42,
        group_id=5,
        sync_mode="direct",
        target_workspace_id=2,
        publisher_workspace_id=1,
    )

    def test_publisher_edits_participation_without_mode_picker(self):
        channel = SimpleNamespace(
            id=10,
            sync_id=42,
            workspace_id=1,
            channel_id="C_LOCAL",
            deleted_at=None,
            publishes=True,
            subscribes=False,
            reaction_direction=constants.REACTION_DIRECTION_SEND,
            reaction_style=None,
        )
        captured = {}

        def capture(self, **kwargs):
            captured["blocks"] = list(self.blocks)
            captured["kwargs"] = kwargs

        def get_record(model, *args, **kwargs):
            from db import schemas

            if model is schemas.WorkspaceGroup:
                return SimpleNamespace(id=5, name="HQ")
            return self.SYNC

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", self.WORKSPACE)),
            patch("handlers.channel_sync._sync_channel_by_pk", return_value=channel),
            patch("handlers.channel_sync.DbManager.get_record", side_effect=get_record),
            patch("handlers.channel_sync.orm.BlockView.post_modal", capture),
        ):
            handle_edit_sync(
                _action_body("c:10", f"{actions.CONFIG_EDIT_SYNC}_c_10"),
                MagicMock(),
                MagicMock(),
                {},
            )

        action_ids = [getattr(block, "action", None) for block in captured["blocks"]]
        texts = [getattr(getattr(block, "element", None), "initial_value", None) for block in captured["blocks"]]
        assert actions.CONFIG_SYNC_PARTICIPATION in action_ids
        assert actions.CONFIG_SYNC_REACTION_STYLE in action_ids
        assert any(text and "<#C_LOCAL>" in text and "HQ" in text for text in texts)
        assert captured["kwargs"]["title_text"] == "Edit Sync"
        assert self.SYNC.sync_mode == "direct"

    def test_subscriber_edits_participation_and_reaction_type(self):
        workspace = SimpleNamespace(id=2, team_id="T2")
        channel = SimpleNamespace(
            id=10,
            sync_id=42,
            workspace_id=2,
            channel_id="C_SUB",
            deleted_at=None,
            publishes=False,
            subscribes=True,
            reaction_direction=constants.REACTION_DIRECTION_RECEIVE,
            reaction_style=constants.REACTION_STYLE_DIRECT_ONLY,
        )
        captured = {}

        def capture(self, **kwargs):
            captured["blocks"] = list(self.blocks)

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U2", workspace)),
            patch("handlers.channel_sync._sync_channel_by_pk", return_value=channel),
            patch("handlers.channel_sync.DbManager.get_record", return_value=self.SYNC),
            patch("handlers.channel_sync.orm.BlockView.post_modal", capture),
        ):
            handle_edit_sync(
                _action_body("c:10", f"{actions.CONFIG_EDIT_SYNC}_c_10"),
                MagicMock(),
                MagicMock(),
                {},
            )

        action_ids = [getattr(block, "action", None) for block in captured["blocks"]]
        assert actions.CONFIG_SYNC_PARTICIPATION in action_ids
        assert actions.CONFIG_SYNC_REACTION_STYLE in action_ids
        participation = next(
            block for block in captured["blocks"] if getattr(block, "action", None) == actions.CONFIG_SYNC_PARTICIPATION
        )
        assert [opt.value for opt in participation.element.options] == [
            "publish_only",
            "subscribe_only",
            "publish_and_subscribe",
        ]
        style = next(
            block
            for block in captured["blocks"]
            if getattr(block, "action", None) == actions.CONFIG_SYNC_REACTION_STYLE
        )
        assert [opt.value for opt in style.element.options] == [
            constants.REACTION_STYLE_THREADED_AND_DIRECT,
            constants.REACTION_STYLE_DIRECT_ONLY,
            constants.REACTION_STYLE_OFF,
        ]

    def test_sync_only_edit_reference_no_longer_opens_policy_picker(self):
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", self.WORKSPACE)),
            patch("handlers.channel_sync.orm.BlockView.post_modal") as post_modal,
        ):
            handle_edit_sync(
                _action_body("s:42", f"{actions.CONFIG_EDIT_SYNC}_s_42"),
                MagicMock(),
                MagicMock(),
                {},
            )
        post_modal.assert_not_called()

    def test_ack_has_no_mode_policy_validation(self):
        with patch(
            "handlers.channel_sync._get_authorized_workspace",
            return_value=("U1", self.WORKSPACE),
        ):
            assert handle_edit_sync_submit_ack({}, MagicMock(), {}) is None

    def test_submit_updates_participation_and_keeps_direct_sync_mode(self):
        channel = SimpleNamespace(
            id=10,
            sync_id=42,
            workspace_id=1,
            deleted_at=None,
            publishes=True,
            subscribes=False,
            reaction_direction=constants.REACTION_DIRECTION_SEND,
            reaction_style=None,
        )
        body = _submit_body(
            actions.CONFIG_SYNC_PARTICIPATION,
            "publish_and_subscribe",
            publish_flow=True,
        )
        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", self.WORKSPACE)),
            patch("handlers.channel_sync.DbManager.get_record", return_value=self.SYNC),
            patch("handlers.channel_sync.DbManager.update_records") as update_sync,
            patch("handlers.channel_sync._sync_channel_by_pk", return_value=channel),
            patch("helpers.reaction.update_sync_channel_reactions") as update_channel,
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
            patch("handlers.channel_sync._refresh_group_member_homes"),
        ):
            handle_edit_sync_submit(body, MagicMock(), MagicMock(), {})

        update_sync.assert_not_called()
        update_channel.assert_called_once_with(
            10,
            style=constants.DEFAULT_REACTION_STYLE_NEW_RECEIVE,
            publishes=True,
            subscribes=True,
        )
        assert self.SYNC.sync_mode == "direct"
