"""Tests for Slack event_id idempotency (at-least-once delivery)."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from db.event_claims import (  # noqa: E402
    claim_event,
    complete_event,
    release_event,
    run_claimed,
    slack_event_identity,
)
from db.schemas import ProcessedEvent  # noqa: E402
from handlers.message import respond_to_message_event  # noqa: E402
from handlers.reaction_event import handle_reaction  # noqa: E402


@pytest.fixture
def event_db(tmp_path, monkeypatch):
    """File-backed SQLite so claim rows survive across sessions (not :memory:)."""
    import db as db_mod
    from db import initialize_database

    url = f"sqlite:///{tmp_path / 'events.db'}"
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("DATABASE_URL", url)
    old_engine = db_mod.GLOBAL_ENGINE
    old_session = db_mod.GLOBAL_SESSION
    old_schema = db_mod.GLOBAL_SCHEMA
    db_mod.GLOBAL_ENGINE = None
    db_mod.GLOBAL_SESSION = None
    db_mod.GLOBAL_SCHEMA = None
    initialize_database()
    yield
    if db_mod.GLOBAL_ENGINE:
        db_mod.GLOBAL_ENGINE.dispose()
    db_mod.GLOBAL_ENGINE = old_engine
    db_mod.GLOBAL_SESSION = old_session
    db_mod.GLOBAL_SCHEMA = old_schema


@pytest.fixture(autouse=True)
def pipeline_guards():
    with (
        patch("helpers.channel_has_membership", return_value=True),
        patch("helpers.origin_publishes_anywhere", return_value=True),
        patch("helpers.iter_publish_targets", return_value=[object()]),
        patch("helpers.post_meta_exists_for_channel_ts", return_value=False),
        patch("helpers.has_user_action_echo", return_value=False),
        patch("helpers.take_user_action_echo", return_value=False),
        patch("handlers.message.helpers.complete_copy_ts_from_pending_share", return_value=None),
    ):
        yield


def _message_body(**overrides):
    body = {
        "event_id": "EvMESSAGE1",
        "team_id": "T001",
        "event": {
            "type": "message",
            "channel": "C001",
            "user": "U001",
            "text": "Hello",
            "ts": "1234567890.000001",
        },
    }
    body.update(overrides)
    return body


class TestSlackEventIdentity:
    def test_reads_envelope_not_event_ts(self):
        body = {
            "event_id": "EvABC",
            "team_id": "T9",
            "event": {"ts": "1.2", "type": "message"},
        }
        assert slack_event_identity(body) == ("T9", "EvABC")

    def test_missing_event_id_returns_none(self):
        assert slack_event_identity({"team_id": "T1", "event": {"ts": "1.2"}}) is None


class TestClaimHelpers:
    def test_second_claim_is_rejected(self, event_db):
        assert claim_event("T001", "Ev1") is True
        assert claim_event("T001", "Ev1") is False

    def test_complete_then_claim_skips(self, event_db):
        assert claim_event("T001", "Ev2") is True
        complete_event("T001", "Ev2")
        assert claim_event("T001", "Ev2") is False

    def test_release_allows_retry(self, event_db):
        assert claim_event("T001", "Ev3") is True
        release_event("T001", "Ev3")
        assert claim_event("T001", "Ev3") is True

    def test_stale_pending_is_reclaimed(self, event_db):
        from db import close_session, get_session

        assert claim_event("T001", "EvStale") is True
        session = get_session()
        try:
            row = session.query(ProcessedEvent).filter(ProcessedEvent.event_id == "EvStale").one()
            row.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
            session.commit()
        finally:
            close_session(session)
        assert claim_event("T001", "EvStale") is True

    def test_concurrent_claim_only_one_wins(self, event_db):
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            results.append(claim_event("T001", "EvRace"))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 1
        assert results.count(False) == 1

    def test_work_returning_false_releases_claim(self, event_db):
        calls: list[int] = []

        def work() -> bool:
            calls.append(1)
            return False

        body = {"event_id": "EvNotReady", "team_id": "T001"}
        run_claimed(body, work)
        run_claimed(body, work)
        assert calls == [1, 1]


class TestRespondToMessageEventDedup:
    def test_text_only_no_subtype_still_calls_new_post(self):
        client = MagicMock()
        logger = MagicMock()
        context = {}

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._build_file_context", return_value=([], [])),
        ):
            respond_to_message_event(_message_body(event_id=""), client, logger, context)

        mock_new.assert_called_once()

    def test_copy_exists_skips_inbound_create(self):
        with (
            patch("helpers.post_meta_exists_for_channel_ts", return_value=True),
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch("handlers.message._build_file_context") as build_fc,
        ):
            respond_to_message_event(_message_body(event_id=""), MagicMock(), MagicMock(), {})

        mock_new.assert_not_called()
        mock_reply.assert_not_called()
        build_fc.assert_not_called()

    def test_no_subtype_with_files_skips_without_building_file_context(self):
        body = _message_body(event_id="")
        body["event"]["files"] = [{"id": "F1", "mimetype": "image/jpeg"}]

        client = MagicMock()
        logger = MagicMock()
        context = {}

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._build_file_context") as build_fc,
        ):
            respond_to_message_event(body, client, logger, context)

        mock_new.assert_not_called()
        build_fc.assert_not_called()

    def test_pending_file_share_completes_claim_so_retry_is_noop(self, event_db):
        body = _message_body()
        body["event"]["files"] = [{"id": "F1", "mimetype": "image/jpeg"}]
        with (
            patch("handlers.message._is_own_bot_message", return_value=False) as own_bot,
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._build_file_context") as build_fc,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        own_bot.assert_called_once()
        mock_new.assert_not_called()
        build_fc.assert_not_called()

    def test_own_bot_file_share_records_pending_copy_ts(self, event_db):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "file_share"
        body["event"]["files"] = [{"id": "F1"}]
        with (
            patch("handlers.message._is_own_bot_message", return_value=True),
            patch("handlers.message.helpers.complete_copy_ts_from_pending_share", return_value=True) as complete,
            patch("handlers.message._handle_new_post") as mock_new,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
        complete.assert_called_once()
        mock_new.assert_not_called()

    def test_own_bot_pending_copy_not_ready_releases_claim(self, event_db):
        body = _message_body()
        body["event"]["subtype"] = "file_share"
        body["event"]["files"] = [{"id": "F1"}]
        with (
            patch("handlers.message._is_own_bot_message", return_value=True),
            patch("handlers.message.helpers.complete_copy_ts_from_pending_share", return_value=False) as complete,
            patch("handlers.message._handle_new_post") as mock_new,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
        assert complete.call_count == 2
        mock_new.assert_not_called()

    def test_file_share_subtype_still_calls_new_post(self):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "file_share"
        body["event"]["files"] = [{"id": "F1", "mimetype": "image/jpeg"}]

        client = MagicMock()
        logger = MagicMock()
        context = {}

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch(
                "handlers.message._build_file_context",
                return_value=([], [{"path": "/tmp/x", "name": "x.jpg", "mimetype": "image/jpeg"}]),
            ),
        ):
            respond_to_message_event(body, client, logger, context)

        mock_new.assert_called_once()

    def test_user_token_file_id_echo_skips_file_share(self):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "file_share"
        body["event"]["files"] = [{"id": "F99"}]

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message.helpers.has_user_action_echo", return_value=True),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch("handlers.message._build_file_context") as build_fc,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mock_new.assert_not_called()
        mock_reply.assert_not_called()
        build_fc.assert_not_called()

    def test_user_token_file_id_echo_skips_every_event_with_that_file(self):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "file_share"
        body["event"]["files"] = [{"id": "F99"}]
        taken = []

        def take(_team, _user, kind, fingerprint):
            taken.append((kind, fingerprint))
            return False

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message.helpers.has_user_action_echo", return_value=True) as has_echo,
            patch("handlers.message.helpers.take_user_action_echo", side_effect=take),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
            body["event"]["subtype"] = "message_changed"
            body["event"]["message"] = {"files": [{"id": "F99"}], "ts": body["event"]["ts"]}
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        assert has_echo.call_count == 2
        assert taken == []
        mock_new.assert_not_called()
        mock_reply.assert_not_called()

    def test_user_token_message_echo_skips_without_consuming(self):
        body = _message_body(event_id="")
        peeked = []

        def has_echo(_team, _user, kind, fingerprint):
            peeked.append((kind, fingerprint))
            return kind == "message"

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message.helpers.has_user_action_echo", side_effect=has_echo),
            patch("handlers.message.helpers.take_user_action_echo") as take,
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_message_edit") as mock_edit,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
            body["event"]["subtype"] = "message_changed"
            body["event"]["message"] = {"text": "Hello", "ts": body["event"]["ts"]}
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        assert ("message", "C001:1234567890.000001") in peeked
        take.assert_not_called()
        mock_new.assert_not_called()
        mock_edit.assert_not_called()

    def test_file_share_with_own_thread_ts_is_new_post(self):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "file_share"
        body["event"]["thread_ts"] = body["event"]["ts"]
        body["event"]["files"] = [{"id": "F1", "mimetype": "image/jpeg", "timestamp": 1234567890}]

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch(
                "handlers.message._build_file_context",
                return_value=([], [{"path": "/tmp/x", "name": "x.jpg"}]),
            ),
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mock_new.assert_called_once()
        mock_reply.assert_not_called()

    def test_thread_reply_with_parent_files_is_not_pending_file_share(self):
        body = _message_body(event_id="")
        body["event"]["ts"] = "1234567890.000002"
        body["event"]["thread_ts"] = "1234567890.000001"
        body["event"]["files"] = [{"id": "F99"}]

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch("handlers.message._build_file_context", return_value=([], [])) as build_fc,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mock_reply.assert_called_once()
        mock_new.assert_not_called()
        assert "files" not in build_fc.call_args.args[0]["event"]

    def test_thread_reply_with_parent_file_echo_still_syncs(self):
        body = _message_body(event_id="")
        body["event"]["ts"] = "1234567890.000002"
        body["event"]["thread_ts"] = "1234567890.000001"
        body["event"]["files"] = [{"id": "F99"}]

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message.helpers.has_user_action_echo", side_effect=lambda *_a, **_k: _a[2] == "file"),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch("handlers.message._build_file_context", return_value=([], [])),
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mock_reply.assert_called_once()
        mock_new.assert_not_called()

    def test_file_share_download_empty_dms_and_skips(self):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "file_share"
        body["event"]["text"] = ""
        body["event"]["files"] = [{"id": "F1"}]
        client = MagicMock()

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch("handlers.message._build_file_context", return_value=([], [])),
            patch("handlers.message.helpers.notify_source_user_error") as notify,
        ):
            respond_to_message_event(body, client, MagicMock(), {})

        mock_new.assert_not_called()
        mock_reply.assert_not_called()
        notify.assert_called_once()
        assert notify.call_args.kwargs["source_client"] is client

    def test_thread_file_only_without_subtype_keeps_files(self):
        body = _message_body(event_id="")
        body["event"]["text"] = ""
        body["event"]["ts"] = "1234567890.000002"
        body["event"]["thread_ts"] = "1234567890.000001"
        body["event"]["files"] = [{"id": "F_NEW"}]

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch(
                "handlers.message._build_file_context",
                return_value=([], [{"path": "/tmp/x", "name": "x.jpg"}]),
            ) as build_fc,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mock_reply.assert_called_once()
        mock_new.assert_not_called()
        assert build_fc.call_args.args[0]["event"]["files"] == [{"id": "F_NEW"}]

    def test_thread_file_share_still_syncs_with_files(self):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "file_share"
        body["event"]["ts"] = "1234567890.000002"
        body["event"]["thread_ts"] = "1234567890.000001"
        body["event"]["files"] = [{"id": "F_NEW"}]

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch(
                "handlers.message._build_file_context",
                return_value=([], [{"path": "/tmp/x", "name": "x.jpg"}]),
            ) as build_fc,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mock_reply.assert_called_once()
        mock_new.assert_not_called()
        assert build_fc.call_args.args[0]["event"]["files"] == [{"id": "F_NEW"}]

    def test_thread_file_share_upload_false_keeps_files(self):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "file_share"
        body["event"]["upload"] = False
        body["event"]["text"] = "Hello from Workspace A"
        body["event"]["ts"] = "1234567890.000002"
        body["event"]["thread_ts"] = "1234567890.000001"
        body["event"]["files"] = [
            {
                "id": "F99",
                "mode": "hosted",
                "url_private": "https://files.slack.com/F99",
            }
        ]

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch(
                "handlers.message._build_file_context",
                return_value=([], [{"path": "/tmp/x", "name": "x.jpg"}]),
            ) as build_fc,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mock_reply.assert_called_once()
        mock_new.assert_not_called()
        assert build_fc.call_args.args[0]["event"]["files"][0]["id"] == "F99"

    def test_thread_file_share_upload_false_file_echo_skips(self):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "file_share"
        body["event"]["upload"] = False
        body["event"]["ts"] = "1234567890.000002"
        body["event"]["thread_ts"] = "1234567890.000001"
        body["event"]["files"] = [{"id": "F99"}]

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message.helpers.has_user_action_echo", side_effect=lambda *_a, **_k: _a[2] == "file"),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch("handlers.message._build_file_context") as build_fc,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mock_reply.assert_not_called()
        mock_new.assert_not_called()
        build_fc.assert_not_called()

    def test_top_level_plain_message_with_url_is_not_pending(self):
        body = _message_body(event_id="")
        body["event"]["files"] = [
            {
                "id": "F1",
                "mode": "hosted",
                "url_private": "https://files.slack.com/F1",
            }
        ]

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch(
                "handlers.message._build_file_context",
                return_value=([], [{"path": "/tmp/x", "name": "x.jpg"}]),
            ) as build_fc,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mock_new.assert_called_once()
        mock_reply.assert_not_called()
        assert build_fc.call_args.args[0]["event"]["files"][0]["id"] == "F1"

    def test_top_level_file_share_upload_false_with_caption_keeps_files(self):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "file_share"
        body["event"]["upload"] = False
        body["event"]["text"] = "Hello from Workspace A"
        body["event"]["files"] = [
            {
                "id": "F1",
                "mode": "hosted",
                "url_private": "https://files.slack.com/F1",
            }
        ]

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch(
                "handlers.message._build_file_context",
                return_value=([], [{"path": "/tmp/x", "name": "x.jpg"}]),
            ) as build_fc,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mock_new.assert_called_once()
        mock_reply.assert_not_called()
        assert build_fc.call_args.args[0]["event"]["files"][0]["id"] == "F1"

    def test_hidden_by_limit_file_share_dms_and_skips(self):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "file_share"
        body["event"]["text"] = ""
        body["event"]["files"] = [{"id": "F1", "mode": "hidden_by_limit"}]
        client = MagicMock()

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch("handlers.message._build_file_context", return_value=([], [])),
            patch("handlers.message.helpers.notify_source_user_error") as notify,
        ):
            respond_to_message_event(body, client, MagicMock(), {})

        mock_new.assert_not_called()
        mock_reply.assert_not_called()
        notify.assert_called_once()
        assert notify.call_args.kwargs["source_client"] is client

    def test_top_level_slack_hosted_video_block_waits_for_file_share(self):
        body = _message_body(event_id="")
        body["event"]["files"] = []
        body["event"]["blocks"] = [
            {
                "type": "video",
                "video_url": "https://files.slack.com/files-pri/clip.mp4",
                "title": {"type": "plain_text", "text": "clip.mp4"},
            }
        ]

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._build_file_context") as build_fc,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mock_new.assert_not_called()
        build_fc.assert_not_called()

    def test_thread_broadcast_with_caption_keeps_files(self):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "thread_broadcast"
        body["event"]["text"] = "also sent to channel"
        body["event"]["ts"] = "1234567890.000002"
        body["event"]["thread_ts"] = "1234567890.000001"
        body["event"]["files"] = [
            {
                "id": "F1",
                "mode": "hosted",
                "url_private": "https://files.slack.com/F1",
            }
        ]

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch(
                "handlers.message._build_file_context",
                return_value=([], [{"path": "/tmp/x", "name": "x.jpg"}]),
            ) as build_fc,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mock_reply.assert_called_once()
        mock_new.assert_not_called()
        assert build_fc.call_args.args[0]["event"]["files"][0]["id"] == "F1"

    def test_hosted_file_download_shortfall_dms_even_with_image_block(self):
        body = _message_body(event_id="")
        body["event"]["subtype"] = "file_share"
        body["event"]["text"] = "caption"
        body["event"]["files"] = [
            {
                "id": "F1",
                "mode": "hosted",
                "url_private": "https://files.slack.com/F1",
            }
        ]
        client = MagicMock()

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch(
                "handlers.message._build_file_context",
                return_value=([{"type": "image", "image_url": "https://gif.example/a.gif"}], []),
            ),
            patch("handlers.message.helpers.notify_source_user_error") as notify,
        ):
            respond_to_message_event(body, client, MagicMock(), {})

        mock_new.assert_not_called()
        mock_reply.assert_not_called()
        notify.assert_called_once()
        assert notify.call_args.kwargs["source_client"] is client

    def test_thread_reply_without_parent_releases_claim(self, event_db):
        body = _message_body()
        body["event"]["ts"] = "1234567890.000002"
        body["event"]["thread_ts"] = "1234567890.000001"
        lookups = {"n": 0}

        def _no_parent(*_a, **_k):
            lookups["n"] += 1
            return []

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message.helpers.get_post_records", side_effect=_no_parent),
            patch("handlers.message._build_file_context", return_value=([], [])),
            patch("handlers.message._handle_new_post") as mock_new,
        ):
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        assert lookups["n"] == 2
        mock_new.assert_not_called()

    def test_edit_without_post_meta_releases_claim(self, event_db):
        body = _message_body()
        body["event"]["subtype"] = "message_changed"
        body["event"]["message"] = {"ts": "1234567890.000001", "text": "edited"}
        lookups = {"n": 0}

        def _no_meta(*_a, **_k):
            lookups["n"] += 1
            return []

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._parse_event_fields_light") as parse,
            patch("handlers.message.helpers.get_post_records", side_effect=_no_meta),
            patch("handlers.message._build_file_context", return_value=([], [])),
            patch("handlers.message.helpers.run_sync_pipeline") as pipeline,
        ):
            parse.return_value = {
                "event_subtype": "message_changed",
                "thread_ts": None,
                "msg_text": "edited",
                "channel_id": "C001",
                "user_id": "U001",
                "team_id": "T001",
                "ts": "1234567890.000001",
                "mentioned_users": [],
                "content_blocks": [],
                "reply_broadcast": False,
            }
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        assert lookups["n"] == 2
        pipeline.assert_not_called()

    def test_delete_without_post_meta_releases_claim(self, event_db):
        body = _message_body()
        body["event"]["subtype"] = "message_deleted"
        body["event"]["previous_message"] = {"ts": "1234567890.000001"}
        lookups = {"n": 0}

        def _no_meta(*_a, **_k):
            lookups["n"] += 1
            return []

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._parse_event_fields_light") as parse,
            patch("handlers.message._try_handle_reaction_notice_delete", return_value=False),
            patch("handlers.message.helpers.get_post_records", side_effect=_no_meta),
            patch("handlers.message._build_file_context", return_value=([], [])),
            patch("handlers.message.helpers.run_sync_pipeline") as pipeline,
        ):
            parse.return_value = {
                "event_subtype": "message_deleted",
                "thread_ts": None,
                "msg_text": "",
                "channel_id": "C001",
                "user_id": "U001",
                "team_id": "T001",
                "ts": "1234567890.000001",
                "mentioned_users": [],
                "content_blocks": [],
                "reply_broadcast": False,
            }
            respond_to_message_event(body, MagicMock(), MagicMock(), {})
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        assert lookups["n"] == 2
        pipeline.assert_not_called()

    def test_duplicate_event_id_syncs_once(self, event_db):
        body = _message_body()
        client = MagicMock()
        logger = MagicMock()
        context = {}

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._build_file_context", return_value=([], [])),
        ):
            respond_to_message_event(body, client, logger, context)
            respond_to_message_event(body, client, logger, {})

        mock_new.assert_called_once()

    def test_failed_first_attempt_then_retry_syncs_once(self, event_db):
        body = _message_body()
        client = MagicMock()
        logger = MagicMock()
        calls = {"n": 0}

        def _boom(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("sync failed")

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._handle_new_post", side_effect=_boom),
            patch("handlers.message._build_file_context", return_value=([], [])),
        ):
            with pytest.raises(RuntimeError, match="sync failed"):
                respond_to_message_event(body, client, logger, {})
            respond_to_message_event(body, client, logger, {})

        assert calls["n"] == 2

    def test_edit_uses_claim(self, event_db):
        body = _message_body()
        body["event"]["subtype"] = "message_changed"
        body["event"]["message"] = {"ts": "1234567890.000001", "text": "edited"}
        client = MagicMock()
        logger = MagicMock()

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._parse_event_fields_light") as parse,
            patch("handlers.message._handle_message_edit") as mock_edit,
            patch("handlers.message._build_file_context", return_value=([], [])),
        ):
            parse.return_value = {
                "event_subtype": "message_changed",
                "thread_ts": None,
                "msg_text": "edited",
                "channel_id": "C001",
                "user_id": "U001",
                "team_id": "T001",
                "ts": "1234567890.000001",
                "mentioned_users": [],
                "content_blocks": [],
                "reply_broadcast": False,
            }
            respond_to_message_event(body, client, logger, {})
            respond_to_message_event(body, client, logger, {})

        mock_edit.assert_called_once()

    def test_delete_uses_claim(self, event_db):
        body = _message_body()
        body["event"]["subtype"] = "message_deleted"
        body["event"]["previous_message"] = {"ts": "1234567890.000001"}
        client = MagicMock()
        logger = MagicMock()

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._parse_event_fields_light") as parse,
            patch("handlers.message._try_handle_reaction_notice_delete", return_value=False),
            patch("handlers.message._handle_message_delete") as mock_delete,
            patch("handlers.message._build_file_context", return_value=([], [])),
        ):
            parse.return_value = {
                "event_subtype": "message_deleted",
                "thread_ts": None,
                "msg_text": "",
                "channel_id": "C001",
                "user_id": "U001",
                "team_id": "T001",
                "ts": "1234567890.000001",
                "mentioned_users": [],
                "content_blocks": [],
                "reply_broadcast": False,
            }
            respond_to_message_event(body, client, logger, {})
            respond_to_message_event(body, client, logger, {})

        mock_delete.assert_called_once()

    def test_thread_reply_uses_claim(self, event_db):
        body = _message_body()
        body["event"]["thread_ts"] = "1234567890.000000"
        client = MagicMock()
        logger = MagicMock()

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("handlers.message._parse_event_fields_light") as parse,
            patch("handlers.message._handle_thread_reply") as mock_reply,
            patch("handlers.message._build_file_context", return_value=([], [])),
        ):
            parse.return_value = {
                "event_subtype": None,
                "thread_ts": "1234567890.000000",
                "msg_text": "Hello",
                "channel_id": "C001",
                "user_id": "U001",
                "team_id": "T001",
                "ts": "1234567890.000001",
                "mentioned_users": [],
                "content_blocks": [],
                "reply_broadcast": False,
            }
            respond_to_message_event(body, client, logger, {})
            respond_to_message_event(body, client, logger, {})

        mock_reply.assert_called_once()

    def test_no_publish_targets_skips_enrich_and_still_creates_origin(self):
        client = MagicMock()
        logger = MagicMock()

        with (
            patch("handlers.message._is_own_bot_message", return_value=False),
            patch("helpers.iter_publish_targets", return_value=[]),
            patch("handlers.message._enrich_event_fields_for_sync") as enrich,
            patch("handlers.message._handle_new_post") as mock_new,
            patch("handlers.message._build_file_context") as build_fc,
        ):
            respond_to_message_event(_message_body(event_id=""), client, logger, {})

        enrich.assert_not_called()
        build_fc.assert_not_called()
        mock_new.assert_called_once()


class TestHandleReactionClaim:
    def _reaction_body(self):
        return {
            "event_id": "EvREACT1",
            "team_id": "T001",
            "event": {
                "type": "reaction_added",
                "user": "U001",
                "reaction": "thumbsup",
                "item": {"type": "message", "channel": "C001", "ts": "123.000001"},
            },
        }

    def test_reaction_removed_still_claims(self, event_db):
        body = self._reaction_body()
        body["event"]["type"] = "reaction_removed"
        with patch("handlers.reaction_event.run_claimed") as mock_run:
            handle_reaction(body, MagicMock(), MagicMock(), {})
        mock_run.assert_called_once()

    def test_reaction_without_post_meta_releases_claim_for_retry(self, event_db):
        body = self._reaction_body()
        records = [(MagicMock(), MagicMock(), MagicMock())]
        with (
            patch("handlers.reaction_event.helpers.get_own_bot_user_id", return_value="UBOT"),
            patch("handlers.reaction_event.helpers.get_post_records", side_effect=[[], records]),
            patch("handlers.reaction_event._sync_reaction_records") as mock_sync,
        ):
            handle_reaction(body, MagicMock(), MagicMock(), {})
            handle_reaction(body, MagicMock(), MagicMock(), {})
        mock_sync.assert_called_once()

    def test_unconfigured_channel_skips_post_meta_and_completes_claim(self, event_db):
        body = self._reaction_body()
        with (
            patch("handlers.reaction_event.helpers.get_own_bot_user_id", return_value="UBOT"),
            patch("helpers.channel_has_membership", return_value=False),
            patch("handlers.reaction_event.helpers.get_post_records") as post_meta,
            patch("handlers.reaction_event._sync_reaction_records") as mock_sync,
        ):
            handle_reaction(body, MagicMock(), MagicMock(), {})
            handle_reaction(body, MagicMock(), MagicMock(), {})
        post_meta.assert_not_called()
        mock_sync.assert_not_called()

    def test_reaction_added_claims_before_side_effects(self, event_db):
        body = self._reaction_body()
        records = [(MagicMock(), MagicMock(), MagicMock())]
        with (
            patch("handlers.reaction_event.helpers.get_own_bot_user_id", return_value="UBOT"),
            patch("handlers.reaction_event.helpers.get_post_records", return_value=records),
            patch("handlers.reaction_event.run_claimed") as mock_run,
        ):
            handle_reaction(body, MagicMock(), MagicMock(), {})
        mock_run.assert_called_once()

    def test_duplicate_reaction_event_id_skips_second_sync(self, event_db):
        body = self._reaction_body()
        records = [(MagicMock(), MagicMock(), MagicMock())]
        with (
            patch("handlers.reaction_event.helpers.get_own_bot_user_id", return_value="UBOT"),
            patch("handlers.reaction_event.helpers.get_post_records", return_value=records),
            patch("handlers.reaction_event._sync_reaction_records") as mock_sync,
        ):
            handle_reaction(body, MagicMock(), MagicMock(), {})
            handle_reaction(body, MagicMock(), MagicMock(), {})
        mock_sync.assert_called_once()


class TestHandleReactionEchoSkip:
    def test_target_echo_skips_fan_out_inside_claim(self, event_db):
        body = {
            "event_id": "EvECHO1",
            "team_id": "T2",
            "event": {
                "type": "reaction_added",
                "user": "U_MAPPED",
                "reaction": "thumbsup",
                "item": {"type": "message", "channel": "C_TGT", "ts": "200.0"},
            },
        }
        records = [(MagicMock(), MagicMock(), MagicMock())]
        with (
            patch("handlers.reaction_event.helpers.get_own_bot_user_id", return_value="UBOT"),
            patch("handlers.reaction_event.helpers.get_post_records", return_value=records),
            patch("handlers.reaction_event.helpers.take_user_action_echo", return_value=True) as take_mock,
            patch("handlers.reaction_event._sync_reaction_records") as sync_mock,
        ):
            handle_reaction(body, MagicMock(), MagicMock(), {})

        take_mock.assert_called_once()
        sync_mock.assert_not_called()

    def test_human_reaction_still_syncs(self, event_db):
        body = {
            "event_id": "EvHUMAN1",
            "team_id": "T2",
            "event": {
                "type": "reaction_added",
                "user": "U_HUMAN",
                "reaction": "thumbsup",
                "item": {"type": "message", "channel": "C_TGT", "ts": "200.0"},
            },
        }
        records = [(MagicMock(), MagicMock(), MagicMock())]
        with (
            patch("handlers.reaction_event.helpers.get_own_bot_user_id", return_value="UBOT"),
            patch("handlers.reaction_event.helpers.get_post_records", return_value=records),
            patch("handlers.reaction_event.helpers.take_user_action_echo", return_value=False),
            patch("handlers.reaction_event._sync_reaction_records") as sync_mock,
        ):
            handle_reaction(body, MagicMock(), MagicMock(), {})

        sync_mock.assert_called_once()

    def test_duplicate_event_id_still_skips_after_echo_take(self, event_db):
        body = {
            "event_id": "EvDUP1",
            "team_id": "T2",
            "event": {
                "type": "reaction_added",
                "user": "U_MAPPED",
                "reaction": "thumbsup",
                "item": {"type": "message", "channel": "C_TGT", "ts": "200.0"},
            },
        }
        records = [(MagicMock(), MagicMock(), MagicMock())]
        with (
            patch("handlers.reaction_event.helpers.get_own_bot_user_id", return_value="UBOT"),
            patch("handlers.reaction_event.helpers.get_post_records", return_value=records),
            patch("handlers.reaction_event.helpers.take_user_action_echo", return_value=True),
            patch("handlers.reaction_event._sync_reaction_records") as sync_mock,
        ):
            handle_reaction(body, MagicMock(), MagicMock(), {})
            handle_reaction(body, MagicMock(), MagicMock(), {})

        sync_mock.assert_not_called()


class TestEventIsNewFileShare:
    def test_upload_false_is_still_file_share(self):
        from helpers.files import event_is_new_file_share

        assert event_is_new_file_share({"subtype": "file_share", "upload": False}) is True

    def test_omitted_upload_is_new(self):
        from helpers.files import event_is_new_file_share

        assert event_is_new_file_share({"subtype": "file_share"}) is True

    def test_plain_message_is_not(self):
        from helpers.files import event_is_new_file_share

        assert event_is_new_file_share({"files": [{"id": "F1"}]}) is False
