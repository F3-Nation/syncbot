"""Modal open helpers notify the user when Slack's trigger_id expires."""

import os
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from slack_sdk.errors import SlackApiError  # noqa: E402

from helpers.core import format_error_dm  # noqa: E402
from slack import orm  # noqa: E402


def _slack_error(code: str) -> SlackApiError:
    return SlackApiError(code, {"ok": False, "error": code})


class TestOpenOrPushView:
    def test_expired_trigger_id_dms_user(self):
        client = MagicMock()
        client.views_open.side_effect = _slack_error("expired_trigger_id")
        body = {"user": {"id": "U123"}}

        orm.open_or_push_view(
            client,
            "trig",
            {"type": "modal", "callback_id": "create_sync_submit", "blocks": []},
            body=body,
        )

        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert client.chat_postMessage.call_args.kwargs["channel"] == "U123"
        assert "click the button again" in text
        assert "```" in text
        assert "error: expired_trigger_id" in text
        assert "window: create_sync_submit" in text
        assert "open: open" in text

    def test_other_errors_do_not_dm(self):
        client = MagicMock()
        client.views_open.side_effect = _slack_error("invalid_trigger")
        body = {"user": {"id": "U123"}}

        orm.open_or_push_view(
            client,
            "trig",
            {"type": "modal", "callback_id": "settings_submit", "blocks": []},
            body=body,
        )

        client.chat_postMessage.assert_not_called()

    def test_successful_open_does_not_dm(self):
        client = MagicMock()
        body = {"user": {"id": "U123"}}

        orm.open_or_push_view(
            client,
            "trig",
            {"type": "modal", "callback_id": "create_group_submit", "blocks": []},
            body=body,
        )

        client.views_open.assert_called_once()
        client.chat_postMessage.assert_not_called()


class TestFormatErrorDm:
    def test_summary_only_without_details(self):
        assert format_error_dm("hello") == "hello"

    def test_appends_fenced_details(self):
        text = format_error_dm("hello", {"error": "boom", "event": "x"})
        assert text.startswith("hello\n```\n")
        assert "error: boom" in text
        assert "event: x" in text
        assert text.endswith("```")
