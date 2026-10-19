"""Loading modals open in the ack. A failed open does not DM."""

import os
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from helpers.core import format_error_dm  # noqa: E402
from slack import actions, orm  # noqa: E402


def _slack_error(code: str) -> SlackApiError:
    return SlackApiError(code, {"ok": False, "error": code})


class TestOpenOrPushView:
    @pytest.mark.parametrize("code", ["expired_trigger_id", "invalid_trigger", "duplicate_external_id"])
    def test_open_errors_do_not_dm(self, code):
        client = MagicMock()
        client.views_open.side_effect = _slack_error(code)

        orm.open_or_push_view(
            client,
            "trig",
            {"type": "modal", "callback_id": actions.LOADING_MODAL_CALLBACK, "blocks": []},
        )

        client.chat_postMessage.assert_not_called()

    def test_successful_open_does_not_dm(self):
        client = MagicMock()

        orm.open_or_push_view(
            client,
            "trig",
            {"type": "modal", "callback_id": "create_group_submit", "blocks": []},
        )

        client.views_open.assert_called_once()
        client.chat_postMessage.assert_not_called()


class TestUpdateOpenedView:
    def test_not_found_does_not_dm(self):
        client = MagicMock()
        client.views_update.side_effect = _slack_error("not_found")

        orm.update_opened_view(
            client,
            {"team": {"id": "T1"}, "user": {"id": "U1"}},
            "trig",
            {"type": "modal", "blocks": []},
        )

        client.chat_postMessage.assert_not_called()
        assert orm.modal_was_updated()


class TestLoadingModal:
    def test_ack_opens_loading_view(self):
        from app import open_loading_modal

        client = MagicMock()
        body = {
            "type": "block_actions",
            "trigger_id": "trig.1",
            "team": {"id": "T1"},
            "user": {"id": "U1"},
            "actions": [{"action_id": actions.CONFIG_OPEN_SETTINGS}],
        }
        with patch("db.DbManager.get_record") as get_record:
            open_loading_modal(body, client)

        get_record.assert_not_called()
        client.users_info.assert_not_called()
        client.views_push.assert_not_called()
        view = client.views_open.call_args.kwargs["view"]
        assert view["title"]["text"] == "Loading..."
        assert view["blocks"][0]["text"]["text"] == "If this doesn't load, please close and try again."
        assert view["close"]["text"] == "Close"
        assert "submit" not in view
        assert view["external_id"] == orm.build_modal_external_id("T1", "trig.1")

    def test_user_mapping_edit_pushes_once(self):
        from app import open_loading_modal

        client = MagicMock()
        body = {
            "type": "block_actions",
            "trigger_id": "trig",
            "team": {"id": "T1"},
            "actions": [{"action_id": "user_mapping_edit_9"}],
        }
        open_loading_modal(body, client)

        client.views_push.assert_called_once()
        client.views_open.assert_not_called()
        pushed = client.views_push.call_args.kwargs["view"]
        assert pushed["title"]["text"] == "Loading..."
        assert pushed["blocks"][0]["text"]["text"] == "If this doesn't load, please close and try again."

    def test_action_ack_opens_then_acks(self):
        from app import action_ack

        client = MagicMock()
        ack = MagicMock()
        body = {
            "type": "block_actions",
            "trigger_id": "trig",
            "team": {"id": "T1"},
            "actions": [{"action_id": actions.CONFIG_OPEN_SETTINGS}],
        }

        def _open(**kwargs):
            ack.assert_not_called()
            return {"ok": True}

        client.views_open.side_effect = _open
        action_ack(body, client, ack)
        ack.assert_called_once_with()

    def test_settings_work_updates_the_same_external_id(self):
        from handlers.settings import handle_open_settings

        client = MagicMock()
        body = {"team": {"id": "T1"}, "user": {"id": "U1"}, "trigger_id": "trig.1"}
        with (
            patch("handlers.settings.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.settings.helpers.get_team_id_from_body", return_value="T1"),
            patch("handlers.settings.helpers.is_workspace_admin", return_value=True),
            patch("handlers.settings.helpers.is_settings_visible_for_workspace", return_value=True),
            patch("handlers.settings._build_settings_form") as build_form,
        ):
            build_form.return_value = orm.BlockView(blocks=[orm.SectionBlock(label="Settings")])
            handle_open_settings(body, client, MagicMock(), {})

        client.views_open.assert_not_called()
        client.views_push.assert_not_called()
        assert client.views_update.call_args.kwargs["external_id"] == orm.build_modal_external_id("T1", "trig.1")
        view = client.views_update.call_args.kwargs["view"]
        assert view["callback_id"] == actions.CONFIG_SETTINGS_SUBMIT
        assert view["external_id"] == orm.build_modal_external_id("T1", "trig.1")

    def test_handler_without_an_update_shows_denial(self):
        import app as app_module

        client = MagicMock()
        body = {
            "type": "block_actions",
            "trigger_id": "trig",
            "team": {"id": "T1"},
            "user": {"id": "U1"},
            "actions": [{"action_id": "open_settings"}],
        }

        def handler(b, c, log, ctx):
            return None

        with (
            patch.object(app_module, "LOCAL_DEVELOPMENT", True),
            patch.object(app_module, "MAIN_MAPPER", {"block_actions": {"open_settings": handler}}),
            patch.object(app_module, "emit_metric"),
        ):
            app_module.main_response(body, MagicMock(), client, MagicMock(), {})

        view = client.views_update.call_args.kwargs["view"]
        assert view["callback_id"] == actions.MODAL_DENIED_CALLBACK
        assert view["blocks"][0]["text"]["text"] == ":lock: You can't open that."
        client.chat_postMessage.assert_not_called()


class TestModalActionSets:
    def test_push_actions_are_openers_in_the_mapper(self):
        from routing import ACTION_MAPPER, MODAL_OPEN_ACTIONS, MODAL_PUSH_ACTIONS, VIEW_ACK_MAPPER, VIEW_MAPPER

        assert MODAL_PUSH_ACTIONS <= MODAL_OPEN_ACTIONS
        assert set(ACTION_MAPPER) >= MODAL_OPEN_ACTIONS
        assert {actions.CONFIG_USER_MAPPING_EDIT} == MODAL_PUSH_ACTIONS
        assert actions.LOADING_MODAL_CALLBACK not in VIEW_MAPPER
        assert actions.LOADING_MODAL_CALLBACK not in VIEW_ACK_MAPPER
        assert actions.MODAL_DENIED_CALLBACK not in VIEW_MAPPER
        assert actions.MODAL_DENIED_CALLBACK not in VIEW_ACK_MAPPER


class TestFormatErrorDm:
    def test_summary_only_without_details(self):
        assert format_error_dm("hello") == "hello"

    def test_appends_fenced_details(self):
        text = format_error_dm("hello", {"error": "boom", "event": "x"})
        assert text.startswith("hello\n```\n")
        assert "error: boom" in text
        assert "event: x" in text
        assert text.endswith("```")
