"""Message subtype allowlist, hosted files, and federation payload parity."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from federation.deliver import build_remote_envelope  # noqa: E402
from handlers.message import _build_file_context, respond_to_message_event  # noqa: E402
from helpers.files import download_slack_files, event_keeps_hosted_files, max_file_bytes  # noqa: E402
from helpers.slack_api import post_message  # noqa: E402
from helpers.sync_apply import ApplyOutcome  # noqa: E402


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
            patch("handlers.message.helpers.iter_publish_targets", return_value=[object()]),
            patch("handlers.message.helpers.post_meta_exists_for_channel_ts", return_value=False),
            patch("handlers.message.helpers.has_user_action_echo", return_value=False),
            patch("handlers.message.helpers.take_user_action_echo", return_value=False),
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
            patch("handlers.message.helpers.iter_publish_targets", return_value=[object()]),
            patch("handlers.message.helpers.post_meta_exists_for_channel_ts", return_value=False),
            patch("handlers.message.helpers.has_user_action_echo", return_value=False),
            patch("handlers.message.helpers.take_user_action_echo", return_value=False),
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
            patch("handlers.message.run_claimed", side_effect=lambda _body, fn: fn()) as claimed,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
        files.assert_not_called()
        new_post.assert_not_called()
        claimed.assert_called_once()

    def test_message_replied_is_skipped(self):
        body = _message_body(subtype="message_replied", thread_ts="1.1")
        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as new_post,
            patch("handlers.message._handle_thread_reply") as thread_reply,
            patch("handlers.message.run_claimed", side_effect=lambda _body, fn: fn()) as claimed,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
        new_post.assert_not_called()
        thread_reply.assert_not_called()
        claimed.assert_called_once()


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
            {"id": "Fhidden", "mode": "hidden_by_limit", "name": "old.png"},
            {"id": "Fext", "is_external": True, "name": "drive.doc"},
        ]
        with patch("helpers.files._download_and_hash") as download:
            download.side_effect = [
                ("aa" * 32, 1, "/tmp/sb-file-" + "aa" * 32),
                ("bb" * 32, 1, "/tmp/sb-file-" + "bb" * 32),
                ("cc" * 32, 1, "/tmp/sb-file-" + "cc" * 32),
            ]
            got = download_slack_files(files, client, logger)
        assert [f["name"] for f in got] == ["doc.pdf", "clip.mp3", "bundle.zip"]
        assert download.call_count == 3

    def test_build_file_context_passes_all_hosted_files(self):
        files = [
            {
                "id": f"F{i:02d}",
                "url_private": f"https://files.slack.com/f{i}",
                "mimetype": "application/pdf",
                "name": f"a{i}.pdf",
            }
            for i in range(21)
        ]
        body = _message_body(subtype="file_share", files=files)
        downloaded = [{"path": f"/tmp/a{i}.pdf", "name": f"a{i}.pdf"} for i in range(21)]
        with patch("handlers.message.helpers.download_slack_files", return_value=downloaded) as dl:
            _blocks, direct = _build_file_context(body, MagicMock(), MagicMock())
        assert len(direct) == 21
        passed = dl.call_args.args[0]
        assert len(passed) == 21
        assert passed[20]["id"] == "F20"

    def test_gif_attachment_stays_when_a_file_is_also_shared(self):
        body = _message_body(
            text="see https://example.com/notes",
            files=[
                {
                    "id": "Fpng",
                    "url_private": "https://files.slack.com/png",
                    "mimetype": "image/png",
                    "name": "notes.png",
                }
            ],
        )
        body["event"]["attachments"] = [
            {
                "fallback": "clip",
                "blocks": [{"type": "image", "image_url": "https://media.example/clip.gif", "alt_text": "clip"}],
            },
            {"fallback": "png thumb", "image_url": "https://files.slack.com/files-pri/thumb.png"},
        ]
        downloaded = [{"path": "/tmp/notes.png", "name": "notes.png"}]
        with patch("handlers.message.helpers.download_slack_files", return_value=downloaded):
            blocks, direct = _build_file_context(body, MagicMock(), MagicMock())
        assert direct == downloaded
        assert blocks == [{"type": "image", "image_url": "https://media.example/clip.gif", "alt_text": "clip"}]

    def test_event_keeps_file_only_thread_without_subtype(self):
        event = {"files": [{"id": "F1"}], "type": "message"}
        assert event_keeps_hosted_files(event, is_reply=True, text=" ")
        assert event_keeps_hosted_files(event, is_reply=True, text="")
        assert not event_keeps_hosted_files(event, is_reply=True, text="see attached")

    def test_event_keeps_also_send_to_channel_thread_files(self):
        event = {
            "subtype": "thread_broadcast",
            "files": [{"id": "F1"}],
            "type": "message",
        }
        assert event_keeps_hosted_files(event, is_reply=True, text="also sent to channel")
        flagged = {"files": [{"id": "F1"}], "reply_broadcast": True, "type": "message"}
        assert event_keeps_hosted_files(flagged, is_reply=True, text="also sent to channel")
        assert event_keeps_hosted_files(
            {"subtype": "reply_broadcast", "files": [{"id": "F1"}]},
            is_reply=True,
            text="also sent to channel",
        )

    def test_event_keeps_upload_false_thread_file_share(self):
        event = {"subtype": "file_share", "upload": False, "files": [{"id": "F1"}]}
        assert event_keeps_hosted_files(event, is_reply=True, text="")
        assert event_keeps_hosted_files(event, is_reply=True, text="Hello from Workspace A")

    def test_max_file_bytes_is_slack_1gb(self):
        assert max_file_bytes() == 1024 * 1024 * 1024

    def test_download_prefers_url_private_download(self):
        client = MagicMock()
        client.token = "xoxb-0-0"
        logger = MagicMock()
        files = [
            {
                "id": "F1",
                "url_private": "https://files.slack.com/private",
                "url_private_download": "https://files.slack.com/download",
                "name": "doc.pdf",
                "mimetype": "application/pdf",
            }
        ]
        with patch("helpers.files._download_and_hash", return_value=("aa" * 32, 1, "/tmp/sb-file-" + "aa" * 32)) as dl:
            download_slack_files(files, client, logger)
        assert dl.call_args.args[0] == "https://files.slack.com/download"

    def test_post_message_empty_text_stays_blank(self):
        slack = MagicMock()
        slack.chat_postMessage.return_value = {"ts": "1.2"}
        with patch("helpers.slack_api.WebClient", return_value=slack):
            post_message(bot_token="xoxb-test", channel_id="C1", msg_text="  ")
        assert slack.chat_postMessage.call_args.kwargs["text"] == ""


class TestFederationPayloadParity:
    def test_remote_envelope_keeps_images_and_reply_broadcast(self):
        payload = build_remote_envelope(
            {
                "kind": "message",
                "action": "create",
                "post_id": "p1",
                "text": "hi",
                "thread_post_id": "parent",
                "images": [{"url": "https://gif.example/a.gif", "alt_text": "gif"}],
                "reply_broadcast": True,
            },
            "C1",
        )
        assert payload["images"] == [{"url": "https://gif.example/a.gif", "alt_text": "gif"}]
        assert payload["reply_broadcast"] is True
        assert payload["thread_post_id"] == "parent"
        assert payload["channel_id"] == "C1"

    def test_remote_envelope_keeps_edit_images(self):
        payload = build_remote_envelope(
            {
                "kind": "message",
                "action": "edit",
                "post_id": "p1",
                "text": "edited",
                "images": [{"url": "https://gif.example/a.gif", "alt_text": "gif"}],
            },
            "C1",
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
            "kind": "message",
            "action": "create",
            "channel_id": "C1",
            "text": "hi",
            "post_id": "p1",
            "reply_broadcast": True,
            "user_name": "Ada Lovelace",
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
            patch.object(federation_api, "_source_stub_id", return_value=None),
            patch("federation.api.helpers.decrypt_bot_token", return_value="xoxb"),
            patch("federation.api.helpers.resolve_channel_references", side_effect=lambda text, *_a, **_k: text),
            patch("federation.api.WebClient"),
            patch("helpers.post_meta.get_target_post_meta", return_value=None),
            patch.object(federation_api, "_get_post_records", return_value=[]),
            patch("federation.api.apply_target", return_value=ApplyOutcome(created=created)) as apply,
        ):
            status, resp = federation_api.handle_message(body, fed_ws)

        assert status == 200
        assert resp["ok"] is True
        envelope = apply.call_args.args[0]
        assert envelope["reply_broadcast"] is True
        assert envelope["images"][0]["image_url"] == "https://gif.example/a.gif"

    def test_handle_message_file_refs_without_sha256_is_409(self):
        from federation import api as federation_api

        sc = MagicMock()
        sc.id = 9
        sc.channel_id = "C1"
        sc.subscribes = True
        ws = MagicMock()
        fed_ws = MagicMock()
        body = {
            "kind": "message",
            "action": "create",
            "channel_id": "C1",
            "text": "hi",
            "post_id": "p1",
            "file_refs": [{"name": "a.pdf", "size": 1}],
        }
        with (
            patch.object(federation_api, "_resolve_channel_for_federated", return_value=(sc, ws)),
            patch.object(federation_api, "_source_stub_id", return_value=None),
            patch.object(federation_api, "_get_post_records", return_value=[]),
            patch("federation.api.apply_target") as apply,
        ):
            status, resp = federation_api.handle_message(body, fed_ws)

        assert status == 409
        assert resp["error"] == "incomplete_file"
        apply.assert_not_called()

    def test_handle_message_materialize_fail_is_409(self):
        from federation import api as federation_api

        sc = MagicMock()
        sc.id = 9
        sc.channel_id = "C1"
        sc.subscribes = True
        ws = MagicMock()
        fed_ws = MagicMock()
        sha = "aa" * 32
        body = {
            "kind": "message",
            "action": "create",
            "channel_id": "C1",
            "text": "hi",
            "post_id": "p1",
            "file_refs": [{"sha256": sha, "size": 1, "name": "a.pdf"}],
        }
        with (
            patch.object(federation_api, "_resolve_channel_for_federated", return_value=(sc, ws)),
            patch.object(federation_api, "_source_stub_id", return_value=None),
            patch.object(federation_api, "_get_post_records", return_value=[]),
            patch("federation.files.materialize_file", return_value=None),
            patch("federation.api.apply_target") as apply,
        ):
            status, resp = federation_api.handle_message(body, fed_ws)

        assert status == 409
        assert resp["error"] == "incomplete_file"
        assert resp["sha256"] == sha
        apply.assert_not_called()

    def test_handle_message_assemble_failed_is_409(self):
        from federation import api as federation_api
        from federation.files import AssembleFailed

        sc = MagicMock()
        sc.id = 9
        sc.channel_id = "C1"
        sc.subscribes = True
        ws = MagicMock()
        fed_ws = MagicMock()
        sha = "aa" * 32
        body = {
            "kind": "message",
            "action": "create",
            "channel_id": "C1",
            "text": "hi",
            "post_id": "p1",
            "file_refs": [{"sha256": sha, "size": 1, "name": "a.pdf"}],
        }
        with (
            patch.object(federation_api, "_resolve_channel_for_federated", return_value=(sc, ws)),
            patch.object(federation_api, "_source_stub_id", return_value=None),
            patch.object(federation_api, "_get_post_records", return_value=[]),
            patch("federation.files.materialize_file", side_effect=AssembleFailed(sha)),
            patch("federation.api.apply_target") as apply,
        ):
            status, resp = federation_api.handle_message(body, fed_ws)

        assert status == 409
        assert resp["error"] == "assemble_failed"
        assert resp["sha256"] == sha
        apply.assert_not_called()


class TestDeliverRemoteFileContract:
    def test_stage_fail_dms_and_skips_push(self):
        from federation.deliver import deliver_remote

        envelope = {
            "kind": "message",
            "action": "create",
            "post_id": "p1",
            "source_user_id": "U001",
            "file_refs": [{"sha256": "aa" * 32, "size": 1, "path": "/tmp/x", "name": "x.png"}],
        }
        sc = MagicMock()
        sc.channel_id = "C1"
        sc.id = 1
        source_client = MagicMock()
        with (
            patch("federation.deliver._stage_files_to_peer", return_value=False),
            patch("federation.deliver.push_message") as push,
            patch("federation.deliver.notify_source_user_error") as notify,
        ):
            created = deliver_remote(envelope, MagicMock(), sc, source_client=source_client)

        assert created == []
        push.assert_not_called()
        notify.assert_called_once()
        assert notify.call_args.kwargs["source_client"] is source_client
        assert notify.call_args.kwargs["source_user_id"] == "U001"

    def test_parent_missing_does_not_dm(self):
        from federation.deliver import deliver_remote

        envelope = {
            "kind": "message",
            "action": "create",
            "post_id": "p1",
            "thread_post_id": "parent",
            "source_user_id": "U001",
        }
        sc = MagicMock()
        sc.channel_id = "C1"
        sc.id = 1
        with (
            patch("federation.deliver.push_message", return_value={"_http_status": 409, "error": "parent_missing"}),
            patch("federation.deliver.notify_source_user_error") as notify,
        ):
            created = deliver_remote(envelope, MagicMock(), sc, source_client=MagicMock())

        assert created == []
        notify.assert_not_called()

    def test_incomplete_file_restages_then_succeeds(self):
        from federation.deliver import deliver_remote

        envelope = {
            "kind": "message",
            "action": "create",
            "post_id": "p1",
            "source_user_id": "U001",
            "file_refs": [{"sha256": "aa" * 32, "size": 1, "path": "/tmp/x", "name": "x.png"}],
        }
        sc = MagicMock()
        sc.channel_id = "C1"
        sc.id = 1
        with (
            patch("federation.deliver._stage_files_to_peer", return_value=True) as stage,
            patch(
                "federation.deliver.push_message",
                side_effect=[
                    {"_http_status": 409, "error": "incomplete_file"},
                    {"_http_status": 200, "ok": True, "ts": "11.000001"},
                ],
            ) as push,
            patch("federation.deliver.notify_source_user_error") as notify,
            patch("federation.deliver.DbManager.create_records"),
        ):
            created = deliver_remote(envelope, MagicMock(), sc, source_client=MagicMock())

        assert len(created) == 1
        assert stage.call_count == 2
        assert push.call_count == 2
        notify.assert_not_called()

    def test_incomplete_file_second_409_dms(self):
        from federation.deliver import deliver_remote

        envelope = {
            "kind": "message",
            "action": "create",
            "post_id": "p1",
            "source_user_id": "U001",
            "file_refs": [{"sha256": "aa" * 32, "size": 1, "path": "/tmp/x", "name": "x.png"}],
        }
        sc = MagicMock()
        sc.channel_id = "C1"
        sc.id = 1
        with (
            patch("federation.deliver._stage_files_to_peer", return_value=True),
            patch(
                "federation.deliver.push_message",
                return_value={"_http_status": 409, "error": "incomplete_file"},
            ),
            patch("federation.deliver.notify_source_user_error") as notify,
        ):
            created = deliver_remote(envelope, MagicMock(), sc, source_client=MagicMock())

        assert created == []
        notify.assert_called_once()
        assert notify.call_args.kwargs["details"]["reason"] == "incomplete_file"

    def test_assemble_failed_dms_without_another_upload(self):
        from federation.deliver import deliver_remote

        envelope = {
            "kind": "message",
            "action": "create",
            "post_id": "p1",
            "source_user_id": "U001",
            "file_refs": [{"sha256": "aa" * 32, "size": 1, "path": "/tmp/x", "name": "x.png"}],
        }
        sc = MagicMock()
        sc.channel_id = "C1"
        sc.id = 1
        with (
            patch("federation.deliver._stage_files_to_peer", return_value=True) as stage,
            patch(
                "federation.deliver.push_message",
                return_value={"_http_status": 409, "error": "assemble_failed"},
            ) as push,
            patch("federation.deliver.notify_source_user_error") as notify,
        ):
            created = deliver_remote(envelope, MagicMock(), sc, source_client=MagicMock())

        assert created == []
        assert stage.call_count == 1
        assert push.call_count == 1
        notify.assert_called_once()
        assert notify.call_args.kwargs["details"]["reason"] == "assemble_failed"
        assert notify.call_args.kwargs["details"]["error"] == "assemble_failed"
