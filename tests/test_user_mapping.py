"""User Mapping modal, directory email map, and Auto Map Now."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from builders.user_mapping import (  # noqa: E402
    build_user_mapping_entry,
    build_user_mapping_list_blocks,
)
from handlers.channel_sync import handle_create_sync_submit_work  # noqa: E402
from handlers.group import _activate_group_membership  # noqa: E402
from handlers.users import (  # noqa: E402
    handle_user_mapping_auto_map,
    handle_user_mapping_edit_submit,
    handle_user_mapping_refresh,
)
from helpers.user_map import (  # noqa: E402
    _find_user_map,
    _map_from_directory,
    ensure_mapped_target_user_id,
    format_last_auto_map_line,
    get_display_name_and_icon_for_synced_message,
    run_auto_map_for_workspace,
    seed_user_mappings,
)
from slack.orm import BlockView  # noqa: E402
from tests.event_fixtures import make_event_context  # noqa: E402


class TestDirectoryEmailMatch:
    def test_email_map_from_directory_rows_without_client(self):
        source = {"email": "Same@Example.com", "real_name": "Ada", "display_name": "Ada"}
        target = SimpleNamespace(
            slack_user_id="U_TARGET",
            email="same@example.com",
            real_name="Ada Lovelace",
            display_name="Ada",
            normalized_name="Ada",
        )
        by_email = {"same@example.com": [target]}
        uid, method = _map_from_directory(source, [target], by_email)
        assert uid == "U_TARGET"
        assert method == "email"

    def test_find_user_map_skips_lookup_when_directory_has_email(self):
        source = {"email": "a@ex.com", "real_name": "A", "display_name": "A"}
        target = SimpleNamespace(
            slack_user_id="U2",
            email="a@ex.com",
            real_name="A",
            display_name="A",
            normalized_name="A",
        )
        client = MagicMock()
        uid, method = _find_user_map(
            "U1",
            source,
            client,
            target_workspace_id=2,
            target_candidates=[target],
            target_by_email={"a@ex.com": [target]},
        )
        assert uid == "U2"
        assert method == "email"
        client.users_lookupByEmail.assert_not_called()


class TestSeedWithoutPartnerCrawl:
    def test_seed_runs_when_partner_directory_empty(self):
        source_entries = [
            SimpleNamespace(
                slack_user_id="U_SRC",
                display_name="Src",
                real_name="Src",
                email="s@ex.com",
            )
        ]
        with (
            patch("helpers.user_map.DbManager.find_records") as find,
            patch("helpers.user_map.DbManager.create_records") as create_many,
            patch("helpers.user_map.DbManager.update_records"),
        ):
            find.side_effect = [source_entries, []]
            created = seed_user_mappings(1, 2, group_id=9)
        assert created == 1
        create_many.assert_called_once()
        row = create_many.call_args.args[0][0]
        assert row.source_user_id == "U_SRC"
        assert row.target_workspace_id == 2
        assert row.map_method == "none"

    def test_auto_map_does_not_refresh_directory(self):
        mapping = SimpleNamespace(
            id=1,
            source_workspace_id=1,
            source_user_id="U_SRC",
            target_workspace_id=2,
            map_method="none",
        )
        target_dir = SimpleNamespace(
            slack_user_id="U_TGT",
            email="a@ex.com",
            real_name="Ada",
            display_name="Ada",
            normalized_name="Ada",
            deleted_at=None,
        )

        with (
            patch("helpers.user_map.DbManager.find_records") as find,
            patch("helpers.user_map.DbManager.update_records") as update,
            patch("helpers.user_map._source_profile_from_directory") as profile,
        ):
            find.side_effect = [[mapping], [target_dir]]
            profile.return_value = {
                "email": "a@ex.com",
                "real_name": "Ada",
                "display_name": "Ada",
            }
            newly, still = run_auto_map_for_workspace(None, 2, seeded=1)

        assert newly == 1
        assert still == 0
        assert update.called

    def test_auto_map_skips_source_slack_lookup_when_disallowed(self):
        mapping = SimpleNamespace(
            id=1,
            source_workspace_id=1,
            source_user_id="U_SRC",
            target_workspace_id=2,
            map_method="none",
        )
        with (
            patch("helpers.user_map.DbManager.find_records") as find,
            patch("helpers.user_map._source_profile_from_directory", return_value=None),
            patch("helpers.user_map._get_source_profile_full") as slack_lookup,
            patch("helpers.user_map.get_workspace_by_id") as get_ws,
        ):
            find.side_effect = [[mapping], []]
            newly, still = run_auto_map_for_workspace(MagicMock(), 2, allow_slack_email_lookup=False, seeded=0)

        assert newly == 0
        assert still == 1
        slack_lookup.assert_not_called()
        get_ws.assert_not_called()


class TestLastAutoMapStatus:
    def test_format_twenty_new_found(self):
        line = format_last_auto_map_line({"at": "2026-09-02T12:00:00Z", "newly_matched": 20, "still_unmatched": 3})
        assert line == "Last run on September 2, 2026 with 20 new found."

    def test_format_zero_new_found(self):
        line = format_last_auto_map_line({"at": "2026-09-02T12:00:00Z", "newly_matched": 0, "still_unmatched": 5})
        assert line == "Last run on September 2, 2026 with 0 new found."

    def test_format_none_is_empty_hint(self):
        line = format_last_auto_map_line(None)
        assert "No auto map yet" in line


class TestUserMappingModal:
    def test_open_posts_db_list_without_seed_or_map(self):
        workspace = SimpleNamespace(id=1, team_id="T1", bot_token="xoxb-1")
        body = {
            "trigger_id": "trig",
            "user": {"id": "U1"},
            "team": {"id": "T1"},
            "actions": [{"value": "5"}],
        }
        client = MagicMock()
        with (
            patch("builders.user_mapping._deny_unauthorized", return_value=False),
            patch("builders.user_mapping.helpers.get_workspace_record", return_value=workspace),
            patch(
                "builders.user_mapping.build_user_mapping_list_blocks",
                return_value=([MagicMock()], {"group_id": 5, "page": 0}),
            ),
            patch("builders.user_mapping.orm.BlockView.post_modal") as post,
            patch("builders.user_mapping.seed_mappings_for_workspace") as seed,
            patch("helpers.run_auto_map_for_workspace") as auto_map,
        ):
            build_user_mapping_entry(body, client, MagicMock(), {})

        post.assert_called_once()
        seed.assert_not_called()
        auto_map.assert_not_called()
        client.views_publish.assert_not_called()

    def test_edit_save_does_not_refresh_home(self):
        workspace = SimpleNamespace(id=1, team_id="T1")
        mapping = SimpleNamespace(
            id=99,
            source_workspace_id=2,
            target_workspace_id=1,
            target_user_id=None,
            map_method="none",
        )
        body = {
            "view": {
                "state": {
                    "values": {
                        "b1": {"user_mapping_edit_select": {"selected_user": "U_LOCAL"}},
                    }
                },
                "private_metadata": '{"mapping_id": 99, "group_id": 5, "page": 0, "parent_view_id": "V1"}',
            }
        }
        with (
            patch("handlers.users._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.users.DbManager.get_record", return_value=mapping),
            patch("handlers.users.DbManager.find_records", return_value=[]),
            patch("handlers.users.DbManager.update_records"),
            patch("handlers.users.update_user_mapping_modal") as update_modal,
            patch("handlers.users.builders.refresh_home_tab_for_workspace") as refresh_home,
        ):
            handle_user_mapping_edit_submit(body, MagicMock(), MagicMock(), {})

        update_modal.assert_called_once()
        refresh_home.assert_not_called()

    def test_refresh_reloads_from_db_without_crawl(self):
        workspace = SimpleNamespace(id=10, team_id="T1")
        body = {
            "view": {"id": "V1", "private_metadata": '{"group_id": 3, "page": 0}'},
            "actions": [{"action_id": "user_mapping_refresh", "value": "3"}],
        }
        client = MagicMock()
        with (
            patch("handlers.users._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.users.seed_mappings_for_workspace") as seed,
            patch("handlers.users.helpers.run_auto_map_for_workspace") as auto_map,
            patch("handlers.users.update_user_mapping_modal") as update_modal,
        ):
            handle_user_mapping_refresh(body, client, MagicMock(), {})

        seed.assert_not_called()
        auto_map.assert_not_called()
        update_modal.assert_called_once()
        assert update_modal.call_args.kwargs.get("mapping_in_progress", False) is False

    def test_auto_map_updates_modal_mapping_then_results(self):
        workspace = SimpleNamespace(id=10, team_id="T1")
        body = {
            "view": {"id": "V1", "private_metadata": '{"group_id": 3, "page": 0}'},
            "actions": [{"action_id": "user_mapping_auto_map", "value": "3"}],
        }
        client = MagicMock()
        with (
            patch("handlers.users._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.users.helpers._cache_get", return_value=None),
            patch("handlers.users.helpers._cache_set") as cache_set,
            patch("handlers.users.helpers._cache_delete"),
            patch("handlers.users.seed_mappings_for_workspace", return_value=2) as seed,
            patch(
                "handlers.users.helpers.run_auto_map_for_workspace",
                return_value=(20, 3),
            ) as auto_map,
            patch("handlers.users.set_last_auto_map") as set_last,
            patch("handlers.users.helpers._cache_delete_prefix"),
            patch("handlers.users.update_user_mapping_modal") as update_modal,
            patch("handlers.users.builders.refresh_home_tab_for_workspace") as refresh_home,
        ):
            handle_user_mapping_auto_map(body, client, MagicMock(), {})

        seed.assert_called_once()
        auto_map.assert_called_once()
        assert auto_map.call_args.kwargs.get("allow_slack_email_lookup") is False
        set_last.assert_called_once()
        assert set_last.call_args.kwargs["newly_matched"] == 20
        assert update_modal.call_count >= 2
        assert update_modal.call_args_list[0].kwargs.get("mapping_in_progress") is True
        assert update_modal.call_args_list[-1].kwargs.get("mapping_in_progress", False) is False
        refresh_home.assert_not_called()
        cache_set.assert_called()

    def test_second_auto_map_click_does_not_start_another_job(self):
        workspace = SimpleNamespace(id=10, team_id="T1")
        body = {
            "view": {"id": "V1", "private_metadata": '{"group_id": 3, "page": 0}'},
            "actions": [{"action_id": "user_mapping_auto_map", "value": "3"}],
        }
        with (
            patch("handlers.users._get_authorized_workspace", return_value=("U1", workspace)),
            patch("handlers.users.helpers._cache_get", return_value=True),
            patch("handlers.users.seed_mappings_for_workspace") as seed,
            patch("handlers.users.helpers.run_auto_map_for_workspace") as auto_map,
            patch("handlers.users.update_user_mapping_modal") as update_modal,
        ):
            handle_user_mapping_auto_map(body, MagicMock(), MagicMock(), {})

        seed.assert_not_called()
        auto_map.assert_not_called()
        update_modal.assert_called_once()
        assert update_modal.call_args.kwargs.get("mapping_in_progress") is True

    def test_mapping_in_progress_is_short_placeholder(self):
        workspace = SimpleNamespace(id=1, team_id="T1")
        with (
            patch("builders.user_mapping.DbManager.find_records", return_value=[]),
            patch("builders.user_mapping.get_last_auto_map", return_value=None),
            patch("builders.user_mapping._linked_workspace_ids") as linked,
            patch("builders.user_mapping._collect_mappings") as collect,
        ):
            blocks, _meta = build_user_mapping_list_blocks(workspace, group_id=5, mapping_in_progress=True)

        linked.assert_not_called()
        collect.assert_not_called()
        blob = str(BlockView(blocks=blocks).as_form_field())
        assert "Mapping users..." in blob
        assert "user_mapping_auto_map" not in blob
        assert "Refresh List" in blob
        assert "Users with the same email across Workspaces can be found by clicking Auto Map Now." in blob

    def test_idle_list_uses_map_copy(self):
        workspace = SimpleNamespace(id=1, team_id="T1")
        with (
            patch("builders.user_mapping.DbManager.find_records", return_value=[]),
            patch("builders.user_mapping.get_last_auto_map", return_value=None),
            patch("builders.user_mapping._linked_workspace_ids", return_value=set()),
            patch("builders.user_mapping._collect_mappings", return_value=[]),
        ):
            blocks, _meta = build_user_mapping_list_blocks(workspace, group_id=5, mapping_in_progress=False)

        blob = str(BlockView(blocks=blocks).as_form_field())
        assert "Auto Map Now" in blob
        assert "user_mapping_auto_map" in blob
        assert "Refresh List" in blob
        assert "Auto-match" not in blob


class TestLeftoverActionIdsDropped:
    def test_old_match_action_ids_not_routed(self):
        from routing import ACTION_MAPPER

        assert "manage_user_matching" not in ACTION_MAPPER
        assert "user_mapping_auto_match" not in ACTION_MAPPER
        assert "manage_user_mapping" in ACTION_MAPPER
        assert "user_mapping_auto_map" in ACTION_MAPPER


class TestJoinSeedsOnly:
    def test_activate_membership_seeds_without_crawl_or_map(self):
        workspace = SimpleNamespace(id=1, team_id="T1", bot_token="enc", deleted_at=None)
        group = SimpleNamespace(id=9, name="G")
        partner = SimpleNamespace(id=2, team_id="T2", bot_token="enc", deleted_at=None)
        member = SimpleNamespace(workspace_id=2)

        with (
            patch("handlers.group.DbManager.find_records", return_value=[member]),
            patch("handlers.group.helpers.get_workspace_by_id", return_value=partner),
            patch("handlers.group.helpers.seed_user_mappings") as seed,
            patch("handlers.group.helpers.run_auto_map_for_workspace") as auto_map,
        ):
            _activate_group_membership(MagicMock(), workspace, group)

        assert seed.call_count == 2
        auto_map.assert_not_called()


class TestCreateSyncAnnouncement:
    def test_create_sync_posts_announcement_after_membership(self):
        workspace = SimpleNamespace(id=10, team_id="T1")
        client = MagicMock()
        body = {"view": {"team_id": "T1"}, "user": {"id": "U1"}}
        created: list = []

        def create_record(record):
            record.id = 99 + len(created)
            created.append(record)

        with (
            patch("handlers.channel_sync._get_authorized_workspace", return_value=("U1", workspace)),
            patch(
                "handlers.channel_sync._parse_private_metadata",
                return_value={"group_id": 5, "sync_mode": "group"},
            ),
            patch("handlers.channel_sync._get_selected_conversation_or_option", return_value="Cpub"),
            patch("handlers.channel_sync._get_selected_option_value", return_value=None),
            patch("handlers.channel_sync._validate_channel_selection", return_value=None),
            patch("handlers.channel_sync.helpers.get_user_token", return_value=None),
            patch("handlers.channel_sync.helpers.lookup_channel_meta", return_value=("general", False)),
            patch("handlers.channel_sync.DbManager.create_record", side_effect=create_record),
            patch("handlers.channel_sync.DbManager.find_records", return_value=[SimpleNamespace(name="Region")]),
            patch("handlers.channel_sync._ensure_membership_or_rollback", return_value=True),
            patch("handlers.channel_sync.helpers.format_admin_label", return_value=("Ada", "Ada (WS)")),
            patch("handlers.channel_sync.builders.refresh_home_tab_for_workspace"),
            patch("handlers.channel_sync._refresh_group_member_homes"),
        ):
            handle_create_sync_submit_work(body, client, MagicMock(), {})

        assert client.chat_postMessage.called
        announce = [c for c in client.chat_postMessage.call_args_list if c.kwargs.get("channel") == "Cpub"]
        assert len(announce) == 1
        text = announce[0].kwargs["text"]
        assert "created a Sync for this Channel" in text
        assert "Ada" in text


class TestEnsureMappedTargetUserId:
    def test_unique_target_directory_email_persists_without_lookup(self):
        target = SimpleNamespace(
            slack_user_id="U_DEST",
            email="same@ex.com",
            real_name="Ada",
            display_name="Ada",
            normalized_name="Ada",
        )
        target_client = MagicMock()
        with (
            patch("helpers.user_map._mapping_row_for_pair", return_value=None),
            patch(
                "helpers.user_map._source_profile_from_directory",
                return_value={"email": "same@ex.com", "display_name": "Ada", "real_name": "Ada"},
            ),
            patch("helpers.user_map._get_source_profile_full") as source_slack,
            patch("helpers.user_map.DbManager.find_records", return_value=[target]),
            patch("helpers.user_map.DbManager.create_record") as create,
            patch("helpers.user_map.get_workspace_by_id", return_value=SimpleNamespace(team_id="TDEST")),
            patch("helpers.export_import.invalidate_home_tab_caches_for_team") as invalidate,
        ):
            uid = ensure_mapped_target_user_id(
                "U_SRC",
                1,
                2,
                source_client=MagicMock(),
                target_client=target_client,
            )

        assert uid == "U_DEST"
        source_slack.assert_not_called()
        target_client.users_lookupByEmail.assert_not_called()
        create.assert_called_once()
        row = create.call_args.args[0]
        assert row.target_user_id == "U_DEST"
        assert row.map_method == "email"
        invalidate.assert_called_once_with("TDEST")

    def test_directory_miss_uses_one_lookup_by_email(self):
        target_client = MagicMock()
        target_client.users_lookupByEmail.return_value = {"user": {"id": "U_LOOKED"}}
        with (
            patch("helpers.user_map._mapping_row_for_pair", return_value=None),
            patch(
                "helpers.user_map._source_profile_from_directory",
                return_value={"email": "a@ex.com", "display_name": "A", "real_name": "A"},
            ),
            patch("helpers.user_map.DbManager.find_records", return_value=[]),
            patch("helpers.user_map.DbManager.create_record") as create,
            patch("helpers.user_map.get_workspace_by_id", return_value=SimpleNamespace(team_id="T2")),
            patch("helpers.export_import.invalidate_home_tab_caches_for_team"),
        ):
            uid = ensure_mapped_target_user_id("U_SRC", 1, 2, target_client=target_client)

        assert uid == "U_LOOKED"
        target_client.users_lookupByEmail.assert_called_once_with(email="a@ex.com")
        create.assert_called_once()
        assert create.call_args.args[0].map_method == "email"

    def test_ambiguous_target_email_persists_none_stub(self):
        a = SimpleNamespace(
            slack_user_id="U1", email="dup@ex.com", real_name="A", display_name="A", normalized_name="A"
        )
        b = SimpleNamespace(
            slack_user_id="U2", email="dup@ex.com", real_name="B", display_name="B", normalized_name="B"
        )
        target_client = MagicMock()
        with (
            patch("helpers.user_map._mapping_row_for_pair", return_value=None),
            patch(
                "helpers.user_map._source_profile_from_directory",
                return_value={"email": "dup@ex.com", "display_name": "X", "real_name": "X"},
            ),
            patch("helpers.user_map.DbManager.find_records", return_value=[a, b]),
            patch("helpers.user_map.DbManager.create_record") as create,
            patch("helpers.user_map.DbManager.update_records") as update,
        ):
            uid = ensure_mapped_target_user_id("U_SRC", 1, 2, target_client=target_client)

        assert uid is None
        create.assert_called_once()
        row = create.call_args.args[0]
        assert row.map_method == "none"
        assert row.target_user_id is None
        update.assert_not_called()
        target_client.users_lookupByEmail.assert_not_called()

    def test_lookup_miss_persists_none_stub(self):
        target_client = MagicMock()
        with (
            patch("helpers.user_map._mapping_row_for_pair", return_value=None),
            patch(
                "helpers.user_map._source_profile_from_directory",
                return_value={"email": "gone@ex.com", "display_name": "G", "real_name": "G"},
            ),
            patch("helpers.user_map.DbManager.find_records", return_value=[]),
            patch("helpers.user_map._lookup_user_by_email", return_value=None),
            patch("helpers.user_map.DbManager.create_record") as create,
        ):
            uid = ensure_mapped_target_user_id("U_SRC", 1, 2, target_client=target_client)

        assert uid is None
        create.assert_called_once()
        assert create.call_args.args[0].map_method == "none"

    def test_already_mapped_skips_slack_and_write(self):
        source_client = MagicMock()
        target_client = MagicMock()
        existing = SimpleNamespace(
            map_method="email",
            target_user_id="U_EXISTING",
            mapped_at=datetime.now(UTC),
        )
        with (
            patch("helpers.user_map._mapping_row_for_pair", return_value=existing),
            patch("helpers.user_map._source_profile_from_directory") as profile,
            patch("helpers.user_map.DbManager.create_record") as create,
            patch("helpers.user_map.DbManager.update_records") as update,
        ):
            uid = ensure_mapped_target_user_id(
                "U_SRC",
                1,
                2,
                source_client=source_client,
                target_client=target_client,
            )

        assert uid == "U_EXISTING"
        profile.assert_not_called()
        create.assert_not_called()
        update.assert_not_called()
        source_client.users_info.assert_not_called()
        target_client.users_lookupByEmail.assert_not_called()

    def test_failure_returns_none_without_raising(self):
        with (
            patch("helpers.user_map._mapping_row_for_pair", return_value=None),
            patch(
                "helpers.user_map._source_profile_from_directory",
                side_effect=RuntimeError("boom"),
            ),
        ):
            uid = ensure_mapped_target_user_id("U_SRC", 1, 2, target_client=MagicMock())
        assert uid is None

    def test_home_invalidate_failure_still_returns_mapped_id(self):
        target = SimpleNamespace(
            slack_user_id="U_DEST",
            email="same@ex.com",
            real_name="Ada",
            display_name="Ada",
            normalized_name="Ada",
        )
        with (
            patch("helpers.user_map._mapping_row_for_pair", return_value=None),
            patch(
                "helpers.user_map._source_profile_from_directory",
                return_value={"email": "same@ex.com", "display_name": "Ada", "real_name": "Ada"},
            ),
            patch("helpers.user_map.DbManager.find_records", return_value=[target]),
            patch("helpers.user_map.DbManager.create_record"),
            patch("helpers.user_map.get_workspace_by_id", side_effect=RuntimeError("db down")),
        ):
            uid = ensure_mapped_target_user_id("U_SRC", 1, 2, target_client=MagicMock())
        assert uid == "U_DEST"


class TestSyncedMessageDisplayName:
    def test_mapped_author_uses_target_display_name(self):
        target_client = MagicMock()
        with (
            patch("helpers.user_map.ensure_mapped_target_user_id", return_value="U_DEST"),
            patch("helpers.user_map.get_user_info", return_value=("Local Nacho", "https://target/n.png")),
        ):
            name, icon, mapped, mapped_id = get_display_name_and_icon_for_synced_message(
                "U_SRC",
                1,
                "Remote Alice",
                "https://src/a.png",
                target_client,
                2,
            )
        assert mapped is True
        assert name == "Local Nacho"
        assert icon == "https://target/n.png"
        assert mapped_id == "U_DEST"

    def test_mapped_author_stays_mapped_if_target_profile_missing(self):
        target_client = MagicMock()
        with (
            patch("helpers.user_map.ensure_mapped_target_user_id", return_value="U_DEST"),
            patch("helpers.user_map.get_user_info", return_value=(None, None)),
            patch("helpers.user_map._source_profile_from_directory", return_value=None),
        ):
            name, _icon, mapped, mapped_id = get_display_name_and_icon_for_synced_message(
                "U_SRC",
                1,
                "Remote Alice",
                None,
                target_client,
                2,
            )
        assert mapped is True
        assert name == "Remote Alice"
        assert mapped_id == "U_DEST"

    def test_display_name_is_not_normalized_for_sync(self):
        target_client = MagicMock()
        with patch("helpers.user_map.ensure_mapped_target_user_id", return_value=None):
            name, _icon, mapped, mapped_id = get_display_name_and_icon_for_synced_message(
                "U_SRC",
                1,
                "John Smith (Admin)",
                None,
                target_client,
                2,
            )
        assert mapped is False
        assert mapped_id is None
        assert name == "John Smith (Admin)"


class TestAuthorBeforeMentions:
    def test_same_instance_target_maps_author_before_mention_rewrite(self):
        from helpers.slack_write import slack_write_create

        order: list[str] = []

        def _display(*_a, **_k):
            order.append("author")
            return ("Ada", None, True, "U_MAP")

        def _mentions(text, *_a, **_k):
            order.append("mentions")
            return text

        ctx = make_event_context(
            msg_text="hi <@U_SRC>",
            mentioned_users=[{"user_id": "U_SRC"}],
            user_id="U_SRC",
            reply_broadcast=False,
        )
        with (
            patch("helpers.slack_write.decrypt_bot_token", return_value="xoxb"),
            patch("helpers.slack_write.WebClient"),
            patch("helpers.slack_write.get_display_name_and_icon_for_synced_message", side_effect=_display),
            patch("helpers.slack_write.apply_mentioned_users", side_effect=_mentions),
            patch("helpers.slack_write.resolve_channel_references", side_effect=lambda t, *_a, **_k: t),
            patch("helpers.workspace.get_workspace_by_id", return_value=None),
            patch("helpers.slack_write.post_message", return_value={"ts": "2.0"}),
        ):
            slack_write_create(
                envelope={
                    "source_workspace_id": 1,
                    "source_user_id": "U_SRC",
                    "user_name": "Ada",
                    "workspace_name": "A",
                    "text": ctx["msg_text"],
                },
                sync_channel=SimpleNamespace(channel_id="C_TGT", id=2),
                workspace=SimpleNamespace(id=2, team_id="T2", bot_token="enc"),
                source_client=MagicMock(),
            )
        assert order == ["author", "mentions"]
