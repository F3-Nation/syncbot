"""Unit tests for handler parsing and dispatch helpers."""

import os
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from handlers import (
    EventContext,
    _is_own_bot_message,
    _parse_event_fields,
    _sanitize_text,
)
from handlers.group import _generate_invite_code

# -----------------------------------------------------------------------
# _parse_event_fields
# -----------------------------------------------------------------------


class TestParseEventFields:
    def _make_client(self):
        client = MagicMock()
        client.users_info.return_value = {
            "user": {
                "id": "U123",
                "profile": {"display_name": "TestUser", "real_name": "Test User"},
            }
        }
        return client

    def test_basic_message(self):
        body = {
            "team_id": "T001",
            "event": {
                "type": "message",
                "channel": "C001",
                "user": "U001",
                "text": "Hello world",
                "ts": "1234567890.000001",
            },
        }
        ctx = _parse_event_fields(body, self._make_client())
        assert ctx["team_id"] == "T001"
        assert ctx["channel_id"] == "C001"
        assert ctx["user_id"] == "U001"
        assert ctx["msg_text"] == "Hello world"
        assert ctx["event_subtype"] is None
        assert ctx["reply_broadcast"] is False

    def test_thread_broadcast_sets_reply_broadcast(self):
        body = {
            "team_id": "T001",
            "event": {
                "type": "message",
                "subtype": "thread_broadcast",
                "channel": "C001",
                "user": "U001",
                "text": "Also send to channel",
                "ts": "1234567890.000002",
                "thread_ts": "1234567890.000001",
            },
        }
        ctx = _parse_event_fields(body, self._make_client())
        assert ctx["event_subtype"] == "thread_broadcast"
        assert ctx["reply_broadcast"] is True
        assert ctx["thread_ts"] == "1234567890.000001"

    def test_empty_text_defaults_to_space(self):
        body = {
            "team_id": "T001",
            "event": {
                "type": "message",
                "channel": "C001",
                "user": "U001",
                "ts": "1234567890.000001",
            },
        }
        ctx = _parse_event_fields(body, self._make_client())
        assert ctx["msg_text"] == " "

    def test_message_changed_subtype(self):
        body = {
            "team_id": "T001",
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C001",
                "message": {
                    "user": "U001",
                    "text": "Edited text",
                    "ts": "1234567890.000001",
                },
            },
        }
        ctx = _parse_event_fields(body, self._make_client())
        assert ctx["event_subtype"] == "message_changed"
        assert ctx["msg_text"] == "Edited text"
        assert ctx["user_id"] == "U001"

    def test_message_deleted_subtype(self):
        body = {
            "team_id": "T001",
            "event": {
                "type": "message",
                "subtype": "message_deleted",
                "channel": "C001",
                "previous_message": {
                    "ts": "1234567890.000001",
                },
            },
        }
        ctx = _parse_event_fields(body, self._make_client())
        assert ctx["event_subtype"] == "message_deleted"
        assert ctx["ts"] == "1234567890.000001"


# -----------------------------------------------------------------------
# EventContext TypedDict
# -----------------------------------------------------------------------


class TestEventContextType:
    def test_event_context_is_dict(self):
        ctx = EventContext(
            team_id="T1",
            channel_id="C1",
            user_id="U1",
            msg_text="hi",
            mentioned_users=[],
            thread_ts=None,
            ts="123.456",
            event_subtype=None,
            reply_broadcast=False,
            content_blocks=[],
        )
        assert isinstance(ctx, dict)
        assert ctx["team_id"] == "T1"


# -----------------------------------------------------------------------
# _sanitize_text
# -----------------------------------------------------------------------


class TestSanitizeText:
    def test_strips_whitespace(self):
        assert _sanitize_text("  hello  ") == "hello"

    def test_truncates_long_text(self):
        result = _sanitize_text("a" * 200, max_length=100)
        assert len(result) == 100

    def test_none_passthrough(self):
        assert _sanitize_text(None) is None

    def test_empty_string_passthrough(self):
        assert _sanitize_text("") == ""

    def test_custom_max_length(self):
        result = _sanitize_text("abcdefgh", max_length=5)
        assert result == "abcde"


# -----------------------------------------------------------------------
# _is_own_bot_message
# -----------------------------------------------------------------------


class TestIsOwnBotMessage:
    def _make_client_with_bot_id(self, bot_id: str = "B_SYNCBOT"):
        client = MagicMock()
        client.auth_test.return_value = {"bot_id": bot_id}
        return client

    def test_own_bot_message_detected(self):
        body = {"event": {"type": "message", "subtype": "bot_message", "bot_id": "B_SYNCBOT", "text": "synced"}}
        client = self._make_client_with_bot_id("B_SYNCBOT")
        context = {"bot_id": "B_SYNCBOT"}
        assert _is_own_bot_message(body, client, context) is True

    def test_other_bot_message_not_flagged(self):
        body = {"event": {"type": "message", "subtype": "bot_message", "bot_id": "B_OTHER", "text": "hello"}}
        client = self._make_client_with_bot_id("B_SYNCBOT")
        context = {"bot_id": "B_SYNCBOT"}
        assert _is_own_bot_message(body, client, context) is False

    def test_regular_user_message_not_flagged(self):
        body = {"event": {"type": "message", "user": "U001", "text": "hello"}}
        client = self._make_client_with_bot_id("B_SYNCBOT")
        context = {"bot_id": "B_SYNCBOT"}
        assert _is_own_bot_message(body, client, context) is False

    def test_own_bot_in_message_changed(self):
        body = {
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C001",
                "message": {"bot_id": "B_SYNCBOT", "subtype": "bot_message", "text": "edited"},
            },
        }
        client = self._make_client_with_bot_id("B_SYNCBOT")
        context = {"bot_id": "B_SYNCBOT"}
        assert _is_own_bot_message(body, client, context) is True

    def test_other_bot_in_message_changed(self):
        body = {
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C001",
                "message": {"bot_id": "B_OTHER", "subtype": "bot_message", "text": "edited"},
            },
        }
        client = self._make_client_with_bot_id("B_SYNCBOT")
        context = {"bot_id": "B_SYNCBOT"}
        assert _is_own_bot_message(body, client, context) is False

    def test_fallback_to_auth_test_when_context_empty(self):
        body = {"event": {"type": "message", "subtype": "bot_message", "bot_id": "B_SYNCBOT", "text": "hi"}}
        client = self._make_client_with_bot_id("B_SYNCBOT")
        context = {}
        assert _is_own_bot_message(body, client, context) is True


class TestParseEventFieldsBotMessage:
    def _make_client(self):
        client = MagicMock()
        client.users_info.return_value = {
            "user": {"id": "U123", "profile": {"display_name": "TestUser", "real_name": "Test User"}}
        }
        return client

    def test_bot_message_has_no_user_id(self):
        body = {
            "team_id": "T001",
            "event": {
                "type": "message",
                "subtype": "bot_message",
                "bot_id": "B_OTHER",
                "username": "WeatherBot",
                "text": "Today's forecast",
                "ts": "1234567890.000001",
                "channel": "C001",
            },
        }
        ctx = _parse_event_fields(body, self._make_client())
        assert ctx["user_id"] is None
        assert ctx["event_subtype"] == "bot_message"
        assert ctx["msg_text"] == "Today's forecast"


# -----------------------------------------------------------------------
# _generate_invite_code
# -----------------------------------------------------------------------


class TestGenerateInviteCode:
    def test_code_format(self):
        code = _generate_invite_code()
        assert len(code) == 8  # 3 + dash + 4
        assert code[3] == "-"
        assert code[:3].isalnum()
        assert code[4:].isalnum()

    def test_code_is_uppercase(self):
        code = _generate_invite_code()
        assert code == code.upper()

    def test_codes_are_unique(self):
        codes = {_generate_invite_code() for _ in range(50)}
        assert len(codes) > 45

    def test_custom_length(self):
        code = _generate_invite_code(length=8)
        assert len(code) == 9  # 3 + dash + 5
        assert code[3] == "-"


# -----------------------------------------------------------------------
# Invite code normalisation (same logic as group invite code)
# -----------------------------------------------------------------------


class TestInviteCodeValidation:
    def test_code_normalisation_adds_dash(self):
        raw = "a7xk9m"
        normalized = raw.strip().upper()
        if "-" not in normalized and len(normalized) >= 6:
            normalized = f"{normalized[:3]}-{normalized[3:]}"
        assert normalized == "A7X-K9M"

    def test_code_already_formatted(self):
        raw = "A7X-K9M"
        normalized = raw.strip().upper()
        if "-" not in normalized and len(normalized) >= 6:
            normalized = f"{normalized[:3]}-{normalized[3:]}"
        assert normalized == "A7X-K9M"

    def test_code_with_whitespace(self):
        raw = "  a7x-k9m  "
        normalized = raw.strip().upper()
        if "-" not in normalized and len(normalized) >= 6:
            normalized = f"{normalized[:3]}-{normalized[3:]}"
        assert normalized == "A7X-K9M"


# -----------------------------------------------------------------------
# get_request_type — group prefix matching
# -----------------------------------------------------------------------


class TestRequestTypeGroupPrefix:
    def test_leave_group_prefix_resolved(self):
        from helpers import get_request_type
        from slack import actions

        body = {
            "type": "block_actions",
            "actions": [{"action_id": f"{actions.CONFIG_LEAVE_GROUP}_42"}],
        }
        req_type, req_id = get_request_type(body)
        assert req_type == "block_actions"
        assert req_id == actions.CONFIG_LEAVE_GROUP

    def test_join_sync_picker_does_not_collapse_onto_join_sync(self):
        from helpers import get_request_type
        from slack import actions

        body = {
            "type": "block_actions",
            "actions": [{"action_id": actions.CONFIG_JOIN_SYNC_SELECT}],
        }
        req_type, req_id = get_request_type(body)
        assert req_type == "block_actions"
        assert req_id == actions.CONFIG_JOIN_SYNC_SELECT
