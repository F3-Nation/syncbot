"""Message subtype allowlist, hosted files, and federation payload parity."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from federation.core import build_edit_payload, build_message_payload  # noqa: E402
from handlers.message import _build_file_context, respond_to_message_event  # noqa: E402
from helpers.files import download_slack_files  # noqa: E402
from helpers.slack_api import post_message  # noqa: E402


def _message_body(*, subtype=None, thread_ts=None, text="Hello", files=None):
    event = {
        "type": "message",
        "channel": "C001",
        "user": "U001",
        "text": text,
        "ts": "1234567890.000002",
    }
    if subtype:
        event["subtype"] = subtype
    if thread_ts:
        event["thread_ts"] = thread_ts
    if files:
        event["files"] = files
    return {"event_id": "Ev1", "team_id": "T001", "event": event}


class TestMessageSubtypeAllowlist:
    def test_thread_broadcast_goes_to_thread_reply(self):
        body = _message_body(subtype="thread_broadcast", thread_ts="1234567890.000001")
        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._build_file_context", return_value=([], [])),
            patch("handlers.message._handle_new_post") as new_post,
            patch("handlers.message._handle_thread_reply") as thread_reply,
            patch("handlers.message.run_claimed", side_effect=lambda _body, fn: fn()),
            patch("handlers.message.helpers.channel_has_membership", return_value=True),
            patch("handlers.message.helpers.origin_publishes_anywhere", return_value=True),
            patch("handlers.message.helpers.post_meta_exists_for_channel_ts", return_value=False),
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
        thread_reply.assert_called_once()
        new_post.assert_not_called()
        ctx = thread_reply.call_args.args[3]
        assert ctx["reply_broadcast"] is True

    def test_me_message_syncs_as_new_post(self):
        body = _message_body(subtype="me_message", text="/me waves")
        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._build_file_context", return_value=([], [])),
            patch("handlers.message._handle_new_post") as new_post,
            patch("handlers.message._handle_thread_reply") as thread_reply,
            patch("handlers.message.run_claimed", side_effect=lambda _body, fn: fn()),
            patch("handlers.message.helpers.channel_has_membership", return_value=True),
            patch("handlers.message.helpers.origin_publishes_anywhere", return_value=True),
            patch("handlers.message.helpers.post_meta_exists_for_channel_ts", return_value=False),
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
        new_post.assert_called_once()
        thread_reply.assert_not_called()

    def test_channel_join_is_skipped(self):
        body = _message_body(subtype="channel_join")
        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._build_file_context") as files,
            patch("handlers.message._handle_new_post") as new_post,
            patch("handlers.message.run_claimed") as claimed,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
        files.assert_not_called()
        new_post.assert_not_called()
        claimed.assert_not_called()

    def test_message_replied_is_skipped(self):
        body = _message_body(subtype="message_replied", thread_ts="1.1")
        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as new_post,
            patch("handlers.message._handle_thread_reply") as thread_reply,
            patch("handlers.message.run_claimed") as claimed,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
        new_post.assert_not_called()
        thread_reply.assert_not_called()
        claimed.assert_not_called()


class TestHostedFiles:
    def test_download_keeps_pdf_audio_zip_and_skips_stubs(self):
        client = MagicMock()
        client.token = "xoxb-0-0"
        logger = MagicMock()
        files = [
            {
                "id": "Fpdf",
                "url_private": "https://files.slack.com/pdf",
                "name": "doc.pdf",
                "mimetype": "application/pdf",
                "filetype": "pdf",
            },
            {
                "id": "Fmp3",
                "url_private": "https://files.slack.com/mp3",
                "name": "clip.mp3",
                "mimetype": "audio/mpeg",
                "filetype": "mp3",
            },
            {
                "id": "Fzip",
                "url_private": "https://files.slack.com/zip",
                "name": "bundle.zip",
                "mimetype": "application/zip",
                "filetype": "zip",
            },
            {"id": "Fstub", "mode": "tombstone", "name": "gone.png"},
            {
                "id": "Faccess",
                "mode": "file_access",
                "url_private": "https://files.slack.com/nope",
                "name": "secret.pdf",
            },
            {"id": "Fext", "is_external": True, "name": "drive.doc"},
        ]
        with patch("helpers.files._download_to_file") as download:
            got = download_slack_files(files, client, logger)
        assert [f["name"] for f in got] == ["doc.pdf", "clip.mp3", "bundle.zip"]
        assert download.call_count == 3

    def test_build_file_context_passes_all_hosted_files(self):
        body = _message_body(
            subtype="file_share",
            files=[
                {
                    "id": "Fpdf",
                    "url_private": "https://files.slack.com/pdf",
                    "mimetype": "application/pdf",
                    "name": "a.pdf",
                },
            ],
        )
        with patch(
            "handlers.message.helpers.download_slack_files", return_value=[{"path": "/tmp/a.pdf", "name": "a.pdf"}]
        ) as dl:
            _blocks, direct = _build_file_context(body, MagicMock(), MagicMock())
        assert direct[0]["name"] == "a.pdf"
        passed = dl.call_args.args[0]
        assert passed[0]["mimetype"] == "application/pdf"

    def test_post_message_fallback_is_shared_a_file(self):
        slack = MagicMock()
        slack.chat_postMessage.return_value = {"ts": "1.2"}
        with patch("helpers.slack_api.WebClient", return_value=slack):
            post_message(bot_token="xoxb", channel_id="C1", msg_text="  ")
        assert slack.chat_postMessage.call_args.kwargs["text"] == "Shared a file"


class TestFederationPayloadParity:
    def test_message_payload_includes_images_and_reply_broadcast(self):
        payload = build_message_payload(
            sync_id=1,
            post_id="p1",
            channel_id="C1",
            user_name="Ada",
            user_avatar_url=None,
            workspace_name="WS",
            text="hi",
            thread_post_id="parent",
            images=[{"url": "https://gif.example/a.gif", "alt_text": "gif"}],
            reply_broadcast=True,
        )
        assert payload["images"] == [{"url": "https://gif.example/a.gif", "alt_text": "gif"}]
        assert payload["reply_broadcast"] is True
        assert payload["thread_post_id"] == "parent"

    def test_edit_payload_includes_images(self):
        payload = build_edit_payload(
            post_id="p1",
            channel_id="C1",
            text="edited",
            timestamp="1.2",
            images=[{"url": "https://gif.example/a.gif", "alt_text": "gif"}],
        )
        assert payload["images"] == [{"url": "https://gif.example/a.gif", "alt_text": "gif"}]


class TestFederationInboundReplyBroadcast:
    def test_handle_message_passes_reply_broadcast_and_images(self):
        from db import schemas
        from federation import api as federation_api

        sc = MagicMock()
        sc.id = 9
        sc.channel_id = "C1"
        sc.subscribes = True
        ws = MagicMock()
        ws.id = 2
        ws.bot_token = "enc"
        fed_ws = MagicMock()
        body = {
            "channel_id": "C1",
            "text": "hi",
            "post_id": "p1",
            "reply_broadcast": True,
            "user": {"display_name": "Ada"},
            "images": [{"url": "https://gif.example/a.gif", "alt_text": "gif"}],
        }
        created = [
            schemas.PostMeta(
                post_id="p1",
                sync_channel_id=9,
                ts=1.2,
                kind="message",
            )
        ]
        with (
            patch.object(federation_api, "_resolve_channel_for_federated", return_value=(sc, ws)),
            patch.object(federation_api, "_resolve_mentions_for_federated", side_effect=lambda text, *_a, **_k: text),
            patch("federation.api.helpers.decrypt_bot_token", return_value="xoxb"),
            patch("federation.api.helpers.resolve_channel_references", side_effect=lambda text, *_a, **_k: text),
            patch("federation.api.WebClient"),
            patch("federation.api.apply_target", return_value=created) as apply,
            patch.object(federation_api.DbManager, "create_records") as create,
        ):
            status, resp = federation_api.handle_message(body, fed_ws)

        assert status == 200
        assert resp["ok"] is True
        envelope = apply.call_args.args[0]
        assert envelope["reply_broadcast"] is True
        assert envelope["images"][0]["image_url"] == "https://gif.example/a.gif"
        assert create.called
