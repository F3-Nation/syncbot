"""Focused unit tests for group handler edge branches."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import constants

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from handlers.group import (  # noqa: E402
    handle_accept_group_invite,
    handle_decline_group_invite,
    handle_join_group_submit,
)
from slack import actions  # noqa: E402

INVITED_WS_ID = 1
INVITER_WS_ID = 2
THIRD_PARTY_WS_ID = 99
MEMBER_ID = 7
GROUP_ID = 5


def _pending_member():
    return SimpleNamespace(
        id=MEMBER_ID,
        workspace_id=INVITED_WS_ID,
        invited_by_workspace_id=INVITER_WS_ID,
        group_id=GROUP_ID,
        status="pending",
        dm_messages=None,
    )


def _invite_body(action_id: str):
    return {
        "user": {"id": "U1", "team_id": "T1"},
        "team": {"id": "T1"},
        "actions": [{"value": str(MEMBER_ID), "action_id": f"{action_id}_{MEMBER_ID}"}],
    }


class TestAcceptGroupInviteAuthorization:
    """Accept is an invitee action: only the invited workspace may activate the invite."""

    def _run(self, acting_workspace_id):
        member = _pending_member()
        group = SimpleNamespace(id=GROUP_ID, name="Test Group")
        acting = SimpleNamespace(id=acting_workspace_id, team_id="T1", bot_token=None, deleted_at=None)

        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group.DbManager.get_record", side_effect=[member, group]),
            patch("handlers.group.DbManager.update_records") as update_records,
            patch("handlers.group.DbManager.find_records", return_value=[]),
            patch("handlers.group.helpers.get_workspace_by_id", return_value=acting),
            patch("handlers.group.helpers.resolve_workspace_name", return_value="WS"),
            patch("handlers.group._activate_group_membership"),
            patch("handlers.group._update_invite_dms"),
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
        ):
            handle_accept_group_invite(
                _invite_body(actions.CONFIG_ACCEPT_GROUP_REQUEST), MagicMock(), MagicMock(), context={}
            )
        return update_records

    def test_invited_workspace_may_accept(self):
        assert self._run(INVITED_WS_ID).called

    def test_third_party_workspace_may_not_accept(self):
        assert not self._run(THIRD_PARTY_WS_ID).called

    def test_inviting_workspace_may_not_accept_on_behalf_of_invitee(self):
        assert not self._run(INVITER_WS_ID).called

    def test_unauthorized_user_is_rejected(self):
        """When REQUIRE_ADMIN is on, _get_authorized_workspace returns None and nothing happens."""
        with (
            patch("handlers.group._get_authorized_workspace", return_value=None),
            patch("handlers.group.DbManager.get_record") as get_record,
            patch("handlers.group.DbManager.update_records") as update_records,
        ):
            handle_accept_group_invite(
                _invite_body(actions.CONFIG_ACCEPT_GROUP_REQUEST), MagicMock(), MagicMock(), context={}
            )

        assert not get_record.called
        assert not update_records.called


class TestDeclineGroupInviteAuthorization:
    """Decline is an invitee action; cancel is an inviter action. Both destroy the row."""

    def _run(self, action_id, acting_workspace_id):
        member = _pending_member()
        group = SimpleNamespace(id=GROUP_ID, name="Test Group")
        acting = SimpleNamespace(id=acting_workspace_id, team_id="T1", bot_token=None, deleted_at=None)

        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group.DbManager.get_record", side_effect=[member, group]),
            patch("handlers.group.DbManager.delete_records") as delete_records,
            patch("handlers.group.DbManager.find_records", return_value=[]),
            patch("handlers.group.helpers.get_workspace_by_id", return_value=acting),
            patch("handlers.group._update_invite_dms"),
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
        ):
            handle_decline_group_invite(_invite_body(action_id), MagicMock(), MagicMock(), context={})
        return delete_records

    def test_invited_workspace_may_decline(self):
        assert self._run(actions.CONFIG_DECLINE_GROUP_REQUEST, INVITED_WS_ID).called

    def test_third_party_may_not_decline(self):
        assert not self._run(actions.CONFIG_DECLINE_GROUP_REQUEST, THIRD_PARTY_WS_ID).called

    def test_inviter_may_not_decline(self):
        assert not self._run(actions.CONFIG_DECLINE_GROUP_REQUEST, INVITER_WS_ID).called

    def test_inviting_workspace_may_cancel(self):
        assert self._run(actions.CONFIG_CANCEL_GROUP_REQUEST, INVITER_WS_ID).called

    def test_third_party_may_not_cancel(self):
        assert not self._run(actions.CONFIG_CANCEL_GROUP_REQUEST, THIRD_PARTY_WS_ID).called

    def test_invitee_may_not_cancel(self):
        assert not self._run(actions.CONFIG_CANCEL_GROUP_REQUEST, INVITED_WS_ID).called

    def test_group_owner_may_cancel(self):
        """A group owner has standing over membership, so it may cancel a pending invite."""
        member = _pending_member()
        group = SimpleNamespace(id=GROUP_ID, name="Test Group")
        acting = SimpleNamespace(id=THIRD_PARTY_WS_ID, team_id="T1", bot_token=None, deleted_at=None)

        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group.DbManager.get_record", side_effect=[member, group]),
            patch("handlers.group.DbManager.delete_records") as delete_records,
            patch("handlers.group.DbManager.find_records", return_value=[]),
            patch("handlers.group.helpers.is_workspace_owner", return_value=True),
            patch("handlers.group.helpers.get_workspace_by_id", return_value=acting),
            patch("handlers.group._update_invite_dms"),
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
        ):
            handle_decline_group_invite(
                _invite_body(actions.CONFIG_CANCEL_GROUP_REQUEST), MagicMock(), MagicMock(), context={}
            )

        assert delete_records.called

    def test_unauthorized_user_is_rejected(self):
        with (
            patch("handlers.group._get_authorized_workspace", return_value=None),
            patch("handlers.group.DbManager.get_record") as get_record,
            patch("handlers.group.DbManager.delete_records") as delete_records,
        ):
            handle_decline_group_invite(
                _invite_body(actions.CONFIG_DECLINE_GROUP_REQUEST), MagicMock(), MagicMock(), context={}
            )

        assert not get_record.called
        assert not delete_records.called


class TestWorkspaceManagerGate:
    """Non-admins are never managers unless listed as extra managers in Settings."""

    def test_extra_manager_is_a_manager(self):
        import helpers

        client = MagicMock()
        with (
            patch("helpers.slack_api._users_info", return_value={"user": {"is_admin": False, "is_owner": False}}),
            patch("helpers.workspace_settings.extra_manager_user_ids", return_value=["U1"]),
        ):
            assert helpers.is_workspace_manager(client, "U1", "T1") is True

    def test_require_admin_env_is_ignored(self):
        import helpers
        from helpers import core as core_mod

        client = MagicMock()
        core_mod._REQUIRE_ADMIN_WARNED = False
        with (
            patch.dict(os.environ, {constants.REQUIRE_ADMIN: "false"}),
            patch("helpers.slack_api._users_info", return_value={"user": {"is_admin": False, "is_owner": False}}),
            patch("helpers.workspace_settings.extra_manager_user_ids", return_value=[]),
        ):
            assert helpers.is_workspace_manager(client, "U1", "T1") is False
        assert core_mod._REQUIRE_ADMIN_WARNED is True


class TestJoinGroupSubmit:
    def test_invalid_group_code_log_is_sanitized(self):
        client = MagicMock()
        logger = MagicMock()
        workspace = SimpleNamespace(id=42)

        body = {
            "user": {"id": "U1"},
            "view": {"state": {"values": {}}},
        }

        with (
            patch("handlers.group._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.group.forms.ENTER_GROUP_CODE_FORM.get_selected_values", return_value={}),
            patch("handlers.group.helpers._cache_get", return_value=0),
            patch("handlers.group.helpers._cache_set"),
            patch("handlers.group.DbManager.find_records", return_value=[]),
            patch("handlers.group.builders.refresh_home_tab_for_workspace"),
            patch("handlers.group._logger.warning") as warn_log,
        ):
            handle_join_group_submit(body, client, logger, context={})

        matched = [call for call in warn_log.call_args_list if call.args and call.args[0] == "group_code_invalid"]
        assert matched, "Expected group_code_invalid warning"
        extra = matched[0].kwargs["extra"]
        assert "code" not in extra
        assert extra["workspace_id"] == workspace.id
        assert extra["attempt"] == 1
        assert "code_length" in extra
