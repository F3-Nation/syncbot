"""Block Kit extraction for synced messages (bot preblasts, truncated event.text)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from handlers.message import _parse_event_fields
from helpers.message_blocks import (
    choose_message_text,
    content_blocks_for_sync,
    text_from_blocks,
)
from helpers.slack_api import post_message
from helpers.slack_write import slack_write_create
from tests.event_fixtures import make_event_context


def _preblast_blocks():
    return [
        {
            "type": "section",
            "block_id": "src1",
            "text": {
                "type": "mrkdwn",
                "text": "*Preblast: The QT Quest*\n*Date:* 2026-09-19\n*Time:* 05:30\n*Where:* <#CCSAUP>\n*Q:* <@U_SRC>",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*WHAT:* a 14 mile race\n🚨 ARRIVE 10 MINUTES EARLY 🚨\n🌭 React if you're an individual racer",
            },
        },
        {
            "type": "actions",
            "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Edit this preblast"}}],
        },
    ]


class TestChooseMessageText:
    def test_prefers_blocks_when_fallback_lost_newlines(self):
        fallback = (
            "Preblast: The QT Quest Date: 2026-09-19 Time: 05:30 Where: #csaup Q: @Loboto Coupons: gift cards maybe"
        )
        blocks = content_blocks_for_sync(_preblast_blocks())
        chosen = choose_message_text(fallback, blocks)
        assert "\n" in chosen
        assert "🚨" in chosen
        assert "🌭" in chosen
        assert "WHAT" in chosen

    def test_plain_text_unchanged_without_blocks(self):
        assert choose_message_text("Hello world", []) == "Hello world"


class TestContentBlocksForSync:
    def test_drops_actions_and_block_ids(self):
        blocks = content_blocks_for_sync(_preblast_blocks())
        assert all(b.get("type") != "actions" for b in blocks)
        assert all("block_id" not in b for b in blocks)
        assert len(blocks) == 2

    def test_skips_image_blocks_without_public_url(self):
        blocks = content_blocks_for_sync([{"type": "image", "slack_file": {"id": "F123"}, "alt_text": "private"}])
        assert blocks == []


class TestParseEventUsesBlocks:
    def test_bot_message_uses_block_body(self):
        body = {
            "team_id": "T001",
            "event": {
                "type": "message",
                "subtype": "bot_message",
                "channel": "C001",
                "text": "Preblast: The QT Quest Date: 2026-09-19 Time: 05:30",
                "username": "Loboto",
                "blocks": _preblast_blocks(),
                "ts": "1.0",
            },
        }
        client = MagicMock()
        client.users_info.return_value = {
            "user": {"id": "U_SRC", "profile": {"display_name": "Loboto", "real_name": "Loboto"}}
        }
        ctx = _parse_event_fields(body, client)
        assert "\n" in ctx["msg_text"]
        assert "🌭" in ctx["msg_text"]
        assert ctx["content_blocks"]
        assert all(b["type"] != "actions" for b in ctx["content_blocks"])
        client.conversations_history.assert_not_called()

    def test_bot_message_without_blocks_loads_history(self):
        body = {
            "team_id": "T001",
            "event": {
                "type": "message",
                "subtype": "bot_message",
                "bot_id": "B001",
                "channel": "C001",
                "text": "Preblast: The QT Quest Date: 2026-09-19",
                "ts": "1.0",
            },
        }
        client = MagicMock()
        client.conversations_history.return_value = {"messages": [{"text": "full", "blocks": _preblast_blocks()}]}
        client.users_info.return_value = {
            "user": {"id": "U_SRC", "profile": {"display_name": "Loboto", "real_name": "Loboto"}}
        }
        ctx = _parse_event_fields(body, client)
        client.conversations_history.assert_called_once()
        assert "🌭" in ctx["msg_text"]
        assert len(ctx["content_blocks"]) == 2

    def test_message_changed_uses_nested_blocks(self):
        body = {
            "team_id": "T001",
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "hidden": True,
                "channel": "C001",
                "ts": "9.9",
                "message": {
                    "type": "message",
                    "subtype": "bot_message",
                    "bot_id": "B_SLACKBLAST",
                    "text": "Preblast: flattened fallback",
                    "blocks": _preblast_blocks(),
                    "ts": "1.0",
                    "edited": {"user": "U_SRC", "ts": "9.9"},
                },
            },
        }
        client = MagicMock()
        client.users_info.return_value = {
            "user": {"id": "U_SRC", "profile": {"display_name": "Loboto", "real_name": "Loboto"}}
        }
        ctx = _parse_event_fields(body, client)
        assert ctx["event_subtype"] == "message_changed"
        assert ctx["ts"] == "1.0"
        assert "🌭" in ctx["msg_text"]
        assert ctx["content_blocks"]
        client.conversations_history.assert_not_called()

    def test_message_changed_without_blocks_loads_history_by_message_ts(self):
        body = {
            "team_id": "T001",
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C001",
                "ts": "9.9",
                "message": {
                    "subtype": "bot_message",
                    "bot_id": "B_SLACKBLAST",
                    "text": "Preblast: flattened fallback",
                    "ts": "1.0",
                },
            },
        }
        client = MagicMock()
        client.conversations_history.return_value = {"messages": [{"text": "full", "blocks": _preblast_blocks()}]}
        client.users_info.return_value = {
            "user": {"id": "U_SRC", "profile": {"display_name": "Loboto", "real_name": "Loboto"}}
        }
        ctx = _parse_event_fields(body, client)
        kwargs = client.conversations_history.call_args.kwargs
        assert kwargs["latest"] == "1.0"
        assert kwargs["inclusive"] is True
        assert "🌭" in ctx["msg_text"]


class TestHandleMessageEditForwardsLayoutBlocks:
    def test_chat_update_gets_section_blocks(self):
        from handlers.message import _handle_message_edit

        ctx = make_event_context(
            channel_id="C_SRC",
            msg_text=text_from_blocks(content_blocks_for_sync(_preblast_blocks())),
            mentioned_users=[{"user_id": "U_SRC", "user_name": "Loboto"}],
            ts="1.0",
            user_id=None,
            content_blocks=content_blocks_for_sync(_preblast_blocks()),
        )
        post_meta = SimpleNamespace(post_id="p1", ts=1.0, sync_channel_id=1)
        sync_channel = SimpleNamespace(channel_id="C_SRC", id=1, sync_id=1)
        workspace = SimpleNamespace(id=1, team_id="T1", bot_token="enc")
        with (
            patch("handlers.message.helpers.get_post_records", return_value=[(post_meta, sync_channel, workspace)]),
            patch("handlers.message.helpers.find_origin_sync_channel", return_value=sync_channel),
            patch("handlers.message.helpers.resolve_workspace_name", return_value="Source"),
            patch("handlers.message.helpers.run_sync_pipeline", return_value=[]) as pipeline,
        ):
            _handle_message_edit(MagicMock(), MagicMock(), ctx, [])
        envelope = pipeline.call_args.args[0]
        blocks = envelope["blocks"]
        assert envelope["source_sync_channel_id"] == 1
        assert envelope["people"][0]["user_id"] == "U_SRC"
        assert blocks[0]["type"] == "section"
        assert blocks[0]["type"] == "section"
        assert "🌭" in blocks[1]["text"]["text"]


class TestDestPostForwardsLayoutBlocks:
    def test_does_not_flatten_into_a_single_section(self):
        ctx = make_event_context(
            msg_text=text_from_blocks(content_blocks_for_sync(_preblast_blocks())),
            mentioned_users=[{"user_id": "U_SRC", "user_name": "Loboto"}],
            user_id=None,
            reply_broadcast=False,
            content_blocks=content_blocks_for_sync(_preblast_blocks()),
        )
        with (
            patch("helpers.slack_write.decrypt_bot_token", return_value="xoxb"),
            patch("helpers.slack_write.WebClient"),
            patch(
                "helpers.slack_write.get_display_name_and_icon_for_synced_message",
                return_value=("Loboto", "https://icon", False, None),
            ),
            patch("helpers.slack_write.apply_mentioned_users", side_effect=lambda t, *_a, **_k: t),
            patch("helpers.slack_write.resolve_channel_references", side_effect=lambda t, *_a, **_k: t),
            patch("helpers.slack_write.resolve_mention_for_workspace", side_effect=lambda *_a, **_k: "<@U_TGT>"),
            patch("helpers.workspace.get_workspace_by_id", return_value=None),
            patch("helpers.slack_write.post_message", return_value={"ts": "9.0"}) as post,
        ):
            slack_write_create(
                envelope={
                    "source_workspace_id": 1,
                    "user_name": "Loboto",
                    "user_avatar_url": "https://icon",
                    "workspace_name": "F3 Tulsa",
                    "text": ctx["msg_text"],
                    "blocks": ctx["content_blocks"],
                },
                sync_channel=SimpleNamespace(channel_id="C_TGT", id=2),
                workspace=SimpleNamespace(id=2, team_id="T2", bot_token="enc"),
                source_client=MagicMock(),
            )
        blocks = post.call_args.kwargs["blocks"]
        assert len(blocks) == 2
        assert blocks[0]["type"] == "section"
        assert "\n" in blocks[0]["text"]["text"]
        assert "🌭" in blocks[1]["text"]["text"]
        assert "<@U_TGT>" in blocks[0]["text"]["text"]


class TestPostMessageSkipsPrependWhenBodyBlocksPresent:
    def test_section_blocks_are_the_body(self):
        slack = MagicMock()
        slack.chat_postMessage.return_value = {"ts": "1.2"}
        body_blocks = content_blocks_for_sync(_preblast_blocks())
        with patch("helpers.slack_api.WebClient", return_value=slack):
            post_message(
                bot_token="xoxb",
                channel_id="C1",
                msg_text="flattened fallback without newlines",
                blocks=body_blocks,
            )
        sent = slack.chat_postMessage.call_args.kwargs["blocks"]
        assert sent[0]["type"] == "section"
        assert sent[0]["text"]["text"].startswith("*Preblast:")
        assert len(sent) == 2

    def test_image_only_blocks_still_prepend_text(self):
        slack = MagicMock()
        slack.chat_postMessage.return_value = {"ts": "1.2"}
        gif = [{"type": "image", "image_url": "https://example.com/a.gif", "alt_text": "gif"}]
        with patch("helpers.slack_api.WebClient", return_value=slack):
            post_message(bot_token="xoxb", channel_id="C1", msg_text="hello", blocks=gif)
        sent = slack.chat_postMessage.call_args.kwargs["blocks"]
        assert sent[0]["type"] == "section"
        assert sent[0]["text"]["text"] == "hello"
        assert sent[1]["type"] == "image"
        assert slack.chat_postMessage.call_args.kwargs["unfurl_links"] is False
        assert slack.chat_postMessage.call_args.kwargs["unfurl_media"] is False

    def test_chat_update_also_disables_unfurl(self):
        slack = MagicMock()
        slack.chat_update.return_value = {"ts": "2.0"}
        with patch("helpers.slack_api.WebClient", return_value=slack):
            post_message(bot_token="xoxb", channel_id="C1", msg_text="hello", update_ts="2.0")
        assert slack.chat_update.call_args.kwargs["unfurl_links"] is False
        assert slack.chat_update.call_args.kwargs["unfurl_media"] is False


class TestRewriteContentBlocksRichText:
    def test_rich_text_channel_becomes_code_text(self):
        from helpers.message_blocks import rewrite_content_blocks

        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "Where: "},
                            {"type": "channel", "channel_id": "C_SRC"},
                        ],
                    }
                ],
            }
        ]

        def rewrite_mrkdwn(text: str) -> str:
            if text == "<#C_SRC>":
                return "`#ao (Acme)`"
            return text

        out = rewrite_content_blocks(blocks, rewrite_mrkdwn, lambda _u: None, lambda u: f"`{u}`")
        els = out[0]["elements"][0]["elements"]
        assert els[0] == {"type": "text", "text": "Where: "}
        assert els[1] == {"type": "text", "text": "#ao (Acme)", "style": {"code": True}}
        assert els[1].get("channel_id") is None

    def test_section_mrkdwn_uses_code_ticks(self):
        from helpers.message_blocks import rewrite_content_blocks

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Where: <#C_SRC>"}}]
        out = rewrite_content_blocks(
            blocks,
            lambda t: t.replace("<#C_SRC>", "`#ao (Acme)`"),
            lambda _u: None,
            lambda u: u,
        )
        assert out[0]["text"]["text"] == "Where: `#ao (Acme)`"

    def test_rich_text_permalink_link_gets_source_label(self):
        from helpers.message_blocks import rewrite_content_blocks

        url = "https://sprockdevbeta.slack.com/archives/C0APSA79WR4/p1788488496065219"
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "link", "url": url}],
                    }
                ],
            }
        ]

        def rewrite_mrkdwn(text: str) -> str:
            if text == url:
                return f"<{url}|Message in #blackops (Sprock Dev Beta)>"
            return text

        out = rewrite_content_blocks(blocks, rewrite_mrkdwn, lambda _u: None, lambda u: u)
        els = out[0]["elements"][0]["elements"]
        assert els[0] == {
            "type": "link",
            "url": url,
            "text": "Message in #blackops (Sprock Dev Beta)",
        }

    def test_rich_text_user_mapped_and_unmapped(self):
        from helpers.message_blocks import rewrite_content_blocks

        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "user", "user_id": "U_SRC"},
                            {"type": "text", "text": " "},
                            {"type": "user", "user_id": "U_NONE"},
                        ],
                    }
                ],
            }
        ]

        def map_user(uid: str):
            return "U_TGT" if uid == "U_SRC" else None

        out = rewrite_content_blocks(blocks, lambda t: t, map_user, lambda u: f"`{u} (Acme)`")
        els = out[0]["elements"][0]["elements"]
        assert els[0] == {"type": "user", "user_id": "U_TGT"}
        assert els[2] == {"type": "text", "text": "U_NONE (Acme)", "style": {"code": True}}

    def test_rich_text_unmapped_user_uses_code_ticked_display_name(self):
        from helpers.message_blocks import rewrite_content_blocks
        from helpers.user_map import unmapped_author_label

        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "user", "user_id": "U_NONE"},
                            {"type": "text", "text": " what's up?"},
                        ],
                    }
                ],
            }
        ]
        out = rewrite_content_blocks(
            blocks,
            lambda t: t,
            lambda _u: None,
            lambda _u: unmapped_author_label("F3ttown Downrange Q", "F3 T-Town Test"),
        )
        els = out[0]["elements"][0]["elements"]
        assert els[0] == {
            "type": "text",
            "text": "F3ttown Downrange Q (F3 T-Town Test)",
            "style": {"code": True},
        }
        assert els[1] == {"type": "text", "text": " what's up?"}


class TestBlockKitLimits:
    def test_content_blocks_trimmed_to_fifty(self):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"b{i}"}} for i in range(55)]
        out = content_blocks_for_sync(blocks)
        assert len(out) == 50

    def test_section_text_clamped_after_rewrite(self):
        from helpers.message_blocks import rewrite_content_blocks

        long = "x" * 3500
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "short"}}]
        out = rewrite_content_blocks(blocks, lambda _t: long, lambda _u: None, lambda u: u)
        assert len(out[0]["text"]["text"]) == 3000
        assert out[0]["text"]["text"].endswith("…")
