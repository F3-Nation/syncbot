"""Authorization tests for the ownership and disband handlers.

These use the mocked-DbManager style of the other handler tests; the real-DB
rule semantics live in ``test_group_roles.py``.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from handlers.group_manage import (  # noqa: E402
    handle_demote_self,
    handle_disband_group_confirm,
    handle_leave_group_confirm,
    handle_promote_to_owner,
)
from slack import actions  # noqa: E402

GROUP_ID = 5
MEMBER_ID = 12
ACTING_WS_ID = 1
OTHER_WS_ID = 2


def _action_body(prefix, target_id):
    return {
        "user": {"id": "U1", "team_id": "T1"},
        "team": {"id": "T1"},
        "actions": [{"value": str(target_id), "action_id": f"{prefix}_{target_id}"}],
    }


class TestPromoteToOwner:
    def _run(self, *, acting_is_owner, target_eligible=True):
        acting = SimpleNamespace(id=ACTING_WS_ID, team_id="T1", bot_token=None, deleted_at=None)
        # get_record is mocked for both the member and the group lookup, so this
        # stands in for either; `name` is the group's.
        target = SimpleNamespace(id=MEMBER_ID, group_id=GROUP_ID, workspace_id=OTHER_WS_ID, role="member", name="G")
        eligible = [target] if target_eligible else []

        with (
            patch("handlers._common._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group_manage.DbManager.get_record", return_value=target),
            patch("handlers.group_manage.helpers.is_workspace_owner", return_value=acting_is_owner),
            patch("handlers.group_manage.helpers.get_promotable_members", return_value=eligible),
            patch("handlers.group_manage.DbManager.update_records") as update_records,
            patch("handlers.group_manage.helpers.get_workspace_by_id", return_value=acting),
            patch("handlers.group_manage.helpers.resolve_workspace_name", return_value="WS"),
            patch("handlers.group_manage._notify_group_admins"),
            patch("handlers.group_manage.builders.refresh_home_tab_for_workspace"),
        ):
            handle_promote_to_owner(
                _action_body(actions.CONFIG_PROMOTE_TO_OWNER, MEMBER_ID), MagicMock(), MagicMock(), context={}
            )
        return update_records

    def test_owner_may_promote_an_eligible_member(self):
        assert self._run(acting_is_owner=True).called

    def test_non_owner_may_not_promote(self):
        assert not self._run(acting_is_owner=False).called

    def test_ineligible_target_is_rejected(self):
        """Pending invitees and federated members are filtered out upstream."""
        assert not self._run(acting_is_owner=True, target_eligible=False).called


class TestDemoteSelf:
    def _run(self, *, target_workspace_id, owner_count):
        acting = SimpleNamespace(id=ACTING_WS_ID, team_id="T1", bot_token=None, deleted_at=None)
        target = SimpleNamespace(
            id=MEMBER_ID, group_id=GROUP_ID, workspace_id=target_workspace_id, role="owner", name="G"
        )
        owners = [SimpleNamespace(id=MEMBER_ID + i, workspace_id=i) for i in range(owner_count)]
        owners[0] = SimpleNamespace(id=MEMBER_ID, workspace_id=target_workspace_id)

        with (
            patch("handlers._common._get_authorized_workspace", return_value=("U1", acting)),
            patch("handlers.group_manage.DbManager.get_record", return_value=target),
            patch("handlers.group_manage.helpers.get_active_owners", return_value=owners),
            patch("handlers.group_manage.DbManager.update_records") as update_records,
            patch("handlers.group_manage.helpers.resolve_workspace_name", return_value="WS"),
            patch("handlers.group_manage._notify_group_admins"),
            patch("handlers.group_manage.builders.refresh_home_tab_for_workspace"),
        ):
            handle_demote_self(
                _action_body(actions.CONFIG_DEMOTE_SELF, MEMBER_ID), MagicMock(), MagicMock(), context={}
            )
        return update_records

    def test_owner_may_demote_itself_while_another_owner_remains(self):
        assert self._run(target_workspace_id=ACTING_WS_ID, owner_count=2).called

    def test_sole_owner_may_not_demote_itself(self):
        assert not self._run(target_workspace_id=ACTING_WS_ID, owner_count=1).called

    def test_an_owner_may_not_demote_a_peer(self):
        """Self-demotion only: letting owners demote each other invites ownership fights."""
        assert not self._run(target_workspace_id=OTHER_WS_ID, owner_count=2).called


class TestDisbandConfirm:
    def _run(self, *, can_disband, blocked=False):
        acting = SimpleNamespace(id=ACTING_WS_ID, team_id="T1", bot_token=None, deleted_at=None)
        group = SimpleNamespace(id=GROUP_ID, name="G")
        meta = {"group_id": GROUP_ID}
        if blocked:
            meta["blocked"] = True

        with (
            patch("handlers.group_manage.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
            patch("handlers._common._parse_private_metadata", return_value=meta),
            patch("handlers.group_manage.helpers.get_workspace_record", return_value=acting),
            patch(
                "handlers.group_manage.helpers.can_disband",
                return_value=(can_disband, "" if can_disband else "co_owner_exists"),
            ),
            patch("handlers.group_manage.DbManager.find_records", return_value=[group]),
            patch("handlers.group_manage.helpers.format_admin_label", return_value=("A", "A from WS")),
            patch("handlers.group_manage._notify_group_admins"),
            patch("handlers.group_manage.DbManager.delete_records") as delete_records,
            patch("handlers.group_manage.builders.refresh_home_tab_for_workspace"),
        ):
            handle_disband_group_confirm({"view": {"team_id": "T1"}}, MagicMock(), MagicMock(), context={})
        return delete_records

    def test_disband_proceeds_when_both_gates_pass(self):
        assert self._run(can_disband=True).called

    def test_disband_is_rejected_on_submit_when_a_gate_fails(self):
        """main_response has no auth gate, so the modal-time check is bypassable."""
        assert not self._run(can_disband=False).called

    def test_the_blocked_explanation_modal_does_not_disband(self):
        assert not self._run(can_disband=True, blocked=True).called


class TestLeaveGroupConfirmOwnerGuard:
    def _run(self, *, can_leave, blocked=False):
        acting = SimpleNamespace(id=ACTING_WS_ID, team_id="T1", bot_token=None, deleted_at=None)
        meta = {"group_id": GROUP_ID}
        if blocked:
            meta["blocked"] = True

        with (
            patch("handlers.group_manage.helpers.get_user_id_from_body", return_value="U1"),
            patch("handlers.group_manage.helpers.is_workspace_manager", return_value=True),
            patch("handlers._common._parse_private_metadata", return_value=meta),
            patch("handlers.group_manage.helpers.get_workspace_record", return_value=acting),
            patch(
                "handlers.group_manage.helpers.can_workspace_leave",
                return_value=(can_leave, "" if can_leave else "sole_owner"),
            ),
            patch("handlers.group_manage.DbManager.find_records", return_value=[]) as find_records,
        ):
            handle_leave_group_confirm({"view": {"team_id": "T1"}}, MagicMock(), MagicMock(), context={})
        return find_records

    def test_a_permitted_departure_proceeds_past_the_guard(self):
        assert self._run(can_leave=True).called

    def test_a_sole_owner_departure_is_rejected_on_submit(self):
        assert not self._run(can_leave=False).called

    def test_the_blocked_explanation_modal_does_not_leave(self):
        assert not self._run(can_leave=True, blocked=True).called
