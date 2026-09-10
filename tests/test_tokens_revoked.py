"""tokens_revoked and app_uninstalled use Bolt's installation store methods."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from handlers.tokens import handle_app_uninstalled, handle_tokens_revoked

WORKSPACE = SimpleNamespace(id=10, team_id="T1", workspace_name="WS", bot_token="enc", deleted_at=None)


def _body(*, oauth=None, bot=None):
    return {
        "team_id": "T1",
        "event": {"type": "tokens_revoked", "tokens": {"oauth": oauth or [], "bot": bot or []}},
    }


class TestTokensRevokedOauthOnly:
    def test_clears_that_users_token_and_republishes_home_without_uninstalling(self):
        with (
            patch("handlers.tokens.helpers.clear_user_authorization", return_value=True) as clear,
            patch("handlers.tokens._republish_home_after_user_revoke") as republish,
            patch("handlers.tokens._uninstall_workspace") as uninstall,
        ):
            handle_tokens_revoked(_body(oauth=["U9"]), MagicMock(), MagicMock(), {})

        clear.assert_called_once_with("T1", "U9")
        republish.assert_called_once()
        assert republish.call_args.args[:2] == ("T1", "U9")
        uninstall.assert_not_called()

    def test_bot_token_revoke_is_still_a_workspace_uninstall_when_bot_is_dead(self):
        with (
            patch("handlers.tokens.helpers.clear_user_authorization") as clear,
            patch("handlers.tokens._republish_home_after_user_revoke") as republish,
            patch("handlers.tokens._uninstall_workspace") as uninstall,
            patch("handlers.tokens.DbManager.get_record", return_value=WORKSPACE),
            patch("handlers.tokens._workspace_bot_is_alive", return_value=False),
        ):
            handle_tokens_revoked(_body(oauth=["U9"], bot=["U_BOT"]), MagicMock(), MagicMock(), {})

        uninstall.assert_called_once_with("T1")
        clear.assert_not_called()
        republish.assert_not_called()

    def test_bot_array_on_user_revoke_does_not_uninstall_when_bot_token_still_works(self):
        with (
            patch("handlers.tokens.helpers.clear_user_authorization", return_value=True) as clear,
            patch("handlers.tokens._republish_home_after_user_revoke") as republish,
            patch("handlers.tokens._uninstall_workspace") as uninstall,
            patch("handlers.tokens.DbManager.get_record", return_value=WORKSPACE),
            patch("handlers.tokens._workspace_bot_is_alive", return_value=True),
        ):
            handle_tokens_revoked(_body(oauth=["U9"], bot=["U_BOT"]), MagicMock(), MagicMock(), {})

        uninstall.assert_not_called()
        clear.assert_called_once_with("T1", "U9")
        republish.assert_called_once()
        assert republish.call_args.args[:2] == ("T1", "U9")


class TestAppUninstalled:
    def test_wipes_installations_and_pauses_the_workspace(self):
        with patch("handlers.tokens._uninstall_workspace") as uninstall:
            handle_app_uninstalled(
                {"team_id": "T1", "event": {"type": "app_uninstalled"}},
                MagicMock(),
                MagicMock(),
                {},
            )

        uninstall.assert_called_once_with("T1")


class TestUninstallWorkspace:
    def test_uses_bolt_delete_all_then_soft_deletes(self):
        from handlers.tokens import _uninstall_workspace

        with (
            patch("handlers.tokens.helpers.clear_workspace_installations") as purge,
            patch("handlers.tokens._soft_delete_uninstalled_workspace") as soft,
        ):
            _uninstall_workspace("T1")

        purge.assert_called_once_with("T1")
        soft.assert_called_once_with("T1")


class TestClearUserAuthorization:
    def test_deletes_the_user_scoped_installation_row(self):
        from helpers.conversations import clear_user_authorization

        store = MagicMock()
        with patch("helpers.conversations._installation_store", return_value=store):
            assert clear_user_authorization("T1", "U1") is True

        store.delete_installation.assert_called_once_with(enterprise_id=None, team_id="T1", user_id="U1")


class TestClearWorkspaceInstallations:
    def test_calls_bolt_delete_all(self):
        from helpers.conversations import clear_workspace_installations

        store = MagicMock()
        with patch("helpers.conversations._installation_store", return_value=store):
            assert clear_workspace_installations("T1") is True

        store.delete_all.assert_called_once_with(enterprise_id=None, team_id="T1")
