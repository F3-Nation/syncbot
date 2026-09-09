"""Tests for the Authorize SyncBot section on the Home tab.

Slack will not let an app add itself to a private channel, so SyncBot needs a
user token from whoever is publishing. This section is how a person hands that
over, which is why it is shown to everyone rather than to admins only, and why
the Home tab content hash has to be per user: a Refresh straight after
authorizing must not replay cached blocks that still show the button.

When we add user scopes later, the section comes back with two lists: what they
already granted (so it does not look like a redo) and what is still needed.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from builders.home import (
    _build_authorize_section,
    _home_tab_content_hash,
    build_home_tab,
    home_tab_hash_key,
)
from slack import actions, orm
from slack_manifest_scopes import USER_PERMISSION_GROUPS

WORKSPACE = SimpleNamespace(id=10, team_id="T1", workspace_name="WS", bot_token=None, deleted_at=None)
AUTHORIZE_URL = "https://syncbot.example.com/slack/install"
ALL_LABELS = [label for label, _scopes in USER_PERMISSION_GROUPS]


def _rendered(blocks: list) -> list[dict]:
    return orm.BlockView(blocks=blocks).as_form_field()


def _text_of(rendered: list[dict]) -> str:
    return repr(rendered)


@contextmanager
def _authorize_patches(*, needed: bool, already: list[str] | None = None, still_needed: list[str] | None = None):
    """Patch the helpers the Home tab uses to decide whether and what to show."""
    if still_needed is None:
        still_needed = ALL_LABELS if needed else []
    if already is None:
        already = []
    with (
        patch("builders.home.helpers.needs_user_authorization", return_value=needed),
        patch("builders.home.helpers.user_permission_lists", return_value=(already, still_needed)),
        patch("builders.home.helpers.authorize_url", return_value=AUTHORIZE_URL),
    ):
        yield


class TestAuthorizeSection:
    def test_hidden_when_every_current_permission_is_granted(self):
        blocks: list = []
        with (
            patch("builders.home.helpers.needs_user_authorization", return_value=False),
            patch("builders.home.helpers.authorize_url", return_value=AUTHORIZE_URL),
        ):
            shown = _build_authorize_section(blocks, "T1", "U1")

        assert shown is False
        assert blocks == []

    def test_first_visit_lists_needed_permissions_only(self):
        """Nothing granted yet, so the already-allowed list would be empty noise."""
        blocks: list = []
        with _authorize_patches(needed=True, already=[], still_needed=ALL_LABELS):
            shown = _build_authorize_section(blocks, "T1", "U1")

        rendered = _rendered(blocks)
        text = _text_of(rendered)
        assert shown is True
        assert rendered[0]["text"]["text"] == "Authorize SyncBot"
        assert "act on your behalf in this Slack Workspace" in rendered[1]["elements"][0]["text"]
        assert "Already allowed permissions" not in text
        assert "Needed permissions" in text
        assert ":white_check_mark:" not in text
        for label in ALL_LABELS:
            assert f"- {label}" in text
        button = rendered[-2]["elements"][0]
        assert button["url"] == AUTHORIZE_URL
        assert button["action_id"] == actions.CONFIG_AUTHORIZE_SYNCBOT

    def test_reauthorize_shows_already_allowed_and_needed(self):
        """A later scope change must look like an addition, not a redo."""
        already = ["Post messages", "View public Channels"]
        still_needed = ["Manage private Channels"]
        blocks: list = []
        with _authorize_patches(needed=True, already=already, still_needed=still_needed):
            shown = _build_authorize_section(blocks, "T1", "U1")

        text = _text_of(_rendered(blocks))
        assert shown is True
        assert "Already allowed permissions" in text
        assert ":white_check_mark: Post messages" in text
        assert ":white_check_mark: View public Channels" in text
        assert "Needed permissions" in text
        assert "- Manage private Channels" in text
        assert ":white_check_mark: Manage private Channels" not in text

    def test_install_link_pre_selects_this_workspace(self):
        """Most people are in several workspaces, and Slack otherwise guesses."""
        blocks: list = []
        with (
            patch("builders.home.helpers.needs_user_authorization", return_value=True),
            patch("builders.home.helpers.user_permission_lists", return_value=([], ALL_LABELS)),
            patch("builders.home.helpers.authorize_url", return_value=AUTHORIZE_URL) as authorize_url,
        ):
            _build_authorize_section(blocks, "T1", "U1")

        assert authorize_url.call_args.args == ("T1",)

    def test_italic_intro_does_not_pitch_individual_features(self):
        """The permission lists carry the detail; the intro stays one sentence."""
        blocks: list = []
        with _authorize_patches(needed=True, already=[], still_needed=ALL_LABELS):
            _build_authorize_section(blocks, "T1", "U1")

        intro = _rendered(blocks)[1]["elements"][0]["text"]
        assert "react" not in intro.lower()
        assert "private" not in intro.lower()

    def test_hidden_when_there_is_no_oauth_flow_to_link_to(self):
        """Local single-workspace mode has no install URL, so a button would be a dead end."""
        blocks: list = []
        with (
            patch("builders.home.helpers.needs_user_authorization", return_value=True),
            patch("builders.home.helpers.authorize_url", return_value=None),
        ):
            shown = _build_authorize_section(blocks, "T1", "U1")

        assert shown is False
        assert blocks == []


class TestPermissionLists:
    def test_labels_do_not_include_direct_messages_pins_or_bookmarks(self):
        assert "Send direct messages" not in ALL_LABELS
        assert "Pin Channel items" not in ALL_LABELS
        assert "Channel bookmarks" not in ALL_LABELS

    def test_no_grants_puts_every_group_in_needed(self):
        from helpers.conversations import user_permission_lists

        with patch("helpers.conversations.granted_user_scopes", return_value=frozenset()):
            already, needed = user_permission_lists("T1", "U1")

        assert already == []
        assert needed == ALL_LABELS

    def test_partial_grants_split_across_the_two_lists(self):
        from helpers.conversations import user_permission_lists

        granted = frozenset({"chat:write", "channels:history", "channels:read"})
        with patch("helpers.conversations.granted_user_scopes", return_value=granted):
            already, needed = user_permission_lists("T1", "U1")

        assert already == ["Post messages", "View public Channels"]
        assert "Manage private Channels" in needed
        assert "Post messages" not in needed

    def test_incomplete_group_stays_in_needed(self):
        """files:read without files:write is not 'already allowed' for Share files."""
        from helpers.conversations import user_permission_lists

        with patch("helpers.conversations.granted_user_scopes", return_value=frozenset({"files:read"})):
            already, needed = user_permission_lists("T1", "U1")

        assert "Share files" not in already
        assert "Share files" in needed

    def test_full_grants_mean_no_authorization_is_needed(self):
        from helpers.conversations import needs_user_authorization, user_permission_lists
        from slack_manifest_scopes import USER_SCOPES

        with patch("helpers.conversations.granted_user_scopes", return_value=frozenset(USER_SCOPES)):
            already, needed = user_permission_lists("T1", "U1")
            assert needed == []
            assert already == ALL_LABELS
            assert needs_user_authorization("T1", "U1") is False


class TestHomeTabAdminGate:
    BODY = {"team": {"id": "T1"}, "user": {"id": "U1"}}

    def _build(self, *, is_manager: bool, is_admin: bool = False, needed: bool) -> list[dict]:
        client = MagicMock()
        still_needed = ALL_LABELS if needed else []
        if is_manager and not is_admin:
            is_admin = False
        elif is_manager:
            is_admin = True
        with (
            patch("builders.home.helpers.get_workspace_record", return_value=WORKSPACE),
            patch("builders.home.helpers.is_workspace_admin", return_value=is_admin),
            patch("builders.home.helpers.is_workspace_manager", return_value=is_manager),
            patch("builders.home.helpers.extra_manager_user_ids", return_value=[]),
            patch("builders.home.helpers.needs_user_authorization", return_value=needed),
            patch("builders.home.helpers.user_permission_lists", return_value=([], still_needed)),
            patch("builders.home.helpers.authorize_url", return_value=AUTHORIZE_URL),
            patch("builders.home._get_groups_for_workspace", return_value=[]),
            patch("builders.home.DbManager.find_records", return_value=[]),
            patch("builders.home.helpers.is_settings_visible_for_workspace", return_value=True),
            patch("builders.home.helpers.is_backup_visible_for_workspace", return_value=False),
            patch("builders.home.helpers.is_db_reset_visible_for_workspace", return_value=False),
            patch("builders.home.helpers.is_primary_workspace", return_value=False),
            patch("builders.home.helpers.federation_enabled", return_value=False),
        ):
            return build_home_tab(self.BODY, client, MagicMock(), {}, user_id="U1", return_blocks=True)

    def test_non_manager_can_still_authorize(self):
        rendered = self._build(is_manager=False, needed=True)
        text = _text_of(rendered)

        assert "Authorize SyncBot" in text
        assert "This area of SyncBot is limited to Workspace managers" in text
        assert "SyncBot Configuration" in text
        assert "Refresh" in text
        assert "Create Group" not in text
        assert "Create Sync" not in text
        assert "Settings" not in text

    def test_non_manager_who_is_fully_authorized_still_gets_refresh(self):
        rendered = self._build(is_manager=False, needed=False)
        text = _text_of(rendered)

        assert "Authorize SyncBot" not in text
        assert "This area of SyncBot is limited to Workspace managers" in text
        assert "SyncBot Configuration" in text
        assert "Refresh" in text
        assert "Create Group" not in text

    def test_configuration_sits_above_workspace_groups_for_managers(self):
        rendered = self._build(is_manager=True, needed=True)
        text = _text_of(rendered)
        assert text.index("SyncBot Configuration") < text.index("Workspace Groups")
        assert text.index("Refresh") < text.index("Create Group")

    def test_manager_who_still_needs_authorization_gets_both_sections(self):
        rendered = self._build(is_manager=True, needed=True)
        text = _text_of(rendered)

        assert "Authorize SyncBot" in text
        assert "Create Group" in text
        assert "Create Sync and Join Sync" in text
        assert "Create Sync or Join Sync" in text
        assert "Publish or Subscribe" not in text
        assert "This area of SyncBot is limited to Workspace managers" not in text

    def test_manager_who_is_fully_authorized_sees_no_authorize_section(self):
        rendered = self._build(is_manager=True, needed=False)

        assert "Authorize SyncBot" not in _text_of(rendered)

    def test_non_admin_manager_does_not_see_settings(self):
        client = MagicMock()
        with (
            patch("builders.home.helpers.get_workspace_record", return_value=WORKSPACE),
            patch("builders.home.helpers.is_workspace_admin", return_value=False),
            patch("builders.home.helpers.is_workspace_manager", return_value=True),
            patch("builders.home.helpers.extra_manager_user_ids", return_value=["U1"]),
            patch("builders.home.helpers.needs_user_authorization", return_value=False),
            patch("builders.home.helpers.authorize_url", return_value=AUTHORIZE_URL),
            patch("builders.home._get_groups_for_workspace", return_value=[]),
            patch("builders.home.DbManager.find_records", return_value=[]),
            patch("builders.home.helpers.is_settings_visible_for_workspace", return_value=True),
            patch("builders.home.helpers.is_backup_visible_for_workspace", return_value=True),
            patch("builders.home.helpers.is_db_reset_visible_for_workspace", return_value=True),
            patch("builders.home.helpers.is_primary_workspace", return_value=True),
            patch("builders.home.helpers.federation_enabled", return_value=True),
        ):
            rendered = build_home_tab(self.BODY, client, MagicMock(), {}, user_id="U1", return_blocks=True)

        text = _text_of(rendered)
        assert "Refresh" in text
        assert "Create Group" in text
        assert "Settings" not in text
        assert "Backup/Restore" not in text
        assert "External Connections" not in text


class TestHomeRefreshTargets:
    def test_refresh_publishes_acting_user_only_without_users_list(self):
        from builders.home import refresh_home_tab_for_workspace

        workspace = SimpleNamespace(id=1, team_id="T1", bot_token="enc", deleted_at=None)
        logger = MagicMock()
        with (
            patch("helpers.export_import.invalidate_home_tab_caches_for_team") as invalidate,
            patch("builders.home.build_home_tab") as build,
            patch("builders.home.helpers.decrypt_bot_token", return_value="xoxb"),
            patch("builders.home.WebClient"),
            patch("builders.home.helpers.get_admin_ids") as get_admins,
        ):
            refresh_home_tab_for_workspace(workspace, logger, context={}, user_id="U1")

        invalidate.assert_called_once_with("T1")
        build.assert_called_once()
        assert build.call_args.kwargs.get("user_id") == "U1"
        get_admins.assert_not_called()

    def test_refresh_without_user_id_invalidates_only(self):
        from builders.home import refresh_home_tab_for_workspace

        workspace = SimpleNamespace(id=1, team_id="T1", bot_token="enc", deleted_at=None)
        with (
            patch("helpers.export_import.invalidate_home_tab_caches_for_team") as invalidate,
            patch("builders.home.build_home_tab") as build,
            patch("builders.home.helpers.get_admin_ids") as get_admins,
        ):
            refresh_home_tab_for_workspace(workspace, MagicMock(), context={}, user_id=None)

        invalidate.assert_called_once_with("T1")
        build.assert_not_called()
        get_admins.assert_not_called()


class TestContentHashIsPerUser:
    @pytest.fixture(autouse=True)
    def _empty_workspace(self):
        with (
            patch("builders.home._get_groups_for_workspace", return_value=[]),
            patch("builders.home.DbManager.find_records", return_value=[]),
            patch("builders.home.helpers.is_db_reset_visible_for_workspace", return_value=False),
        ):
            yield

    def test_two_users_differ_when_only_one_has_authorized(self):
        def lists(_team_id, user_id):
            if user_id == "U_AUTHORIZED":
                return (ALL_LABELS, [])
            return ([], ALL_LABELS)

        with patch("builders.home.helpers.user_permission_lists", side_effect=lists):
            authorized = _home_tab_content_hash(WORKSPACE, "U_AUTHORIZED")
            not_authorized = _home_tab_content_hash(WORKSPACE, "U_OTHER")

        assert authorized != not_authorized

    def test_hash_changes_when_new_scopes_are_still_needed(self):
        with patch("builders.home.helpers.user_permission_lists", return_value=(ALL_LABELS, [])):
            complete = _home_tab_content_hash(WORKSPACE, "U1")
        with patch(
            "builders.home.helpers.user_permission_lists",
            return_value=(ALL_LABELS[:-1], [ALL_LABELS[-1]]),
        ):
            missing_one = _home_tab_content_hash(WORKSPACE, "U1")

        assert complete != missing_one

    def test_non_manager_hash_ignores_group_data(self):
        def lists(_team_id, _user_id):
            return ([], ALL_LABELS)

        with (
            patch("builders.home.helpers.user_permission_lists", side_effect=lists),
            patch("builders.home._get_groups_for_workspace") as groups,
        ):
            first = _home_tab_content_hash(WORKSPACE, "U1", is_manager=False, is_admin=False)
            groups.return_value = [(SimpleNamespace(id=99), None)]
            second = _home_tab_content_hash(WORKSPACE, "U1", is_manager=False, is_admin=False)

        assert first == second
        groups.assert_not_called()

    def test_hash_key_is_scoped_to_the_user_under_the_team_prefix(self):
        """Restore-time invalidation deletes by the ``home_tab_hash:{team_id}`` prefix."""
        key = home_tab_hash_key("T1", "U1")

        assert key.startswith("home_tab_hash:T1")
        assert key.endswith(":U1")


class TestRefreshUsesThePerUserKey:
    def test_refresh_home_reads_and_writes_the_per_user_hash(self):
        from handlers.sync import handle_refresh_home

        client = MagicMock()
        body = {"team": {"id": "T1"}, "user": {"id": "U1"}}

        with (
            patch("handlers.sync.helpers.get_workspace_record", return_value=WORKSPACE),
            patch("handlers.sync.helpers.is_workspace_admin", return_value=True),
            patch("handlers.sync.helpers.is_workspace_manager", return_value=True),
            patch("handlers.sync.helpers.extra_manager_user_ids", return_value=[]),
            patch("handlers.sync.builders._home_tab_content_hash", return_value="hash"),
            patch("handlers.sync.helpers.refresh_cooldown_check", return_value=("cached", [], None)) as check,
            patch("handlers.sync.helpers._cache_set"),
        ):
            handle_refresh_home(body, client, MagicMock(), {})

        assert check.call_args.args[1] == "home_tab_hash:T1:U1"


class TestRefreshIsAllowedForEveryone:
    def test_non_manager_refresh_rebuilds_home_without_sweeping_workspace_names(self):
        from handlers.sync import handle_refresh_home

        client = MagicMock()
        body = {"team": {"id": "T1"}, "user": {"id": "U9"}}

        with (
            patch("handlers.sync.helpers.get_workspace_record", return_value=WORKSPACE),
            patch("handlers.sync.helpers.is_workspace_manager", return_value=False),
            patch("handlers.sync.helpers.is_workspace_admin", return_value=False),
            patch("handlers.sync.helpers.extra_manager_user_ids", return_value=[]),
            patch("handlers.sync.builders._home_tab_content_hash", return_value="hash"),
            patch("handlers.sync.helpers.refresh_cooldown_check", return_value=("rebuild", None, None)),
            patch("handlers.sync.DbManager.find_records") as find,
            patch("handlers.sync.builders.build_home_tab", return_value=[{"type": "section"}]) as build,
            patch("handlers.sync.helpers.refresh_after_full"),
        ):
            handle_refresh_home(body, client, MagicMock(), {})

        find.assert_not_called()
        client.team_info.assert_not_called()
        build.assert_called_once()
        client.views_publish.assert_called_once()

    def test_manager_refresh_does_not_team_info_every_workspace(self):
        from handlers.sync import handle_refresh_home

        client = MagicMock()
        body = {"team": {"id": "T1"}, "user": {"id": "U1"}}

        with (
            patch("handlers.sync.helpers.get_workspace_record", return_value=WORKSPACE),
            patch("handlers.sync.helpers.is_workspace_manager", return_value=True),
            patch("handlers.sync.helpers.is_workspace_admin", return_value=True),
            patch("handlers.sync.helpers.extra_manager_user_ids", return_value=[]),
            patch("handlers.sync.builders._home_tab_content_hash", return_value="hash"),
            patch("handlers.sync.helpers.refresh_cooldown_check", return_value=("rebuild", None, None)),
            patch("handlers.sync.DbManager.find_records") as find,
            patch("handlers.sync.builders.build_home_tab", return_value=[{"type": "section"}]),
            patch("handlers.sync.helpers.refresh_after_full"),
        ):
            handle_refresh_home(body, client, MagicMock(), {})

        find.assert_not_called()
        client.team_info.assert_not_called()
