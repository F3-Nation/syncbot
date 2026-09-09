"""Tests for Hybrid reaction notice helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import constants
from helpers.reaction_notice import (
    equivalent_actor_pairs,
    reaction_notice_post_id,
)


def test_reaction_notice_post_id_stable():
    a = reaction_notice_post_id(
        parent_post_id="post-1",
        reaction="thumbsup",
        source_user_id="U_A",
        source_workspace_id=1,
    )
    b = reaction_notice_post_id(
        parent_post_id="post-1",
        reaction="thumbsup",
        source_user_id="U_A",
        source_workspace_id=1,
    )
    assert a == b
    assert a.startswith("rxn-")


def test_reaction_notice_post_id_federation_actor_key():
    fed = reaction_notice_post_id(
        parent_post_id="post-1",
        reaction="heart",
        source_user_id="U_REMOTE",
        source_workspace_id=None,
        federated_instance_id="inst-abc",
    )
    assert fed.startswith("rxn-")


def test_equivalent_actor_pairs_includes_event_and_reverse():
    with patch("helpers.reaction_notice.DbManager.find_records") as find:
        find.side_effect = [
            [],
            [
                SimpleNamespace(source_workspace_id=1, source_user_id="U_A"),
            ],
        ]
        pairs = equivalent_actor_pairs(2, "U_B")
    assert (2, "U_B") in pairs
    assert (1, "U_A") in pairs


def test_same_channel_apply_skipped():
    from helpers.reaction import apply_reaction_to_target

    channel = SimpleNamespace(
        reaction_direction=constants.REACTION_DIRECTION_BOTH,
        reaction_style=constants.REACTION_STYLE_THREADED_AND_DIRECT,
        channel_id="C_SAME",
        id=1,
    )
    workspace = SimpleNamespace(id=1, team_id="T1", bot_token="enc")
    with patch("helpers.reaction.WebClient") as web_client:
        result, notice = apply_reaction_to_target(
            action="add",
            reaction="thumbsup",
            source_user_id="U1",
            source_workspace_id=1,
            source_sync_channel=channel,
            target_post_meta=SimpleNamespace(ts=1.0, post_id="p1"),
            target_sync_channel=channel,
            target_workspace=workspace,
            display_name="Alice",
            icon_url=None,
            posted_from="(A)",
            author_is_mapped=True,
        )
    assert result == "skipped"
    assert notice is None
    web_client.assert_not_called()


def test_find_notices_for_unreact_matches_one_actor_only():
    from helpers.reaction_notice import find_notices_for_unreact

    alice = SimpleNamespace(
        source_workspace_id=1,
        source_user_id="U_A",
        post_id="rxn-a",
    )
    bob = SimpleNamespace(
        source_workspace_id=1,
        source_user_id="U_B",
        post_id="rxn-b",
    )
    with patch("helpers.reaction_notice.DbManager.find_records", return_value=[alice, bob]):
        matched = find_notices_for_unreact(
            parent_post_id="post-1",
            reaction="thumbsup",
            sync_channel_id=10,
            actor_pairs={(1, "U_A")},
        )
    assert matched == [alice]


def test_find_notices_for_unreact_matches_federation_null_workspace():
    from helpers.reaction_notice import find_notices_for_unreact

    fed = SimpleNamespace(
        source_workspace_id=None,
        source_user_id="U_REMOTE",
        post_id="rxn-fed",
    )
    other = SimpleNamespace(
        source_workspace_id=None,
        source_user_id="U_OTHER",
        post_id="rxn-other",
    )
    with patch("helpers.reaction_notice.DbManager.find_records", return_value=[fed, other]):
        matched = find_notices_for_unreact(
            parent_post_id="post-1",
            reaction="thumbsup",
            sync_channel_id=10,
            actor_pairs={(55, "U_REMOTE")},
        )
    assert matched == [fed]


def test_reaction_notice_post_id_shared_across_targets_and_child_parent():
    parent = reaction_notice_post_id(
        parent_post_id="post-1",
        reaction="thumbsup",
        source_user_id="U_A",
        source_workspace_id=1,
    )
    target_b = reaction_notice_post_id(
        parent_post_id="post-1",
        reaction="thumbsup",
        source_user_id="U_A",
        source_workspace_id=1,
    )
    assert parent == target_b
    child = reaction_notice_post_id(
        parent_post_id=parent,
        reaction="heart",
        source_user_id="U_B",
        source_workspace_id=2,
    )
    assert child != parent
    assert child.startswith("rxn-")


def test_unreact_deletes_child_then_parent_notice():
    from helpers.reaction_notice import _delete_notice_subtree

    parent = SimpleNamespace(id=1, post_id="rxn-parent", ts=100.0)
    child = SimpleNamespace(id=2, post_id="rxn-child", ts=101.0)
    sync_channel = SimpleNamespace(id=5, channel_id="C1")
    client = MagicMock()
    calls: list[str] = []

    def _children(parent_post_id, sync_channel_id):
        if parent_post_id == "rxn-parent":
            return [child]
        return []

    with (
        patch("helpers.reaction_notice._child_notices_on_channel", side_effect=_children),
        patch("helpers.reaction_notice.chat_delete_notice", side_effect=lambda _c, _ch, ts: calls.append(f"del:{ts}")),
        patch(
            "helpers.reaction_notice._hard_delete_post_meta_rows",
            side_effect=lambda rows: calls.append(f"db:{rows[0].post_id}"),
        ),
    ):
        _delete_notice_subtree(parent, sync_channel=sync_channel, client=client)

    assert calls == ["del:101.0", "db:rxn-child", "del:100.0", "db:rxn-parent"]


def test_notice_tree_depth_cap_stops_recursion():
    from helpers.reaction_notice import _delete_notice_subtree

    notice = SimpleNamespace(id=1, post_id="rxn-deep", ts=100.0)
    sync_channel = SimpleNamespace(id=5, channel_id="C1")
    client = MagicMock()
    with (
        patch("helpers.reaction_notice._child_notices_on_channel") as children,
        patch("helpers.reaction_notice.chat_delete_notice") as chat_delete,
    ):
        _delete_notice_subtree(notice, sync_channel=sync_channel, client=client, depth=constants.NOTICE_TREE_MAX_DEPTH)
    children.assert_not_called()
    chat_delete.assert_not_called()


def test_chat_delete_notice_treats_message_not_found_as_success():
    from slack_sdk.errors import SlackApiError

    from helpers.reaction_notice import chat_delete_notice

    client = MagicMock()
    client.chat_delete.side_effect = SlackApiError("gone", response={"error": "message_not_found"})
    chat_delete_notice(client, "C1", 100.0)


def test_direct_only_unreact_does_not_scan_leftover_threads():
    from helpers.reaction_notice import delete_notices_for_unreact

    sync_channel = SimpleNamespace(
        id=5,
        channel_id="C1",
        reaction_style=constants.REACTION_STYLE_DIRECT_ONLY,
    )
    client = MagicMock()
    with (
        patch("helpers.reaction_notice.equivalent_actor_pairs", return_value={(1, "U_A")}),
        patch("helpers.reaction_notice.find_notices_for_unreact", return_value=[]),
        patch("helpers.reaction_notice._delete_leftover_thread_notices") as leftover,
    ):
        delete_notices_for_unreact(
            parent_post_id="post-1",
            reaction="thumbsup",
            sync_channel=sync_channel,
            event_workspace_id=1,
            event_user_id="U_A",
            client=client,
        )
    leftover.assert_not_called()
    client.conversations_replies.assert_not_called()


def test_leftover_thread_scan_skipped_when_new_style_rows_exist():
    from helpers.reaction_notice import delete_notices_for_unreact

    notice = SimpleNamespace(id=1, post_id="rxn-a", ts=100.0)
    sync_channel = SimpleNamespace(
        id=5,
        channel_id="C1",
        reaction_style=constants.REACTION_STYLE_THREADED_AND_DIRECT,
    )
    client = MagicMock()
    with (
        patch("helpers.reaction_notice.equivalent_actor_pairs", return_value={(1, "U_A")}),
        patch("helpers.reaction_notice.find_notices_for_unreact", return_value=[notice]),
        patch("helpers.reaction_notice._delete_notice_subtree"),
        patch("helpers.reaction_notice._delete_leftover_thread_notices") as leftover,
    ):
        delete_notices_for_unreact(
            parent_post_id="post-1",
            reaction="thumbsup",
            sync_channel=sync_channel,
            event_workspace_id=1,
            event_user_id="U_A",
            client=client,
        )
    leftover.assert_not_called()


def test_leftover_thread_scan_skips_human_emoji_mention():
    from helpers.reaction_notice import _delete_leftover_thread_notices

    parent = SimpleNamespace(post_id="post-1", ts=100.0)
    sync_channel = SimpleNamespace(id=5, channel_id="C1")
    client = MagicMock()
    client.conversations_replies.return_value = {
        "messages": [
            {"ts": "100.000000", "text": "original"},
            {"ts": "101.000000", "user": "U_HUMAN", "text": "I also like :thumbsup: here"},
            {
                "ts": "102.000000",
                "bot_id": "B_SYNCBOT",
                "text": "reacted with :thumbsup: to <https://example|this message>",
            },
            {"ts": "103.000000", "bot_id": "B_SYNCBOT", "text": "channel paused"},
        ]
    }
    with (
        patch("helpers.reaction_notice.DbManager.find_records", return_value=[parent]),
        patch("helpers.reaction_notice.chat_delete_notice") as chat_delete,
    ):
        _delete_leftover_thread_notices(
            parent_post_id="post-1",
            reaction="thumbsup",
            sync_channel=sync_channel,
            client=client,
        )
    chat_delete.assert_called_once()
    assert chat_delete.call_args.args[2] == "102.000000"


def test_looks_like_hybrid_notice_text():
    from helpers.reaction_notice import _looks_like_hybrid_notice_text

    assert _looks_like_hybrid_notice_text("reacted with :thumbsup: to <https://x|this message>", "thumbsup")
    assert _looks_like_hybrid_notice_text("reacted with :heart:", "heart")
    assert not _looks_like_hybrid_notice_text("nice :thumbsup: from me", "thumbsup")


def test_tombstone_reaction_notice_locally_deletes_children_only_on_channel():
    from helpers.reaction_notice import tombstone_reaction_notice_locally

    notice = SimpleNamespace(id=1, post_id="rxn-parent", ts=100.0)
    child = SimpleNamespace(id=2, post_id="rxn-child", ts=101.0)
    sync_channel = SimpleNamespace(id=5, channel_id="C1")
    client = MagicMock()

    with (
        patch("helpers.reaction_notice.DbManager.find_records", return_value=[child]),
        patch("helpers.reaction_notice.DbManager.delete_records") as delete_records,
        patch("helpers.reaction_notice.chat_delete_notice") as chat_delete,
    ):
        tombstone_reaction_notice_locally(
            notice=notice,
            sync_channel=sync_channel,
            client=client,
        )

    chat_delete.assert_called_once_with(client, "C1", 101.0)
    assert delete_records.call_count == 2


def test_same_workspace_hybrid_no_token_skips_probe():
    from helpers.reaction import apply_reaction_to_target

    source = SimpleNamespace(
        reaction_direction=constants.REACTION_DIRECTION_BOTH,
        reaction_style=constants.REACTION_STYLE_THREADED_AND_DIRECT,
        channel_id="C_SRC",
        id=1,
    )
    target = SimpleNamespace(
        reaction_direction=constants.REACTION_DIRECTION_BOTH,
        reaction_style=constants.REACTION_STYLE_THREADED_AND_DIRECT,
        channel_id="C_TGT",
        id=2,
    )
    workspace = SimpleNamespace(id=99, team_id="T1", bot_token="enc")
    bot_client = MagicMock()
    bot_client.chat_getPermalink.return_value = {"permalink": "https://example/msg"}
    bot_client.chat_postMessage.return_value = {"ts": "200.000001"}

    with (
        patch("helpers.reaction.get_user_token", return_value=None),
        patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
        patch("helpers.reaction.decrypt_bot_token", return_value="xoxb-bot"),
        patch("helpers.reaction.WebClient", return_value=bot_client),
        patch("helpers.reaction._target_reaction_name_is_invalid") as probe,
        patch("helpers.reaction.DbManager.create_records"),
    ):
        apply_reaction_to_target(
            action="add",
            reaction="thumbsup",
            source_user_id="U1",
            source_workspace_id=99,
            source_sync_channel=source,
            target_post_meta=SimpleNamespace(ts=100.0, post_id="p1"),
            target_sync_channel=target,
            target_workspace=workspace,
            display_name="Alice",
            icon_url=None,
            posted_from="(A)",
            author_is_mapped=True,
        )

    probe.assert_not_called()
    bot_client.chat_postMessage.assert_called_once()


def test_migration_import_restores_notice_fields():
    from helpers.export_import import import_migration_data

    data = {
        "workspace": {"team_id": "T1"},
        "syncs": [{"title": "S1", "publisher_team_id": "T1", "target_team_id": "T2"}],
        "sync_channels": [{"sync_title": "S1", "channel_id": "C1", "status": "active"}],
        "post_meta": {
            "S1:C1": [
                {
                    "post_id": "rxn-abc",
                    "ts": 100.0,
                    "kind": constants.POST_META_KIND_REACTION_NOTICE,
                    "parent_post_id": "post-1",
                    "reaction": "heart",
                    "source_user_id": "U1",
                    "source_workspace_id": 3,
                }
            ]
        },
        "user_directory": [],
        "user_mappings": [],
    }
    created_post_meta: list = []
    fake_channel = SimpleNamespace(id=99)

    sync_counter = {"n": 0}

    def _capture(record):
        name = type(record).__name__
        if name == "Sync":
            sync_counter["n"] += 1
            record.id = sync_counter["n"]
        if name == "SyncChannel":
            record.id = fake_channel.id
        if name == "PostMeta":
            created_post_meta.append(record)
        return record

    with (
        patch("helpers.export_import.DbManager.find_records", return_value=[]),
        patch("helpers.export_import.DbManager.create_record", side_effect=_capture),
        patch("helpers.export_import.DbManager.delete_records"),
    ):
        import_migration_data(data, workspace_id=1, group_id=1, team_id_to_workspace_id={"T1": 1, "T2": 2})

    assert len(created_post_meta) == 1
    row = created_post_meta[0]
    assert row.kind == constants.POST_META_KIND_REACTION_NOTICE
    assert row.parent_post_id == "post-1"
    assert row.reaction == "heart"


def test_target_notice_message_deleted_is_local_tombstone_only():
    from handlers.message import respond_to_message_event

    body = {
        "team_id": "T1",
        "event": {
            "type": "message",
            "subtype": "message_deleted",
            "channel": "C1",
            "previous_message": {"ts": "200.000001", "bot_id": "B_SYNCBOT", "text": ":thumbsup:"},
        },
    }
    notice = SimpleNamespace(
        kind=constants.POST_META_KIND_REACTION_NOTICE,
        post_id="rxn-a",
        ts=200.000001,
        id=1,
    )
    workspace = SimpleNamespace(id=7, team_id="T1", bot_token="enc")
    sync_channel = SimpleNamespace(id=5, channel_id="C1")
    logger = MagicMock()
    client = MagicMock()

    with (
        patch("handlers.message.helpers.get_workspace_record", return_value=workspace),
        patch("handlers.message.DbManager.find_records", return_value=[sync_channel]),
        patch("helpers.reaction_notice.find_post_meta_by_channel_ts", return_value=notice),
        patch("helpers.reaction_notice.tombstone_reaction_notice_locally") as tombstone,
        patch("handlers.message._handle_message_delete") as handle_delete,
        patch("handlers.message.helpers.decrypt_bot_token", return_value="xoxb-bot"),
        patch("handlers.message.WebClient"),
        patch("handlers.message.helpers.parse_mentioned_users", return_value=[]),
    ):
        respond_to_message_event(body, client, logger, {"bot_id": "B_SYNCBOT"})

    tombstone.assert_called_once()
    handle_delete.assert_not_called()
    client.reactions_remove.assert_not_called()


def test_own_bot_delete_of_missing_notice_does_not_fan_out():
    from handlers.message import respond_to_message_event

    body = {
        "team_id": "T1",
        "event": {
            "type": "message",
            "subtype": "message_deleted",
            "channel": "C1",
            "previous_message": {"ts": "200.000001", "bot_id": "B_SYNCBOT", "text": "synced post"},
        },
    }
    workspace = SimpleNamespace(id=7, team_id="T1", bot_token="enc")
    sync_channel = SimpleNamespace(id=5, channel_id="C1")
    logger = MagicMock()
    client = MagicMock()

    with (
        patch("handlers.message.helpers.get_workspace_record", return_value=workspace),
        patch("handlers.message.DbManager.find_records", return_value=[sync_channel]),
        patch("helpers.reaction_notice.find_post_meta_by_channel_ts", return_value=None),
        patch("handlers.message._handle_message_delete") as handle_delete,
        patch("handlers.message._is_own_bot_message", return_value=True),
        patch("handlers.message.helpers.parse_mentioned_users", return_value=[]),
    ):
        respond_to_message_event(body, client, logger, {"bot_id": "B_SYNCBOT"})

    handle_delete.assert_not_called()
