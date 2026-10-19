"""Unit tests for helper utilities under ``syncbot/helpers``."""

import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure minimal env vars are set before importing app code
os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
# Placeholder only; never a real token (avoids secret scanners)
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

import helpers

# -----------------------------------------------------------------------
# safe_get
# -----------------------------------------------------------------------


class TestSafeGet:
    def test_simple_dict(self):
        assert helpers.safe_get({"a": 1}, "a") == 1

    def test_nested_dict(self):
        data = {"a": {"b": {"c": 42}}}
        assert helpers.safe_get(data, "a", "b", "c") == 42

    def test_missing_key_returns_none(self):
        assert helpers.safe_get({"a": 1}, "b") is None

    def test_nested_missing_key_returns_none(self):
        assert helpers.safe_get({"a": {"b": 1}}, "a", "c") is None

    def test_none_data_returns_none(self):
        assert helpers.safe_get(None) is None

    def test_empty_dict_returns_none(self):
        assert helpers.safe_get({}, "a") is None

    def test_list_index_access(self):
        data = {"items": [{"name": "first"}, {"name": "second"}]}
        assert helpers.safe_get(data, "items", 0, "name") == "first"
        assert helpers.safe_get(data, "items", 1, "name") == "second"

    def test_list_index_out_of_bounds(self):
        data = {"items": [1]}
        assert helpers.safe_get(data, "items", 5) is None

    def test_deeply_nested(self):
        data = {"a": {"b": {"c": {"d": {"e": "deep"}}}}}
        assert helpers.safe_get(data, "a", "b", "c", "d", "e") == "deep"


# -----------------------------------------------------------------------
# Encryption helpers
# -----------------------------------------------------------------------


class TestEncryption:
    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "my-secret-key-16chars"})
    def test_encrypt_decrypt_roundtrip(self):
        # Use a non-secret placeholder; encryption accepts any string
        token = "xoxb-0-0"
        encrypted = helpers.encrypt_bot_token(token)
        assert encrypted != token
        decrypted = helpers.decrypt_bot_token(encrypted)
        assert decrypted == token

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "my-secret-key-16chars"})
    def test_encrypt_not_stable_ciphertext(self):
        token = "xoxb-0-0"
        a = helpers.encrypt_bot_token(token)
        b = helpers.encrypt_bot_token(token)
        assert a != b
        assert helpers.decrypt_bot_token(a) == token
        assert helpers.decrypt_bot_token(b) == token

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "my-secret-key-16chars"})
    def test_decrypt_invalid_token_raises(self):
        with pytest.raises(ValueError, match="decryption failed"):
            helpers.decrypt_bot_token("not-a-valid-encrypted-token")

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "123"})
    def test_encryption_disabled_with_default_key(self):
        token = "xoxb-0-0"
        assert helpers.encrypt_bot_token(token) == token
        assert helpers.decrypt_bot_token(token) == token

    @patch.dict(os.environ, {}, clear=False)
    def test_encryption_disabled_when_key_missing(self):
        os.environ.pop("DATA_ENCRYPTION_KEY", None)
        os.environ.pop("TOKEN_ENCRYPTION_KEY", None)
        token = "xoxb-0-0"
        assert helpers.encrypt_bot_token(token) == token
        assert helpers.decrypt_bot_token(token) == token

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "key-A-16-characters"})
    def test_wrong_key_raises(self):
        token = "xoxb-0-0"
        encrypted = helpers.encrypt_bot_token(token)

        with (
            patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "key-B-16-characters"}),
            pytest.raises(ValueError, match="decryption failed"),
        ):
            helpers.decrypt_bot_token(encrypted)

    def test_token_encryption_key_legacy_encrypts_and_warns_once(self, caplog):
        import logging

        import constants

        constants._TOKEN_ENCRYPTION_KEY_WARNED = False
        token = "xoxb-0-0"
        with (
            patch.dict(os.environ, {"TOKEN_ENCRYPTION_KEY": "legacy-secret-key16"}, clear=False),
            caplog.at_level(logging.WARNING, logger="syncbot"),
        ):
            os.environ.pop("DATA_ENCRYPTION_KEY", None)
            encrypted = helpers.encrypt_bot_token(token)
            assert encrypted != token
            assert helpers.decrypt_bot_token(encrypted) == token
            helpers.encrypt_bot_token(token)
        warns = [
            r
            for r in caplog.records
            if r.message == "legacy_env_ignored" and getattr(r, "env", None) == "TOKEN_ENCRYPTION_KEY"
        ]
        assert len(warns) == 1
        assert getattr(warns[0], "use", None) == "DATA_ENCRYPTION_KEY"

    def test_token_encryption_key_warns_when_unused_beside_data_key(self, caplog):
        import logging

        import constants

        constants._TOKEN_ENCRYPTION_KEY_WARNED = False
        with (
            patch.dict(
                os.environ,
                {
                    "DATA_ENCRYPTION_KEY": "my-secret-key-16chars",
                    "TOKEN_ENCRYPTION_KEY": "legacy-unused-key16",
                },
                clear=False,
            ),
            caplog.at_level(logging.WARNING, logger="syncbot"),
        ):
            assert helpers.encrypt_bot_token("xoxb-0-0") != "xoxb-0-0"
            helpers.encrypt_bot_token("xoxb-0-0")
        warns = [
            r
            for r in caplog.records
            if r.message == "legacy_env_ignored" and getattr(r, "env", None) == "TOKEN_ENCRYPTION_KEY"
        ]
        assert len(warns) == 1


# -----------------------------------------------------------------------
# In-process cache
# -----------------------------------------------------------------------


class TestCache:
    def setup_method(self):
        helpers._CACHE.clear()

    def test_cache_set_and_get(self):
        helpers._cache_set("k1", "value1")
        assert helpers._cache_get("k1") == "value1"

    def test_cache_miss(self):
        assert helpers._cache_get("nonexistent") is None

    def test_cache_expiry(self):
        helpers._cache_set("k2", "value2", ttl=0)
        time.sleep(0.01)
        assert helpers._cache_get("k2") is None

    def test_cache_within_ttl(self):
        helpers._cache_set("k3", "value3", ttl=60)
        assert helpers._cache_get("k3") == "value3"


# -----------------------------------------------------------------------
# get_request_type
# -----------------------------------------------------------------------


class TestGetRequestType:
    def test_event_callback(self):
        body = {"type": "event_callback", "event": {"type": "message"}}
        assert helpers.get_request_type(body) == ("event_callback", "message")

    def test_view_submission(self):
        body = {"type": "view_submission", "view": {"callback_id": "my_callback"}}
        assert helpers.get_request_type(body) == ("view_submission", "my_callback")

    def test_command(self):
        body = {"command": "/config-syncbot"}
        assert helpers.get_request_type(body) == ("command", "/config-syncbot")

    def test_unknown(self):
        body = {"type": "something_else"}
        assert helpers.get_request_type(body) == ("unknown", "unknown")

    def test_join_sync_suffix_collapses_but_picker_does_not(self):
        from slack import actions

        join = {"type": "block_actions", "actions": [{"action_id": f"{actions.CONFIG_JOIN_SYNC}_55"}]}
        assert helpers.get_request_type(join) == ("block_actions", actions.CONFIG_JOIN_SYNC)
        picker = {"type": "block_actions", "actions": [{"action_id": actions.CONFIG_JOIN_SYNC_SELECT}]}
        assert helpers.get_request_type(picker) == ("block_actions", actions.CONFIG_JOIN_SYNC_SELECT)


class TestBodyIdExtractors:
    def test_block_action_user_and_team(self):
        body = {"user": {"id": "U1", "team_id": "T_USER"}, "team": {"id": "T1"}, "team_id": "T_TOP"}
        assert helpers.get_user_id_from_body(body) == "U1"
        assert helpers.get_team_id_from_body(body) == "T1"

    def test_view_submission_prefers_view_team_id(self):
        body = {
            "user": {"id": "U2", "team_id": "T_USER"},
            "view": {"team_id": "T_VIEW"},
            "team": {"id": "T_TEAM"},
        }
        assert helpers.get_user_id_from_body(body) == "U2"
        assert helpers.get_team_id_from_body(body) == "T_VIEW"

    def test_app_home_opened_event_user_string(self):
        body = {
            "team_id": "T_EVENT",
            "event": {"type": "app_home_opened", "user": "U_HOME"},
        }
        assert helpers.get_user_id_from_body(body) == "U_HOME"
        assert helpers.get_team_id_from_body(body) == "T_EVENT"

    def test_backup_home_button_uses_team_id(self):
        body = {"user": {"id": "U3"}, "team": {"id": "T_BACKUP"}}
        assert helpers.get_user_id_from_body(body) == "U3"
        assert helpers.get_team_id_from_body(body) == "T_BACKUP"

    def test_event_view_team_id(self):
        body = {"event": {"view": {"team_id": "T_EVIEW"}, "user": {"id": "U4"}}}
        assert helpers.get_user_id_from_body(body) == "U4"
        assert helpers.get_team_id_from_body(body) == "T_EVIEW"

    def test_member_joined_channel_event_team(self):
        body = {"event": {"type": "member_joined_channel", "user": "U5", "team": "T_JOIN"}}
        assert helpers.get_user_id_from_body(body) == "U5"
        assert helpers.get_team_id_from_body(body) == "T_JOIN"


# -----------------------------------------------------------------------
# slack_retry decorator
# -----------------------------------------------------------------------


# -----------------------------------------------------------------------
# get_bot_info_from_event
# -----------------------------------------------------------------------


class TestGetBotInfoFromEvent:
    def test_extracts_username_and_icon(self):
        body = {
            "event": {
                "type": "message",
                "subtype": "bot_message",
                "bot_id": "B123",
                "username": "WeatherBot",
                "icons": {"image_48": "https://example.com/icon48.png"},
                "text": "hello",
            }
        }
        name, icon = helpers.get_bot_info_from_event(body)
        assert name == "WeatherBot"
        assert icon == "https://example.com/icon48.png"

    def test_fallback_name_when_no_username(self):
        body = {"event": {"type": "message", "subtype": "bot_message", "bot_id": "B123", "text": "hello"}}
        name, icon = helpers.get_bot_info_from_event(body)
        assert name == "Bot"
        assert icon is None

    def test_icon_fallback_order(self):
        body = {
            "event": {
                "type": "message",
                "subtype": "bot_message",
                "bot_id": "B123",
                "username": "MyBot",
                "icons": {"image_36": "https://example.com/icon36.png", "image_72": "https://example.com/icon72.png"},
                "text": "hello",
            }
        }
        name, icon = helpers.get_bot_info_from_event(body)
        assert icon == "https://example.com/icon36.png"


# -----------------------------------------------------------------------
# slack_retry decorator
# -----------------------------------------------------------------------


class TestSlackRetry:
    def test_success_on_first_try(self):
        @helpers.slack_retry
        def fn():
            return "ok"

        assert fn() == "ok"

    def test_retries_on_429(self):
        from slack_sdk.errors import SlackApiError

        call_count = 0

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "0"}

        @helpers.slack_retry
        def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise SlackApiError("rate_limited", response=mock_response)
            return "ok"

        assert fn() == "ok"
        assert call_count == 3

    def test_non_retryable_error_raises_immediately(self):
        from slack_sdk.errors import SlackApiError

        mock_response = MagicMock()
        mock_response.status_code = 404

        @helpers.slack_retry
        def fn():
            raise SlackApiError("not_found", response=mock_response)

        with pytest.raises(SlackApiError):
            fn()


# -----------------------------------------------------------------------
# resolve_channel_references
# -----------------------------------------------------------------------


class TestResolveChannelReferences:
    """Source #channel ticks and labeled message permalinks."""

    def setup_method(self):
        from helpers._cache import clear_all_caches, clear_request_scope

        clear_all_caches()
        clear_request_scope()

    def _make_workspace(self, team_id="T123", name="Workspace A"):
        ws = MagicMock()
        ws.team_id = team_id
        ws.workspace_name = name
        return ws

    def _make_client(self, channel_name="general"):
        client = MagicMock()
        client.conversations_info.return_value = {"channel": {"name": channel_name}}
        return client

    def test_no_channel_refs_unchanged(self):
        result = helpers.resolve_channel_references("hello world", MagicMock())
        assert result == "hello world"

    def test_empty_text(self):
        result = helpers.resolve_channel_references("", MagicMock())
        assert result == ""

    def test_none_text(self):
        result = helpers.resolve_channel_references(None, MagicMock())
        assert result is None

    def test_code_tick_with_workspace(self):
        client = self._make_client(channel_name="general")
        ws = self._make_workspace(team_id="T123")
        result = helpers.resolve_channel_references("see <#CABC123>", client, ws)
        assert result == "see `#general (Workspace A)`"
        assert "slack://" not in result
        assert "<#C" not in result

    def test_code_tick_without_workspace(self):
        client = self._make_client(channel_name="general")
        result = helpers.resolve_channel_references("see <#CABC123>", client, None)
        assert result == "see `#general`"

    def test_code_tick_does_not_need_team_info(self):
        client = MagicMock()
        client.conversations_info.return_value = {"channel": {"name": "general"}}
        client.team_info.side_effect = Exception("api error")
        ws = self._make_workspace(team_id="T123")
        result = helpers.resolve_channel_references("see <#CABC123>", client, ws)
        assert result == "see `#general (Workspace A)`"

    def test_fallback_when_channel_unresolvable(self):
        client = MagicMock()
        client.conversations_info.side_effect = Exception("channel_not_found")
        ws = self._make_workspace(team_id="T123")
        result = helpers.resolve_channel_references("see <#CABC123>", client, ws)
        assert result == "see #CABC123"

    def test_channel_ref_with_label(self):
        client = self._make_client(channel_name="general")
        ws = self._make_workspace(team_id="T123")
        result = helpers.resolve_channel_references("see <#CABC123|general>", client, ws)
        assert result == "see `#general (Workspace A)`"

    def test_multiple_channel_refs(self):
        client = MagicMock()

        def conv_info(channel):
            names = {"CABC111": "alpha", "CABC222": "beta"}
            return {"channel": {"name": names.get(channel, channel)}}

        client.conversations_info.side_effect = conv_info
        ws = self._make_workspace(team_id="T123")
        result = helpers.resolve_channel_references("see <#CABC111> and <#CABC222>", client, ws)
        assert result == "see `#alpha (Workspace A)` and `#beta (Workspace A)`"
        assert "#alpha" in result
        assert "#beta" in result

    def test_no_deep_links_in_channel_mentions(self):
        client = self._make_client(channel_name="general")
        ws = self._make_workspace(team_id="T123")
        result = helpers.resolve_channel_references("see <#CABC123>", client, ws)
        assert "app_redirect" not in result
        assert "app.slack.com" not in result
        assert "slack://" not in result
        assert result == "see `#general (Workspace A)`"

    def test_source_tick_even_when_target_twin_would_exist(self):
        """Synced twins must not become target <#C>; keep a code-ticked source name."""
        client = self._make_client(channel_name="announcements")
        ws = self._make_workspace(team_id="T123", name="Workspace A")
        result = helpers.resolve_channel_references("see <#CSOURCE123>", client, ws)
        assert result == "see `#announcements (Workspace A)`"
        assert "<#C" not in result
        assert "C_LOCAL" not in result

    def test_channel_archive_url_becomes_tick(self):
        client = self._make_client(channel_name="announcements")
        ws = self._make_workspace(team_id="T123", name="Workspace A")
        text = "see <https://workspace-a.slack.com/archives/CSRC|#general (Workspace B)>"
        result = helpers.resolve_channel_references(text, client, ws)
        assert result == "see `#announcements (Workspace A)`"

    def test_message_permalink_keeps_source_url_with_label(self):
        client = self._make_client(channel_name="announcements")
        ws = self._make_workspace(team_id="T123", name="Workspace A")
        url = "https://workspace-a.slack.com/archives/CSRC123/p1234567890123456"
        result = helpers.resolve_channel_references(f"see {url}", client, ws)
        assert result == f"see <{url}|message in #announcements (Workspace A)>"

    def test_message_permalink_keeps_existing_display_text(self):
        client = self._make_client(channel_name="announcements")
        ws = self._make_workspace(team_id="T123", name="Workspace A")
        url = "https://workspace-a.slack.com/archives/CSRC123/p1234567890123456"
        result = helpers.resolve_channel_references(f"see <{url}|my update>", client, ws)
        assert result == f"see <{url}|my update>"
        client.conversations_info.assert_not_called()

    def test_message_permalink_url_shaped_label_is_replaced(self):
        client = self._make_client(channel_name="announcements")
        ws = self._make_workspace(team_id="T123", name="Workspace A")
        url = "https://workspace-a.slack.com/archives/CSRC123/p1234567890123456"
        result = helpers.resolve_channel_references(
            f"<{url}|This>? Or this: <{url}|{url}>",
            client,
            ws,
        )
        assert result == (f"<{url}|This>? Or this: <{url}|message in #announcements (Workspace A)>")

    def test_message_permalink_uses_subdomain_when_channel_unknown(self):
        client = MagicMock()
        client.conversations_info.side_effect = Exception("channel_not_found")
        result = helpers.resolve_channel_references(
            "https://workspace-a.slack.com/archives/CSRC123/p1234567890123456",
            client,
            None,
        )
        assert result == ("<https://workspace-a.slack.com/archives/CSRC123/p1234567890123456|message in workspace-a>")


# -----------------------------------------------------------------------
# lookup_channel_meta
# -----------------------------------------------------------------------


class TestLookupChannelMeta:
    """Name + private flag for Home and publish, without logging tokens."""

    def setup_method(self):
        helpers._CACHE.clear()
        from helpers._cache import clear_request_scope

        clear_request_scope()

    def test_request_client_is_tried_first(self):
        client = MagicMock()
        client.conversations_info.return_value = {"channel": {"name": "2nd-f", "is_private": False}}

        name, is_private = helpers.lookup_channel_meta("C123", None, client=client)

        assert name == "2nd-f"
        assert is_private is False
        client.conversations_info.assert_called_once_with(channel="C123")

    def test_user_token_is_used_when_bot_cannot_see_the_channel(self):
        bot_client = MagicMock()
        bot_client.conversations_info.side_effect = Exception("channel_not_found")
        user_client = MagicMock()
        user_client.conversations_info.return_value = {"channel": {"name": "leadership", "is_private": True}}

        def web_client(*, token=None, **_kwargs):
            if token == "xoxb-bot":
                return bot_client
            if token == "xoxp-user":
                return user_client
            raise AssertionError(token)

        ws = SimpleNamespace(team_id="T1", instance_id="a" * 64, deleted_at=None)
        with (
            patch("helpers.workspace.get_bot_token", return_value="xoxb-bot"),
            patch("helpers.workspace.WebClient", side_effect=web_client),
        ):
            name, is_private = helpers.lookup_channel_meta("CPRIV", ws, user_token="xoxp-user")

        assert name == "leadership"
        assert is_private is True
        user_client.conversations_info.assert_called_once_with(channel="CPRIV")

    def test_unresolved_name_is_the_channel_id_and_is_not_process_cached(self):
        from helpers._cache import begin_request_scope, clear_request_scope

        client = MagicMock()
        client.conversations_info.side_effect = Exception("channel_not_found")

        # Without a request scope, misses are not memoized (process cache stays success-only).
        name, is_private = helpers.lookup_channel_meta("C_UNRESOLVED", None, client=client)
        helpers.lookup_channel_meta("C_UNRESOLVED", None, client=client)
        assert name == "C_UNRESOLVED"
        assert is_private is False
        assert client.conversations_info.call_count == 2

        # Within one request, misses are memoized so Home/publish do not re-hit Slack.
        begin_request_scope()
        try:
            client.reset_mock()
            client.conversations_info.side_effect = Exception("channel_not_found")
            helpers.lookup_channel_meta("C_UNRESOLVED2", None, client=client)
            helpers.lookup_channel_meta("C_UNRESOLVED2", None, client=client)
            assert client.conversations_info.call_count == 1
            assert "chan_meta:C_UNRESOLVED2" not in helpers._CACHE
        finally:
            clear_request_scope()

    def test_successful_lookup_is_cached(self):
        client = MagicMock()
        client.conversations_info.return_value = {"channel": {"name": "general", "is_private": False}}

        helpers.lookup_channel_meta("C_CACHED_OK", None, client=client)
        helpers.lookup_channel_meta("C_CACHED_OK", None, client=client)

        assert client.conversations_info.call_count == 1

    def test_falls_back_to_stored_sync_channel_name(self):
        client = MagicMock()
        client.conversations_info.side_effect = Exception("channel_not_found")
        ws = SimpleNamespace(id=7, team_id="T1", instance_id="x", deleted_at=None)
        with (
            patch("helpers.workspace.get_bot_token", return_value=None),
            patch("helpers.workspace.stored_channel_name", return_value="announcements"),
        ):
            name, is_private = helpers.lookup_channel_meta("CPEER", ws, client=client)
        assert name == "announcements"
        assert is_private is False


class TestRewriteEnvelopeChannelRefs:
    def test_rewrites_channel_mentions_and_keeps_user_tags(self):
        client = MagicMock()
        client.conversations_info.return_value = {"channel": {"name": "announcements"}}
        ws = SimpleNamespace(workspace_name="Workspace A")
        envelope = {
            "text": "see <#CABC123> and <@U1>",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "<#CABC123> <@U1>"}}],
        }
        helpers.rewrite_envelope_channel_refs(envelope, client, ws)
        assert envelope["text"] == "see `#announcements (Workspace A)` and <@U1>"
        assert envelope["blocks"][0]["text"]["text"] == "`#announcements (Workspace A)` <@U1>"


# -----------------------------------------------------------------------
# Bot identity (per workspace)
# -----------------------------------------------------------------------


class TestOwnAuthInfoIsPerWorkspace:
    """A process-wide auth.test cache invites the wrong workspace's bot."""

    def setup_method(self):
        helpers._CACHE.clear()

    def test_auth_info_is_not_shared_across_bot_tokens(self):
        client_a = MagicMock()
        client_a.token = "xoxb-workspace-a"
        client_a.auth_test.return_value = {"bot_id": "BA", "user_id": "UA"}
        client_b = MagicMock()
        client_b.token = "xoxb-workspace-b"
        client_b.auth_test.return_value = {"bot_id": "BB", "user_id": "UB"}

        assert helpers.get_own_bot_user_id(client_a) == "UA"
        assert helpers.get_own_bot_user_id(client_b) == "UB"
        assert helpers.get_own_bot_id(client_a, {}) == "BA"
        assert helpers.get_own_bot_id(client_b, {}) == "BB"
        assert helpers.get_own_bot_user_id(client_a) == "UA"
        assert client_a.auth_test.call_count == 1
        assert client_b.auth_test.call_count == 1

    def test_context_bot_user_id_wins_over_auth_test(self):
        client = MagicMock()
        client.token = "xoxb-a"
        client.auth_test.return_value = {"bot_id": "BA", "user_id": "UA"}

        assert helpers.get_own_bot_user_id(client, {"bot_user_id": "U_FROM_CONTEXT"}) == "U_FROM_CONTEXT"
        client.auth_test.assert_not_called()

    def test_bypass_cache_calls_auth_test_again(self):
        client = MagicMock()
        client.token = "xoxb-a"
        client.auth_test.return_value = {"bot_id": "BA", "user_id": "UA"}

        helpers.get_own_bot_user_id(client)
        helpers.get_own_bot_user_id(client, bypass_cache=True)

        assert client.auth_test.call_count == 2


class TestApplyMentionedUsersCap:
    def test_every_mention_is_remapped(self):
        ids = [f"U{i:03d}" for i in range(55)]
        text = " ".join(f"<@{uid}>" for uid in ids)
        mentioned = [{"user_id": uid, "user_name": uid} for uid in ids]
        source = MagicMock()
        target = MagicMock()
        with patch(
            "helpers.user_map.resolve_mention_for_workspace",
            side_effect=lambda **kw: f"<@{kw['source_user_id']}_D>",
        ):
            out = helpers.apply_mentioned_users(text, source, target, mentioned, 1, 2)
        assert "<@U000_D>" in out
        assert "<@U049_D>" in out
        assert "<@U054_D>" in out
        assert "<@U050>" not in out


class TestNotifySourceUserError:
    def test_opens_dm_with_format_error_details(self):
        client = MagicMock()
        assert helpers.notify_source_user_error(
            source_client=client,
            source_user_id="U_SRC",
            summary=":warning: SyncBot could not copy your file to the other Channels.",
            details={"event": "file_share_failed", "error": "upload failed"},
        )
        client.chat_postMessage.assert_called_once()
        assert client.chat_postMessage.call_args.kwargs["channel"] == "U_SRC"
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "could not copy your file" in text
        assert "upload failed" in text

    def test_skips_without_client_or_user(self):
        assert helpers.notify_source_user_error(source_client=None, source_user_id="U_SRC", summary="x") is False
        assert helpers.notify_source_user_error(source_client=MagicMock(), source_user_id=None, summary="x") is False
