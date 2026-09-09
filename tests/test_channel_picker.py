"""Tests for the native channel picker and its submit-time validation.

The picker used to be a static list built by enumerating ``conversations.list``,
which silently capped at ~100 options and made larger channels unreachable. It is
now Slack's ``conversations_select``, which cannot filter by app-side state, so
eligibility is checked when the modal is submitted instead.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from handlers.channel_sync import (
    _channel_picker_block,
    _channel_picker_help_text,
    _validate_channel_selection,
)
from slack import actions


@pytest.fixture
def client():
    return MagicMock()


class TestPickerBlock:
    def test_uses_native_conversations_select(self):
        with patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False):
            block = _channel_picker_block("Channel", actions.CONFIG_CREATE_SYNC_SELECT, team_id="T1")

        rendered = block.as_form_field()
        assert rendered["element"]["type"] == "conversations_select"
        assert rendered["element"]["filter"]["include"] == ["public"]

    def test_private_channels_included_when_policy_allows(self):
        with patch("handlers.channel_sync.helpers.allow_private_channels", return_value=True):
            block = _channel_picker_block("Channel", actions.CONFIG_CREATE_SYNC_SELECT, team_id="T1")

        include = block.as_form_field()["element"]["filter"]["include"]
        assert "private" in include

    def test_help_text_warns_when_private_channels_allowed(self):
        with patch("handlers.channel_sync.helpers.allow_private_channels", return_value=True):
            assert "Private Channels are currently allowed" in _channel_picker_help_text(team_id="T1")

        with patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False):
            assert "Only public Channels can be synced" in _channel_picker_help_text(team_id="T1")

        with patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False):
            assert "join this Sync" in _channel_picker_help_text(team_id="T1", subscribe=True)


class TestValidateChannelSelection:
    ACTION = actions.CONFIG_CREATE_SYNC_SELECT

    def test_missing_selection_is_an_error(self, client):
        result = _validate_channel_selection(client, None, self.ACTION)
        assert result["response_action"] == "errors"
        assert self.ACTION in result["errors"]

    def test_placeholder_selection_is_an_error(self, client):
        result = _validate_channel_selection(client, "__none__", self.ACTION)
        assert result["response_action"] == "errors"

    def test_channel_in_another_sync_is_allowed(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": False}}
        with (
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            assert _validate_channel_selection(client, "C1", self.ACTION) is None

    def test_workspace_already_subscribed_to_source_is_rejected(self, client):
        source = SimpleNamespace(workspace_id=1, channel_id="CSOURCE", publishes=True)
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[source]),
            patch("handlers.channel_sync.helpers.already_subscribed_to_source", return_value=True),
        ):
            result = _validate_channel_selection(
                client,
                "C1",
                self.ACTION,
                workspace_id=2,
                source_sync_id=42,
            )

        assert "already subscribes" in result["errors"][self.ACTION]

    def test_duplicate_subscribe_checks_every_publisher_in_the_sync(self, client):
        original = SimpleNamespace(workspace_id=1, channel_id="C_OLD", publishes=False)
        second = SimpleNamespace(workspace_id=3, channel_id="C_NEW", publishes=True)
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[original, second]),
            patch(
                "handlers.channel_sync.helpers.already_subscribed_to_source",
                side_effect=lambda **kwargs: kwargs["source_channel_id"] == "C_NEW",
            ) as already,
        ):
            result = _validate_channel_selection(
                client,
                "C1",
                self.ACTION,
                workspace_id=2,
                source_sync_id=42,
            )

        assert "already subscribes" in result["errors"][self.ACTION]
        assert already.call_count == 1
        assert already.call_args.kwargs["source_channel_id"] == "C_NEW"

    def test_eligible_public_channel_passes(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": False}}
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            assert _validate_channel_selection(client, "C1", self.ACTION) is None

    def test_private_channel_rejected_by_default(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": True}}
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            result = _validate_channel_selection(client, "C1", self.ACTION)

        assert "Private Channels cannot be synced" in result["errors"][self.ACTION]

    def test_private_channel_allowed_when_setting_is_on_and_a_token_exists(self, client):
        """With a user token to invite with, the pick is accepted without a lookup.

        The bot often cannot see a private channel at all, so its own view of the
        channel says nothing useful once either path would work.
        """
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=True),
            patch("handlers.channel_sync.helpers.has_user_token", return_value=True),
        ):
            assert _validate_channel_selection(client, "C1", self.ACTION, team_id="T1", acting_user_id="U1") is None

        client.conversations_info.assert_not_called()

    def test_private_pick_without_a_user_token_points_at_authorize(self, client):
        """Only a member can add an app to a private channel, so say so up front."""
        client.conversations_info.return_value = {"channel": {"is_private": True}}
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=True),
            patch("handlers.channel_sync.helpers.has_user_token", return_value=False),
        ):
            result = _validate_channel_selection(client, "C1", self.ACTION, team_id="T1", acting_user_id="U1")

        assert "Authorize SyncBot" in result["errors"][self.ACTION]

    def test_public_pick_without_a_user_token_still_passes(self, client):
        """A public channel is joined with the bot token, so no authorization is needed."""
        client.conversations_info.return_value = {"channel": {"is_private": False}}
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=True),
            patch("handlers.channel_sync.helpers.has_user_token", return_value=False),
        ):
            assert _validate_channel_selection(client, "C1", self.ACTION, team_id="T1", acting_user_id="U1") is None

    def test_unreadable_channel_is_treated_as_private_when_private_is_allowed(self, client):
        """A private channel the bot has never been in is invisible to the bot token."""
        client.conversations_info.side_effect = Exception("channel_not_found")
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=True),
            patch("handlers.channel_sync.helpers.has_user_token", return_value=False),
        ):
            result = _validate_channel_selection(client, "C1", self.ACTION, team_id="T1", acting_user_id="U1")

        assert "Authorize SyncBot" in result["errors"][self.ACTION]

    def test_unreadable_channel_fails_closed(self, client):
        """A channel SyncBot cannot inspect is one it cannot join either."""
        client.conversations_info.side_effect = Exception("channel_not_found")
        with (
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            result = _validate_channel_selection(client, "C1", self.ACTION)

        assert "could not read that Channel" in result["errors"][self.ACTION]

    def test_republishing_a_previously_used_channel_is_allowed(self, client):
        client.conversations_info.return_value = {"channel": {"is_private": False}}
        with (
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            assert _validate_channel_selection(client, "C1", self.ACTION) is None


class TestJoinSyncAckSurfacesErrors:
    def test_ineligible_channel_returns_errors_response(self):
        from handlers.channel_sync import handle_join_sync_submit_ack

        client = MagicMock()
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": 55}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="Cdup"),
            patch(
                "handlers.channel_sync._validate_channel_selection",
                return_value={
                    "response_action": "errors",
                    "errors": {actions.CONFIG_JOIN_SYNC_SELECT: "duplicate"},
                },
            ),
        ):
            result = handle_join_sync_submit_ack({}, client, {})

        assert result["response_action"] == "errors"
        assert actions.CONFIG_JOIN_SYNC_SELECT in result["errors"]

    def test_missing_sync_id_does_not_claim_success(self):
        from handlers.channel_sync import handle_join_sync_submit_ack

        client = MagicMock()
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={}),
        ):
            assert handle_join_sync_submit_ack({}, client, {}) is None

    def test_eligible_channel_acks_empty(self):
        from handlers.channel_sync import handle_join_sync_submit_ack

        client = MagicMock()
        client.conversations_info.return_value = {"channel": {"is_private": False}}
        workspace = SimpleNamespace(id=10, team_id="T1")

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.channel_sync._parse_private_metadata", return_value={"sync_id": 55}),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="Cnew"),
            patch("handlers.channel_sync.DbManager.get_record", return_value=None),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[]),
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
        ):
            assert handle_join_sync_submit_ack({}, client, {}) is None


class TestJoinSyncIsRoutedForDeferredAck:
    def test_join_sync_submit_has_an_ack_handler(self):
        """Without a VIEW_ACK_MAPPER entry the field errors never reach Slack."""
        import handlers
        import routing

        assert routing.VIEW_ACK_MAPPER[actions.CONFIG_JOIN_SYNC_SUBMIT] is (handlers.handle_join_sync_submit_ack)


class TestCreateJoinFormsRespectPolicy:
    """Create Sync / Join Sync modals share the conversations picker policy."""

    def test_deep_copied_form_can_be_switched_to_public_only(self):
        import copy

        from slack import forms

        form = copy.deepcopy(forms.CREATE_SYNC_FORM)
        form.set_conversations_include_private(False)
        rendered = form.as_form_field()

        pickers = [b["element"] for b in rendered if b.get("element", {}).get("type") == "conversations_select"]
        assert pickers
        assert all(p["filter"]["include"] == ["public"] for p in pickers)

    def test_deep_copied_form_can_include_private(self):
        import copy

        from slack import forms

        form = copy.deepcopy(forms.JOIN_SYNC_FORM)
        form.set_conversations_include_private(True)
        rendered = form.as_form_field()

        pickers = [b["element"] for b in rendered if b.get("element", {}).get("type") == "conversations_select"]
        assert pickers
        assert all("private" in p["filter"]["include"] for p in pickers)
