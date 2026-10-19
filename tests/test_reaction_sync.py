"""Tests for per-channel reaction type, pairing, and apply helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from slack_sdk.errors import SlackApiError

import constants
from helpers.reaction import (
    apply_reaction_to_target,
    default_reaction_style_for_new_channel,
    get_reaction_style,
)
from slack import actions


def _sync_channel(
    style: str | None = None, *, channel_id: str = "C_SRC", publishes: bool = True, subscribes: bool = True
):
    return SimpleNamespace(
        reaction_style=style,
        publishes=publishes,
        subscribes=subscribes,
        channel_id=channel_id,
        id=1,
    )


def _apply(**kwargs):
    defaults = dict(
        action="add",
        reaction="thumbsup",
        source_user_id="U_SRC",
        source_workspace_id=1,
        source_sync_channel=_sync_channel(channel_id="C_SRC"),
        target_sync_channel=_sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT"),
        target_post_meta=SimpleNamespace(ts=100.0, post_id="post-parent"),
        target_workspace=SimpleNamespace(id=2, team_id="T_TGT"),
        display_name="Ada Lovelace",
        icon_url=None,
        posted_from="(A)",
        author_is_mapped=True,
    )
    defaults.update(kwargs)
    return apply_reaction_to_target(**defaults)


class TestDefaults:
    def test_new_subscribe_defaults_to_hybrid(self):
        from handlers.channel_sync import _reaction_style_block

        assert default_reaction_style_for_new_channel(subscribes=True) == (constants.REACTION_STYLE_THREADED_AND_DIRECT)
        assert default_reaction_style_for_new_channel(subscribes=False) is None
        options = _reaction_style_block(actions.CONFIG_SYNC_REACTION_STYLE).element.options
        assert [o.value for o in options] == [
            constants.REACTION_STYLE_THREADED_AND_DIRECT,
            constants.REACTION_STYLE_DIRECT_ONLY,
            constants.REACTION_STYLE_OFF,
        ]
        assert options[0].name.startswith("Hybrid")
        assert options[2].name.startswith("Off")

    def test_existing_null_style_is_hybrid_when_receiving(self):
        existing = _sync_channel(None)
        assert get_reaction_style(existing) == constants.DEFAULT_REACTION_STYLE_EXISTING

    def test_stored_off_is_not_coerced_to_hybrid(self):
        existing = _sync_channel(constants.REACTION_STYLE_OFF)
        assert get_reaction_style(existing) == constants.REACTION_STYLE_OFF


class TestSkipOrigin:
    def test_sync_does_not_apply_on_origin_channel(self):
        from handlers.reaction_event import _sync_reaction_records

        source = _sync_channel(channel_id="C_SRC")
        source.sync_id = 9
        target = _sync_channel(channel_id="C_TGT")
        source_ws = SimpleNamespace(id=1, team_id="T1")
        target_ws = SimpleNamespace(id=2, team_id="T2")
        origin_meta = SimpleNamespace(ts=1.0, post_id="p1", source_workspace_id=1)
        target_meta = SimpleNamespace(ts=2.0, post_id="p2", source_workspace_id=1)
        body = {
            "event": {
                "type": "reaction_added",
                "reaction": "thumbsup",
                "user": "U_SRC",
                "item": {"channel": "C_SRC"},
                "event_ts": "111.000002",
            }
        }
        records = [
            (origin_meta, source, source_ws),
            (target_meta, target, target_ws),
        ]

        with (
            patch("handlers.reaction_event.helpers.get_user_info", return_value=("Ada Lovelace", None)),
            patch("handlers.reaction_event.helpers.resolve_workspace_name", return_value="A"),
            patch("handlers.reaction_event.helpers.run_sync_pipeline", return_value=[]) as pipeline,
        ):
            _sync_reaction_records(body, MagicMock(), records)

        assert pipeline.call_args.kwargs["source_channel_id"] == "C_SRC"
        assert pipeline.call_args.kwargs["source_sync_channel"] is source
        envelope = pipeline.call_args.args[0]
        assert envelope["post_id"] == "p1"
        assert envelope["source_sync_channel_id"] == source.id
        assert envelope["people"][0]["user_id"] == "U_SRC"
        assert envelope["event_ts"] == "111.000002"
        assert "thread_post_id" not in envelope

    def test_publishing_copy_originates_reaction(self):
        from handlers.reaction_event import _sync_reaction_records

        target = _sync_channel(channel_id="C_TGT")
        target.sync_id = 9
        target_ws = SimpleNamespace(id=2, team_id="T2")
        copy_meta = SimpleNamespace(
            ts=2.0,
            post_id="p1",
            posted_as_user_id="U_TGT",
            source_workspace_id=1,
            kind="message",
        )
        body = {
            "event": {
                "type": "reaction_added",
                "reaction": "thumbsup",
                "user": "U_TGT",
                "item": {"channel": "C_TGT"},
            }
        }

        with (
            patch("handlers.reaction_event.helpers.get_user_info", return_value=("Ada Lovelace", None)),
            patch("handlers.reaction_event.helpers.resolve_workspace_name", return_value="B"),
            patch("handlers.reaction_event.helpers.run_sync_pipeline", return_value=[]) as pipeline,
        ):
            _sync_reaction_records(body, MagicMock(), [(copy_meta, target, target_ws)])

        assert pipeline.call_args.kwargs["source_channel_id"] == "C_TGT"
        assert pipeline.call_args.kwargs["source_sync_channel"] is target
        assert pipeline.call_args.args[0]["post_id"] == "p1"


class TestHandleReactionClaim:
    def test_empty_post_records_releases_claim(self):
        from handlers.reaction_event import handle_reaction

        body = {
            "event_id": "Ev1",
            "team_id": "T1",
            "event": {
                "type": "reaction_added",
                "reaction": "eyes",
                "user": "U1",
                "item": {"type": "message", "channel": "C1", "ts": "1.000000"},
            },
        }
        captured = {}

        def _run(_body, fn):
            captured["result"] = fn()

        with (
            patch("handlers.reaction_event.helpers.get_own_bot_user_id", return_value="B1"),
            patch("handlers.reaction_event.helpers.get_team_id_from_body", return_value="T1"),
            patch("handlers.reaction_event.helpers.take_user_action_echo", return_value=False),
            patch("handlers.reaction_event.helpers.channel_has_membership", return_value=True),
            patch("handlers.reaction_event.helpers.origin_publishes_anywhere", return_value=True),
            patch("handlers.reaction_event.helpers.get_post_records", return_value=[]),
            patch("handlers.reaction_event.log_debug") as log_debug,
            patch("handlers.reaction_event.run_claimed", side_effect=_run),
        ):
            handle_reaction(body, MagicMock(), MagicMock(), {})

        assert captured["result"] is False
        log_debug.assert_called_once()
        assert log_debug.call_args.args[0] == "reaction_no_post_meta"


class TestApplyOff:
    def test_add_skips_without_looking_up_a_user_token(self):
        target = _sync_channel(constants.REACTION_STYLE_OFF, channel_id="C_TGT")
        with (
            patch("helpers.reaction.get_user_token") as get_token,
            patch("helpers.reaction._mapped_user_for_target") as mapped,
            patch("helpers.reaction.delete_notices_for_unreact") as leftover,
        ):
            result, notice = _apply(target_sync_channel=target)

        assert result == "skipped"
        assert notice is None
        get_token.assert_not_called()
        mapped.assert_not_called()
        leftover.assert_not_called()

    def test_unreact_is_noop(self):
        target = _sync_channel(constants.REACTION_STYLE_OFF, channel_id="C_TGT")
        with (
            patch("helpers.reaction.get_user_token") as get_token,
            patch("helpers.reaction._mapped_user_for_target") as mapped,
            patch("helpers.reaction.delete_notices_for_unreact") as leftover,
        ):
            result, notice = _apply(action="remove", target_sync_channel=target)

        assert result == "skipped"
        assert notice is None
        get_token.assert_not_called()
        mapped.assert_not_called()
        leftover.assert_not_called()


class TestApplyDirect:
    def test_uses_target_team_for_user_token(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test") as get_token,
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token") as get_bot_token,
            patch("helpers.reaction.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        get_token.assert_called_once_with("T_TGT", "U_MAPPED")
        get_bot_token.assert_not_called()
        user_client.reactions_add.assert_called_once_with(
            channel="C_TGT",
            timestamp="100.000000",
            name="thumbsup",
        )
        user_client.reactions_remove.assert_not_called()
        assert result == "direct"
        assert notice is None

    def test_mapped_user_id_skips_target_lookup(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test") as get_token,
            patch("helpers.reaction._mapped_user_for_target") as mapped_lookup,
            patch("helpers.reaction.get_bot_token") as get_bot_token,
            patch("helpers.reaction.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
                mapped_user_id="U_PASSED",
            )

        mapped_lookup.assert_not_called()
        get_token.assert_called_once_with("T_TGT", "U_PASSED")
        get_bot_token.assert_not_called()
        assert result == "direct"
        assert notice is None

    def test_direct_only_skips_invalid_name(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "invalid_name"})

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test") as get_token,
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token") as get_bot_token,
            patch("helpers.reaction.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                reaction="custom_emoji",
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        get_token.assert_called_once_with("T_TGT", "U_MAPPED")
        get_bot_token.assert_not_called()
        user_client.chat_postMessage.assert_not_called()

    def test_direct_only_no_token_skips_without_probe(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)

        with (
            patch("helpers.reaction.get_user_token", return_value=None),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token") as get_bot_token,
            patch("helpers.reaction.WebClient") as web_client,
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        get_bot_token.assert_not_called()
        web_client.assert_not_called()

    def test_direct_remove_reactions_remove_only(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        user_client = MagicMock()

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test"),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.WebClient", return_value=user_client),
            patch("helpers.reaction.delete_notices_for_unreact") as delete_notices,
        ):
            result, notice = _apply(
                action="remove",
                source_sync_channel=source,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "direct"
        assert notice is None
        user_client.reactions_remove.assert_called_once()
        user_client.chat_delete.assert_not_called()
        user_client.chat_postMessage.assert_not_called()
        delete_notices.assert_called_once()

    def test_direct_only_auth_error_skips_without_probe(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "token_revoked"})

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test"),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token") as get_bot_token,
            patch("helpers.reaction.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        get_bot_token.assert_not_called()
        user_client.chat_postMessage.assert_not_called()

    def test_hybrid_with_token_does_not_probe(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test"),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token") as get_bot_token,
            patch("helpers.reaction.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "direct"
        assert notice is None
        get_bot_token.assert_not_called()
        user_client.reactions_add.assert_called_once()
        user_client.reactions_remove.assert_not_called()

    def test_hybrid_invalid_name_skips(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "invalid_name"})

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test"),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token") as get_bot_token,
            patch("helpers.reaction.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                reaction="custom_emoji",
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        get_bot_token.assert_not_called()
        user_client.chat_postMessage.assert_not_called()

    def test_hybrid_already_reacted_is_direct_no_thread(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "already_reacted"})

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test"),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token") as get_bot_token,
            patch("helpers.reaction.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "direct"
        assert notice is None
        get_bot_token.assert_not_called()
        user_client.chat_postMessage.assert_not_called()

    def test_hybrid_unknown_error_does_not_thread(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "message_not_found"})

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test"),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token") as get_bot_token,
            patch("helpers.reaction.WebClient", return_value=user_client),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "failed"
        assert notice is None
        get_bot_token.assert_not_called()
        user_client.chat_postMessage.assert_not_called()

    def test_hybrid_notice_uses_thread_root_ts(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts="20.000002", post_id="reply-post")
        bot_client = MagicMock()
        bot_client.chat_getPermalink.return_value = {"permalink": "https://workspace-a.slack.com/archives/C_TGT/p20"}
        bot_client.chat_postMessage.return_value = {"ts": "200.000001"}
        bot_client.conversations_replies.return_value = {
            "messages": [{"ts": "20.000002", "thread_ts": "10.000001"}],
        }

        with (
            patch("helpers.reaction.get_user_token", return_value=None),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.WebClient", return_value=bot_client),
            patch("helpers.reaction.get_bot_token", return_value="xoxb-bot"),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "thread"
        assert notice is not None
        assert bot_client.chat_postMessage.call_args.kwargs["thread_ts"] == "10.000001"
        assert bot_client.chat_getPermalink.call_args.kwargs["message_ts"] == "20.000002"
        bot_client.conversations_replies.assert_called_once()
        assert bot_client.conversations_replies.call_args.kwargs["ts"] == "20.000002"

    def test_hybrid_no_token_threads(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        bot_client = MagicMock()
        bot_client.chat_getPermalink.return_value = {"permalink": "https://example/msg"}
        bot_client.chat_postMessage.return_value = {"ts": "200.000001"}

        with (
            patch("helpers.reaction.get_user_token", return_value=None),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.WebClient", return_value=bot_client),
            patch("helpers.reaction.get_bot_token", return_value="xoxb-bot"),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "thread"
        assert notice is not None
        bot_client.reactions_add.assert_called_once()
        bot_client.reactions_remove.assert_called_once()
        bot_client.chat_postMessage.assert_called_once()

    def test_hybrid_notice_uses_mapped_target_display_name(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0, post_id="post-parent")
        bot_client = MagicMock()
        bot_client.chat_getPermalink.return_value = {"permalink": "https://example/msg"}
        bot_client.chat_postMessage.return_value = {"ts": "200.000001"}

        with (
            patch("helpers.reaction.get_user_token", return_value=None),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_user_info", return_value=("Ada", "https://example/icon.png")),
            patch("helpers.reaction.WebClient", return_value=bot_client),
            patch("helpers.reaction.get_bot_token", return_value="xoxb-bot"),
        ):
            result, notice = _apply(
                display_name="Ada Lovelace",
                posted_from="(Workspace A)",
                author_is_mapped=False,
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "thread"
        assert notice is not None
        assert bot_client.chat_postMessage.call_args.kwargs["username"] == "Ada"
        assert bot_client.chat_postMessage.call_args.kwargs["icon_url"] == "https://example/icon.png"

    def test_hybrid_notice_keeps_source_name_when_unmapped(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0, post_id="post-parent")
        bot_client = MagicMock()
        bot_client.chat_getPermalink.return_value = {"permalink": "https://example/msg"}
        bot_client.chat_postMessage.return_value = {"ts": "200.000001"}

        with (
            patch("helpers.reaction.get_user_token", return_value=None),
            patch("helpers.reaction._mapped_user_for_target", return_value=None),
            patch("helpers.reaction.WebClient", return_value=bot_client),
            patch("helpers.reaction.get_bot_token", return_value="xoxb-bot"),
        ):
            result, notice = _apply(
                display_name="Ada Lovelace",
                posted_from="(Workspace A)",
                author_is_mapped=False,
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "thread"
        assert notice is not None
        assert bot_client.chat_postMessage.call_args.kwargs["username"] == "Ada Lovelace (Workspace A)"

    def test_hybrid_no_token_invalid_name_skips(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        bot_client = MagicMock()
        bot_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "invalid_name"})

        with (
            patch("helpers.reaction.get_user_token", return_value=None),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.WebClient", return_value=bot_client),
            patch("helpers.reaction.get_bot_token", return_value="xoxb-bot"),
        ):
            result, notice = _apply(
                reaction="custom_emoji",
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        bot_client.chat_postMessage.assert_not_called()

    def test_hybrid_probe_unsettled_does_not_thread(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        bot_client = MagicMock()
        bot_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "message_not_found"})

        with (
            patch("helpers.reaction.get_user_token", return_value=None),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.WebClient", return_value=bot_client),
            patch("helpers.reaction.get_bot_token", return_value="xoxb-bot"),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        bot_client.chat_postMessage.assert_not_called()

    def test_hybrid_auth_error_threads(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "token_revoked"})
        bot_client = MagicMock()
        bot_client.chat_getPermalink.return_value = {"permalink": "https://example/msg"}
        bot_client.chat_postMessage.return_value = {"ts": "200.000001"}

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test"),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token", return_value="xoxb-bot"),
            patch("helpers.reaction.WebClient", side_effect=[user_client, bot_client]),
        ):
            result, notice = _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "thread"
        assert notice is not None
        user_client.reactions_add.assert_called_once()
        bot_client.reactions_add.assert_called_once()
        bot_client.reactions_remove.assert_called_once()
        bot_client.chat_postMessage.assert_called_once()

    def test_hybrid_auth_error_invalid_name_skips(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "invalid_auth"})
        bot_client = MagicMock()
        bot_client.reactions_add.side_effect = SlackApiError("bad", response={"error": "invalid_name"})

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test"),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token", return_value="xoxb-bot"),
            patch("helpers.reaction.WebClient", side_effect=[user_client, bot_client]),
        ):
            result, notice = _apply(
                reaction="custom_emoji",
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        bot_client.chat_postMessage.assert_not_called()

    def test_successful_native_reaction_remembers_echo(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test"),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token") as get_bot_token,
            patch("helpers.reaction.WebClient", return_value=user_client),
            patch("helpers.reaction.remember_user_action") as remember_mock,
        ):
            _apply(
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        get_bot_token.assert_not_called()
        remember_mock.assert_called_once_with(
            "T_TGT",
            "U_MAPPED",
            "reaction_added",
            "C_TGT:100.000000:thumbsup",
        )

    def test_reaction_removed_never_threads(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        post_meta = SimpleNamespace(ts=100.0)
        user_client = MagicMock()
        user_client.reactions_remove.side_effect = SlackApiError("bad", response={"error": "invalid_name"})

        with (
            patch("helpers.reaction.get_user_token", return_value="xoxp-test"),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.WebClient", return_value=user_client),
            patch("helpers.reaction.get_bot_token", return_value="xoxb-bot"),
            patch("helpers.reaction.delete_notices_for_unreact") as delete_notices,
        ):
            result, notice = _apply(
                action="remove",
                source_sync_channel=source,
                target_post_meta=post_meta,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert notice is None
        user_client.reactions_remove.assert_called_once()
        delete_notices.assert_called_once()
        user_client.chat_postMessage.assert_not_called()

    def test_hybrid_unreact_chat_deletes_notice_ts(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        notice = SimpleNamespace(
            id=9,
            post_id="rxn-a",
            ts=200.000001,
            source_workspace_id=1,
            source_user_id="U_SRC",
        )
        bot_client = MagicMock()

        with (
            patch("helpers.reaction.get_user_token", return_value=None),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token", return_value="xoxb-bot"),
            patch("helpers.reaction.WebClient", return_value=bot_client),
            patch("helpers.reaction_notice.equivalent_actor_pairs", return_value={(1, "U_SRC")}),
            patch("helpers.reaction_notice.get_notices_for_unreact", return_value=[notice]),
            patch("helpers.reaction_notice._child_notices_on_channel", return_value=[]),
            patch("helpers.reaction_notice.DbManager.delete_records"),
        ):
            result, posted = _apply(
                action="remove",
                source_sync_channel=source,
                target_sync_channel=target,
                target_workspace=workspace,
            )

        assert result == "skipped"
        assert posted is None
        bot_client.chat_delete.assert_called()
        bot_client.chat_postMessage.assert_not_called()

    def test_name_probe_runs_once_per_workspace_when_cached(self):
        source = _sync_channel()
        target = _sync_channel(constants.REACTION_STYLE_THREADED_AND_DIRECT, channel_id="C_TGT")
        workspace = SimpleNamespace(id=2, team_id="T_TGT")
        bot_client = MagicMock()
        bot_client.chat_getPermalink.return_value = {"permalink": "https://example/msg"}
        bot_client.chat_postMessage.side_effect = [{"ts": "200.000001"}, {"ts": "200.000002"}]
        cache: dict = {}

        with (
            patch("helpers.reaction.get_user_token", return_value=None),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.get_bot_token", return_value="xoxb-bot"),
            patch("helpers.reaction.WebClient", return_value=bot_client),
        ):
            _apply(
                source_sync_channel=source,
                target_post_meta=SimpleNamespace(ts=100.0),
                target_sync_channel=target,
                target_workspace=workspace,
                name_probe_cache=cache,
            )
            _apply(
                source_sync_channel=source,
                target_post_meta=SimpleNamespace(ts=101.0),
                target_sync_channel=target,
                target_workspace=workspace,
                name_probe_cache=cache,
            )

        assert bot_client.reactions_add.call_count == 1
        assert bot_client.chat_postMessage.call_count == 2


class TestCreateJoinBuilders:
    def test_create_sync_modal_always_shows_reaction_type(self):
        from handlers.channel_sync import _build_create_sync_blocks

        with (
            patch("handlers.channel_sync.helpers.allow_private_channels", return_value=False),
            patch("handlers.channel_sync._group_name", return_value="Shared"),
        ):
            blocks = _build_create_sync_blocks(team_id="T1", group_id=5)

        ids = [getattr(block, "action", None) for block in blocks]
        texts = [getattr(getattr(block, "element", None), "initial_value", None) for block in blocks]
        assert actions.CONFIG_SYNC_PARTICIPATION in ids
        assert actions.CONFIG_CREATE_SYNC_SELECT in ids
        assert actions.CONFIG_SYNC_REACTION_STYLE in ids
        participation = next(
            block for block in blocks if getattr(block, "action", None) == actions.CONFIG_SYNC_PARTICIPATION
        )
        assert [opt.value for opt in participation.element.options] == ["publish_only", "publish_and_subscribe"]
        assert any(text and "Shared" in text for text in texts)

    def test_parse_reaction_fields_defaults_to_hybrid(self):
        from handlers.channel_sync import _parse_reaction_fields

        style = _parse_reaction_fields(
            {"view": {"state": {"values": {}}}},
            style_action=actions.CONFIG_SYNC_REACTION_STYLE,
        )
        assert style == constants.DEFAULT_REACTION_STYLE_NEW_RECEIVE


class TestApplyLastWriteWins:
    def test_stale_event_ts_skips_without_slack(self):
        target = _sync_channel(constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_TGT")
        with (
            patch("helpers.reaction.reaction_event_ts_is_stale", return_value=True),
            patch("helpers.reaction.get_user_token") as get_token,
            patch("helpers.reaction.remember_reaction_event_ts") as remember,
        ):
            result, notice = _apply(target_sync_channel=target, event_ts="1.000001")

        assert result == "skipped"
        assert notice is None
        get_token.assert_not_called()
        remember.assert_not_called()

    def test_newer_remove_applies_after_older_add(self):
        target = _sync_channel(constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_TGT")
        user_client = MagicMock()
        with (
            patch("helpers.reaction.reaction_event_ts_is_stale", return_value=False),
            patch("helpers.reaction.get_user_token", return_value="xoxp-test"),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.WebClient", return_value=user_client),
            patch("helpers.reaction.remember_user_action"),
            patch("helpers.reaction.remember_reaction_event_ts") as remember,
            patch("helpers.reaction.delete_notices_for_unreact"),
        ):
            result, notice = _apply(
                action="remove",
                target_sync_channel=target,
                event_ts="2.000000",
            )

        assert result == "direct"
        assert notice is None
        user_client.reactions_remove.assert_called_once()
        remember.assert_called_once()
        assert remember.call_args.args[-1] == "2.000000"

    def test_later_readd_still_applies(self):
        target = _sync_channel(constants.REACTION_STYLE_DIRECT_ONLY, channel_id="C_TGT")
        user_client = MagicMock()
        with (
            patch("helpers.reaction.reaction_event_ts_is_stale", return_value=False),
            patch("helpers.reaction.get_user_token", return_value="xoxp-test"),
            patch("helpers.reaction._mapped_user_for_target", return_value="U_MAPPED"),
            patch("helpers.reaction.WebClient", return_value=user_client),
            patch("helpers.reaction.remember_user_action"),
            patch("helpers.reaction.remember_reaction_event_ts") as remember,
        ):
            result, _notice = _apply(target_sync_channel=target, event_ts="3.000000")

        assert result == "direct"
        user_client.reactions_add.assert_called_once()
        remember.assert_called_once()
