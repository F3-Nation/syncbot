"""Tests for getting SyncBot into the channel it is about to sync.

Slack rejects ``conversations.join`` on a private channel outright, so the bot
has to be invited by a member using that member's own user token. These tests
pin the two paths apart, and pin the ordering that makes the private path work
at all: the ``SyncChannel`` row is written *before* the bot is added, so the
unconfigured-channel handlers do not show it the door.
"""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from handlers.channel_sync import handle_create_sync_submit_work, handle_join_sync_submit
from helpers.conversations import ConversationAccessError, authorize_url, ensure_bot_in_conversation


def _slack_error(code: str, *, errors=None) -> SlackApiError:
    payload = {"ok": False, "error": code}
    if errors is not None:
        payload["errors"] = errors
    return SlackApiError(code, payload)


@pytest.fixture
def client():
    return MagicMock()


class TestEnsureBotInConversation:
    def test_public_channel_is_joined_with_the_bot_token(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": False, "is_member": False}}

        ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        client.conversations_join.assert_called_once_with(channel="C1")
        client.conversations_invite.assert_not_called()

    def test_existing_membership_needs_no_api_call(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": True}}

        ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        client.conversations_join.assert_not_called()
        client.conversations_invite.assert_not_called()

    def test_public_join_that_says_already_in_channel_is_success(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": False, "is_member": False}}
        client.conversations_join.side_effect = _slack_error("already_in_channel")

        ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

    def test_public_join_failure_is_reported(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": False, "is_member": False}}
        client.conversations_join.side_effect = _slack_error("is_archived")

        with pytest.raises(ConversationAccessError) as exc:
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        assert "is_archived" in str(exc.value)

    def test_private_channel_is_invited_as_the_acting_user(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client) as web_client,
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        assert web_client.call_args.kwargs["token"] == "xoxp-acting-user"
        user_client.conversations_invite.assert_called_once_with(channel="C1", users="UBOT")
        client.conversations_join.assert_not_called()

    def test_channel_the_bot_cannot_see_takes_the_invite_path(self, client):
        """A private channel SyncBot has never been in is invisible to the bot token."""
        client.conversations_info.side_effect = _slack_error("channel_not_found")
        user_client = MagicMock()

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        user_client.conversations_invite.assert_called_once()
        client.conversations_join.assert_not_called()

    def test_invite_that_says_already_in_channel_is_success(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()
        user_client.conversations_invite.side_effect = _slack_error("already_in_channel")

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

    def test_private_channel_without_the_acting_user_token_points_at_authorize(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.conversations.get_user_token", return_value=None),
            patch("helpers.conversations.WebClient") as web_client,
            pytest.raises(ConversationAccessError) as exc,
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        assert "Authorize SyncBot" in str(exc.value)
        web_client.assert_not_called()

    def test_only_the_acting_user_token_is_ever_looked_up(self, client):
        """Another member's token must not be borrowed to reach a private channel.

        The picker only offers a private channel to someone who belongs to it, so
        the acting user's membership is what makes the invite legitimate. Using a
        colleague's token would reach channels the publisher cannot see.
        """
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        tokens = {"U_AUTHORIZED": "xoxp-somebody-else"}
        asked_for: list = []

        def get_user_token(_team_id, user_id):
            asked_for.append(user_id)
            return tokens.get(user_id)

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.conversations.get_user_token", side_effect=get_user_token),
            patch("helpers.conversations.WebClient") as web_client,
            pytest.raises(ConversationAccessError) as exc,
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U_NO_TOKEN")

        assert asked_for == ["U_NO_TOKEN"]
        assert "Authorize SyncBot" in str(exc.value)
        web_client.assert_not_called()

    def test_invite_uses_the_request_scoped_bot_member_id(self, client):
        """Bolt's context is this workspace. A cached auth.test from another is not."""
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="U_FROM_OTHER_WORKSPACE"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
        ):
            ensure_bot_in_conversation(
                client,
                "C1",
                team_id="T1",
                acting_user_id="U1",
                context={"bot_user_id": "U_THIS_WORKSPACE"},
            )

        user_client.conversations_invite.assert_called_once_with(channel="C1", users="U_THIS_WORKSPACE")

    def test_invite_skips_a_bot_id_and_uses_the_member_id(self, client):
        """Passing a ``B…`` bot_id to conversations.invite is ``user_not_found``."""
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOTUSER"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
        ):
            ensure_bot_in_conversation(
                client,
                "C1",
                team_id="T1",
                acting_user_id="U1",
                bot_user_id="B0BOT",
            )

        user_client.conversations_invite.assert_called_once_with(channel="C1", users="UBOTUSER")

    def test_invite_retries_with_a_fresh_identity_on_user_not_found(self, client):
        """A stale cached member ID from another workspace is retried once."""
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()
        user_client.conversations_invite.side_effect = [_slack_error("user_not_found"), {"ok": True}]

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="U_THIS_WORKSPACE"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
        ):
            ensure_bot_in_conversation(
                client,
                "C1",
                team_id="T1",
                acting_user_id="U1",
                bot_user_id="U_OTHER_WORKSPACE",
            )

        invited = [call.kwargs["users"] for call in user_client.conversations_invite.call_args_list]
        assert invited == ["U_OTHER_WORKSPACE", "U_THIS_WORKSPACE"]

    def test_nested_cant_invite_user_not_found_is_retried(self, client):
        """Slack sometimes wraps the unknown invitee as cant_invite + errors[]."""
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()
        user_client.conversations_invite.side_effect = [
            _slack_error(
                "cant_invite",
                errors=[{"ok": False, "error": "user_not_found", "user": "U_OTHER_WORKSPACE"}],
            ),
            {"ok": True},
        ]

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="U_THIS_WORKSPACE"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
        ):
            ensure_bot_in_conversation(
                client,
                "C1",
                team_id="T1",
                acting_user_id="U1",
                bot_user_id="U_OTHER_WORKSPACE",
            )

        invited = [call.kwargs["users"] for call in user_client.conversations_invite.call_args_list]
        assert invited == ["U_OTHER_WORKSPACE", "U_THIS_WORKSPACE"]

    def test_user_not_found_with_the_same_id_explains_the_failure(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()
        user_client.conversations_invite.side_effect = _slack_error("user_not_found")

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOTUSER"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
            pytest.raises(ConversationAccessError) as exc,
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        assert "recognise this app" in str(exc.value)
        assert user_client.conversations_invite.call_count == 1

    def test_installation_lookup_is_scoped_to_the_acting_user(self):
        """``find_installation`` is always called with a ``user_id``.

        Without one it returns the team's most recent install row, which is
        somebody else's token.
        """
        from helpers.conversations import get_user_token

        store = MagicMock()
        store.find_installation.return_value = SimpleNamespace(user_token="xoxp-acting-user")

        with patch("helpers.conversations._installation_store", return_value=store):
            assert get_user_token("T1", "U1") == "xoxp-acting-user"

        assert store.find_installation.call_args.kwargs["user_id"] == "U1"

    def test_acting_user_outside_the_channel_gets_an_explicit_message(self, client):
        """Only reachable via a stale or hand-built payload, since the picker filters."""
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}
        user_client = MagicMock()
        user_client.conversations_invite.side_effect = _slack_error("not_in_channel")

        with (
            patch("helpers.conversations.allow_private_channels", return_value=True),
            patch("helpers.conversations.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.conversations.get_user_token", return_value="xoxp-acting-user"),
            patch("helpers.conversations.WebClient", return_value=user_client),
            pytest.raises(ConversationAccessError) as exc,
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        assert "you are not a member of it" in str(exc.value)

    def test_private_channel_is_refused_when_the_policy_is_off(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}

        with (
            patch("helpers.conversations.allow_private_channels", return_value=False),
            pytest.raises(ConversationAccessError) as exc,
        ):
            ensure_bot_in_conversation(client, "C1", team_id="T1", acting_user_id="U1")

        assert "Private Channels cannot be synced" in str(exc.value)


class TestAuthorizeUrl:
    """Bolt only accepts OAuth that started at this instance's /slack/install.

    The origin is the Host of incoming Slack requests, not SYNCBOT_PUBLIC_URL.
    """

    def test_install_path_carries_the_team(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")

        url = authorize_url("T1", context={"public_base_url": "https://syncbot.example.com"})

        assert url == "https://syncbot.example.com/slack/install?team=T1"

    def test_install_path_without_a_team_is_unchanged(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")

        url = authorize_url(context={"public_base_url": "https://syncbot.example.com"})

        assert url == "https://syncbot.example.com/slack/install"

    def test_remembered_host_is_used_without_context(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
        monkeypatch.setenv("SYNCBOT_PUBLIC_URL", "")
        from helpers.oauth import remember_public_base

        remember_public_base("https://fn.lambda-url.us-east-1.on.aws")

        assert authorize_url("T1") == "https://fn.lambda-url.us-east-1.on.aws/slack/install?team=T1"

    def test_legacy_env_public_url_is_ignored(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
        monkeypatch.setenv("SYNCBOT_PUBLIC_URL", "https://syncbot.example.com")

        url = authorize_url("T1", context={"public_base_url": "https://from-host.example"})

        assert url == "https://from-host.example/slack/install?team=T1"

    def test_no_origin_means_no_link(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
        monkeypatch.setenv("SYNCBOT_PUBLIC_URL", "https://should-not-use.example")
        monkeypatch.setattr("helpers.oauth.get_public_base_url", lambda context=None: None)

        assert authorize_url("T1") is None

    def test_no_client_id_means_no_link(self, monkeypatch):
        monkeypatch.setenv("SLACK_CLIENT_ID", "")

        assert authorize_url("T1", context={"public_base_url": "https://syncbot.example.com"}) is None


class TestCreateSyncWritesRowsBeforeAddingTheBot:
    """Ordering is the whole fix: rows first, membership second."""

    WORKSPACE = SimpleNamespace(id=10, team_id="T1")
    BODY = {"view": {"team_id": "T1"}, "user": {"id": "U1"}}

    def _enter_create_sync_patches(self, stack: ExitStack, created: list) -> None:
        def create_record(record):
            record.id = 99 + len(created)
            created.append(record)

        for patcher in (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", self.WORKSPACE)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"group_id": 7}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="C1"),
            patch("handlers.channel_sync._get_selected_option_value", return_value=None),
            patch("handlers.channel_sync._validate_channel_selection", return_value=None),
            patch("handlers.channel_sync.helpers.get_user_token", return_value=None),
            patch("handlers.channel_sync.helpers.lookup_channel_meta", return_value=("C1", False)),
            patch("handlers.channel_sync.DbManager.create_record", side_effect=create_record),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[SimpleNamespace(name="Group")]),
            patch("handlers.channel_sync.helpers.format_admin_label", return_value=("Admin", "Admin (WS)")),
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
            patch("handlers.channel_sync._refresh_group_member_homes"),
        ):
            stack.enter_context(patcher)

    def test_sync_channel_exists_before_the_bot_is_added(self):
        created: list = []
        order: list[str] = []
        client = MagicMock()

        def ensure(*_args, **_kwargs):
            order.append(f"membership after {len(created)} rows")

        with ExitStack() as stack:
            self._enter_create_sync_patches(stack, created)
            stack.enter_context(patch("handlers._common.helpers.ensure_bot_in_conversation", side_effect=ensure))
            handle_create_sync_submit_work(self.BODY, client, MagicMock(), {})

        assert [type(record).__name__ for record in created] == ["Sync", "SyncChannel"]
        assert order == ["membership after 2 rows"]

    def test_failed_membership_rolls_back_and_dms_the_admin(self):
        created: list = []
        client = MagicMock()

        with ExitStack() as stack:
            self._enter_create_sync_patches(stack, created)
            stack.enter_context(
                patch(
                    "handlers._common.helpers.ensure_bot_in_conversation",
                    side_effect=ConversationAccessError("no dice"),
                )
            )
            purge_sync = stack.enter_context(patch("handlers.channel_sync.helpers.purge_sync"))
            handle_create_sync_submit_work(self.BODY, client, MagicMock(), {})

        purge_sync.assert_called_once_with(created[0].id)
        assert client.chat_postMessage.call_args.kwargs["channel"] == "U1"
        assert "no dice" in client.chat_postMessage.call_args.kwargs["text"]

    def test_create_sync_stores_the_real_channel_name_not_the_id(self):
        created: list = []
        client = MagicMock()

        with ExitStack() as stack:
            self._enter_create_sync_patches(stack, created)
            stack.enter_context(
                patch("handlers.channel_sync.helpers.lookup_channel_meta", return_value=("2nd-f", True))
            )
            stack.enter_context(patch("handlers._common.helpers.ensure_bot_in_conversation"))
            handle_create_sync_submit_work(self.BODY, client, MagicMock(), {})

        assert created[0].title == "2nd-f"

    def test_create_sync_refreshes_title_after_the_bot_joins(self):
        created: list = []
        client = MagicMock()
        names = iter([("C1", False), ("2nd-f", False)])

        with ExitStack() as stack:
            self._enter_create_sync_patches(stack, created)
            stack.enter_context(
                patch(
                    "handlers.channel_sync.helpers.lookup_channel_meta",
                    side_effect=lambda *_a, **_k: next(names),
                )
            )
            update = stack.enter_context(patch("handlers.channel_sync.DbManager.update_records"))
            stack.enter_context(patch("handlers._common.helpers.ensure_bot_in_conversation"))
            handle_create_sync_submit_work(self.BODY, client, MagicMock(), {})

        assert created[0].title == "2nd-f"
        update.assert_called_once()


class TestJoinSyncWritesRowsBeforeAddingTheBot:
    """Join Sync has the same ordering requirement as Create Sync."""

    WORKSPACE = SimpleNamespace(id=10, team_id="T1")
    BODY = {"view": {"team_id": "T1"}, "user": {"id": "U1"}}
    CONTEXT = {"bot_user_id": "U_THIS_WORKSPACE"}

    def _enter_join_sync_patches(self, stack: ExitStack, created: list) -> None:
        def create_record(record):
            record.id = 50 + len(created)
            created.append(record)

        for patcher in (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", self.WORKSPACE)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": 7}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="C1"),
            patch("handlers.channel_sync._validate_channel_selection", return_value=None),
            patch(
                "handlers.channel_sync.DbManager.get_record",
                return_value=SimpleNamespace(id=7, group_id=3, title="2nd-f"),
            ),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.DbManager.create_record", side_effect=create_record),
            patch("handlers.channel_sync.helpers.format_admin_label", return_value=("Admin", "Admin (WS)")),
            patch("handlers.channel_sync.helpers.resolve_channel_name", return_value="#c"),
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
            patch("handlers.channel_sync._refresh_group_member_homes"),
        ):
            stack.enter_context(patcher)

    def test_sync_channel_exists_before_the_bot_is_added_and_context_is_passed(self):
        created: list = []
        order: list[str] = []
        seen_context: list = []
        client = MagicMock()

        def ensure(*_args, **kwargs):
            order.append(f"membership after {len(created)} rows")
            seen_context.append(kwargs.get("context"))

        with ExitStack() as stack:
            self._enter_join_sync_patches(stack, created)
            stack.enter_context(patch("handlers._common.helpers.ensure_bot_in_conversation", side_effect=ensure))
            handle_join_sync_submit(self.BODY, client, MagicMock(), self.CONTEXT)

        assert [type(record).__name__ for record in created] == ["SyncChannel"]
        assert order == ["membership after 1 rows"]
        assert seen_context == [self.CONTEXT]

    def test_failed_membership_rolls_back_and_dms_the_admin(self):
        created: list = []
        client = MagicMock()

        with ExitStack() as stack:
            self._enter_join_sync_patches(stack, created)
            stack.enter_context(
                patch(
                    "handlers._common.helpers.ensure_bot_in_conversation",
                    side_effect=ConversationAccessError("no dice"),
                )
            )
            purge = stack.enter_context(patch("handlers.channel_sync.helpers.purge_sync_channels"))
            handle_join_sync_submit(self.BODY, client, MagicMock(), self.CONTEXT)

        purge.assert_called_once()
        assert purge.call_args.args[0][0] is created[0]
        assert client.chat_postMessage.call_args.kwargs["channel"] == "U1"
        assert "no dice" in client.chat_postMessage.call_args.kwargs["text"]


class TestUnconfiguredChannelStillLeaves:
    """The leave handlers are what keep a random invite from sticking."""

    EVENT = {"team_id": "T1", "event": {"user": "UBOT", "channel": "C1"}}

    def _run(self, sync_channels: list):
        from handlers.sync import handle_member_joined_channel

        client = MagicMock()
        with (
            patch("handlers.sync.helpers.get_own_bot_user_id", return_value="UBOT"),
            patch("handlers.sync.DbManager.find_records", return_value=sync_channels),
        ):
            handle_member_joined_channel(self.EVENT, client, MagicMock(), {})
        return client

    def test_channel_with_no_sync_channel_is_left(self):
        client = self._run([])

        client.conversations_leave.assert_called_once_with(channel="C1")

    def test_configured_channel_is_kept(self):
        client = self._run([object()])

        client.conversations_leave.assert_not_called()
