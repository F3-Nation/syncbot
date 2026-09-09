"""Tests for source envelopes and target Slack writes."""

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from helpers.envelope import build_envelope, post_id_for_post_records
from helpers.slack_write import (
    pick_write_token,
    slack_write_create,
    slack_write_delete,
    slack_write_edit,
)
from helpers.sync_apply import apply_target
from helpers.sync_pipeline import run_sync_pipeline


def _workspace():
    return SimpleNamespace(id=2, team_id="T_TARGET", bot_token="encrypted-bot")


def _sync_channel(channel_id="C_TARGET", *, subscribes=True, row_id=22):
    return SimpleNamespace(id=row_id, channel_id=channel_id, sync_id=7, subscribes=subscribes)


def _envelope(**overrides):
    envelope = {
        "kind": "message",
        "action": "create",
        "post_id": "P1",
        "source_workspace_id": 1,
        "source_user_id": "U_SOURCE",
        "mapped_user_id": "U_TARGET",
        "user_name": "Ada",
        "user_avatar_url": "https://example/icon.png",
        "workspace_name": "Source",
        "text": "hello",
    }
    envelope.update(overrides)
    return envelope


def test_pick_write_token_prefers_mapped_users_token():
    workspace = _workspace()
    with (
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user") as get_token,
        patch("helpers.slack_write.decrypt_bot_token") as decrypt,
    ):
        assert pick_write_token(workspace, "U_TARGET") == ("xoxp-user", "U_TARGET")

    get_token.assert_called_once_with("T_TARGET", "U_TARGET")
    decrypt.assert_not_called()


def test_pick_write_token_falls_back_to_bot_customize():
    workspace = _workspace()
    with (
        patch("helpers.slack_write.get_user_token", return_value=None),
        patch("helpers.slack_write.decrypt_bot_token", return_value="xoxb-bot"),
    ):
        assert pick_write_token(workspace, "U_TARGET") == ("xoxb-bot", None)


def test_user_token_create_posts_natively_and_remembers_echo():
    with (
        patch("helpers.slack_write.decrypt_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.post_message", return_value={"ts": "10.000001"}) as post,
        patch("helpers.slack_write.remember_user_action") as remember,
    ):
        ts, split_ts, posted_as = slack_write_create(
            envelope=_envelope(),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
        )

    assert (ts, split_ts, posted_as) == ("10.000001", None, "U_TARGET")
    kwargs = post.call_args.kwargs
    assert kwargs["bot_token"] == "xoxp-user"
    assert "user_name" not in kwargs
    assert "user_profile_url" not in kwargs
    remember.assert_called_once_with(
        "T_TARGET",
        "U_TARGET",
        "message",
        "C_TARGET:10.000001",
    )


def test_no_user_token_create_uses_bot_customization_without_echo():
    with (
        patch("helpers.slack_write.decrypt_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value=None),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.post_message", return_value={"ts": "10.0"}) as post,
        patch("helpers.slack_write.remember_user_action") as remember,
    ):
        _ts, _split_ts, posted_as = slack_write_create(
            envelope=_envelope(),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
        )

    assert posted_as is None
    assert post.call_args.kwargs["bot_token"] == "xoxb-bot"
    assert post.call_args.kwargs["user_name"] == "Ada"
    assert post.call_args.kwargs["user_profile_url"] == "https://example/icon.png"
    remember.assert_not_called()


@pytest.mark.parametrize("operation", ["edit", "delete"])
def test_user_owned_edit_and_delete_skip_when_sticky_token_is_missing(operation):
    meta = SimpleNamespace(ts=10.0, posted_as_user_id="U_TARGET")
    common = (
        patch("helpers.slack_write.decrypt_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value=None),
    )
    with common[0], common[1]:
        if operation == "edit":
            with (
                patch("helpers.slack_write.WebClient"),
                patch("helpers.slack_write.post_message") as write,
                patch("helpers.workspace.get_workspace_by_id", return_value=None),
            ):
                result = slack_write_edit(
                    envelope=_envelope(action="edit"),
                    sync_channel=_sync_channel(),
                    workspace=_workspace(),
                    target_post_meta=meta,
                )
        else:
            with patch("helpers.slack_write.delete_message") as write:
                result = slack_write_delete(
                    sync_channel=_sync_channel(),
                    workspace=_workspace(),
                    target_post_meta=meta,
                )

    assert result is False
    write.assert_not_called()


def test_sticky_user_token_is_used_for_edit_and_delete_and_echoed():
    meta = SimpleNamespace(ts=10.0, posted_as_user_id="U_TARGET")
    with (
        patch("helpers.slack_write.decrypt_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.post_message") as post,
        patch("helpers.slack_write.delete_message") as delete,
        patch("helpers.slack_write.remember_user_action") as remember,
    ):
        assert slack_write_edit(
            envelope=_envelope(action="edit"),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
            target_post_meta=meta,
        )
        assert slack_write_delete(
            sync_channel=_sync_channel(),
            workspace=_workspace(),
            target_post_meta=meta,
        )

    assert post.call_args.kwargs["bot_token"] == "xoxp-user"
    assert delete.call_args.kwargs["bot_token"] == "xoxp-user"
    assert remember.call_args_list == [
        call("T_TARGET", "U_TARGET", "message", "C_TARGET:10.000000"),
        call("T_TARGET", "U_TARGET", "message", "C_TARGET:10.000000"),
    ]


def test_ordinary_file_share_is_one_upload_without_message_post():
    with (
        patch("helpers.slack_write.decrypt_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.post_message") as post,
        patch("helpers.slack_write.upload_files_to_slack", return_value=(None, "20.0")) as upload,
        patch("helpers.slack_write.remember_user_action"),
    ):
        result = slack_write_create(
            envelope=_envelope(text="", file_refs=[{"path": "/tmp/a.pdf", "name": "a.pdf"}]),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
        )

    assert result == ("20.0", None, "U_TARGET")
    post.assert_not_called()
    upload.assert_called_once()
    assert upload.call_args.kwargs["bot_token"] == "xoxp-user"
    assert upload.call_args.kwargs["thread_ts"] is None


def test_block_body_with_file_splits_and_broadcasts_file_reply():
    source_client = MagicMock()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hello"}}]
    with (
        patch("helpers.slack_write.decrypt_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value=None),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.parse_mentioned_users", return_value=[]),
        patch("helpers.slack_write.apply_mentioned_users", side_effect=lambda text, *_a, **_k: text),
        patch("helpers.slack_write.resolve_channel_references", side_effect=lambda text, *_a, **_k: text),
        patch("helpers.slack_write.build_target_blocks", return_value=blocks),
        patch("helpers.slack_write.post_message", return_value={"ts": "10.0"}) as post,
        patch("helpers.slack_write.upload_files_to_slack", return_value=(None, "20.0")) as upload,
    ):
        result = slack_write_create(
            envelope=_envelope(
                blocks=blocks,
                file_refs=[{"path": "/tmp/a.pdf", "name": "a.pdf"}],
                reply_broadcast=False,
            ),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
            source_client=source_client,
        )

    assert result == ("10.0", "20.0", None)
    assert post.call_args.kwargs["blocks"] == blocks
    assert upload.call_args.kwargs["thread_ts"] == "10.0"
    assert upload.call_args.kwargs["reply_broadcast"] is True


@pytest.mark.parametrize(
    ("kind", "action", "extra", "expected"),
    [
        ("message", "create", {"text": "new"}, {"text": "new"}),
        ("message", "edit", {"text": "changed"}, {"text": "changed"}),
        ("message", "delete", {"text": "ignored"}, {}),
        ("reaction", "add", {"reaction": "eyes"}, {"reaction": "eyes"}),
        ("reaction", "remove", {"reaction": "eyes"}, {"reaction": "eyes"}),
    ],
)
def test_build_envelope_kinds_and_actions(kind, action, extra, expected):
    envelope = build_envelope(
        kind=kind,
        action=action,
        post_id="P1",
        source_channel_id="C_SOURCE",
        source_workspace_id=1,
        **extra,
    )

    assert envelope["kind"] == kind
    assert envelope["action"] == action
    assert envelope["post_id"] == "P1"
    assert envelope["source_channel_id"] == "C_SOURCE"
    for key, value in expected.items():
        assert envelope[key] == value
    if action == "delete":
        assert "text" not in envelope


def test_build_envelope_carries_post_id_and_people():
    envelope = build_envelope(
        kind="message",
        action="create",
        post_id="P2",
        source_channel_id="C_SOURCE",
        source_workspace_id=1,
        source_team_id="T_SOURCE",
        source_sync_channel_id=11,
        people=[{"user_id": "U1", "name": "Ada"}],
        text="reply",
        thread_post_id="PARENT",
    )

    assert envelope["source_sync_channel_id"] == 11
    assert envelope["source_team_id"] == "T_SOURCE"
    assert envelope["people"] == [{"user_id": "U1", "name": "Ada"}]
    assert envelope["thread_post_id"] == "PARENT"
    assert post_id_for_post_records(envelope) == "PARENT"
    assert post_id_for_post_records(_envelope()) is None
    assert post_id_for_post_records(_envelope(action="edit")) == "P1"
    assert post_id_for_post_records(_envelope(kind="reaction", action="add", reaction="eyes")) == "P1"


def test_apply_target_does_not_unthread_a_reply():
    with patch("helpers.sync_apply.slack_write_create") as write:
        created = apply_target(
            _envelope(thread_post_id="PARENT"),
            _sync_channel(),
            _workspace(),
        )

    assert created == []
    write.assert_not_called()


def test_apply_target_records_sticky_posted_as_user():
    with patch(
        "helpers.sync_apply.slack_write_create",
        return_value=("10.0", None, "U_TARGET"),
    ):
        created = apply_target(
            _envelope(),
            _sync_channel(),
            _workspace(),
        )

    assert len(created) == 1
    assert created[0].posted_as_user_id == "U_TARGET"
    assert created[0].source_user_id == "U_SOURCE"


def test_run_sync_pipeline_applies_once_per_unique_target():
    targets = [
        (_sync_channel("C_ONE", row_id=1), SimpleNamespace(id=10, team_id="T1")),
        (_sync_channel("C_TWO", row_id=2), SimpleNamespace(id=20, team_id="T2")),
    ]
    with (
        patch("helpers.sync_pipeline.iter_publish_targets", return_value=targets),
        patch("helpers.sync_pipeline.get_federated_workspace_for_sync", return_value=None),
        patch("helpers.sync_pipeline.get_post_records_for_post_id", return_value=[]),
        patch("helpers.sync_pipeline.apply_target", side_effect=[["one"], ["two"]]) as apply,
    ):
        result = run_sync_pipeline(
            _envelope(),
            source_channel_id="C_SOURCE",
        )

    assert result == ["one", "two"]
    assert [item.args[1].channel_id for item in apply.call_args_list] == ["C_ONE", "C_TWO"]


def test_federation_images_accept_block_kit_or_wire_shape():
    from federation.deliver import federation_image_payloads

    blocks = [{"type": "image", "image_url": "https://gif.example/a.gif", "alt_text": "gif"}]
    wire = [{"url": "https://gif.example/a.gif", "alt_text": "gif"}]
    assert federation_image_payloads(blocks) == wire
    assert federation_image_payloads(wire) == wire


def test_caption_only_file_falls_back_to_bot_upload_when_user_token_fails():
    from slack_sdk.errors import SlackApiError

    user_err = SlackApiError("denied", {"ok": False, "error": "invalid_auth"})
    with (
        patch("helpers.slack_write.decrypt_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch(
            "helpers.slack_write.upload_files_to_slack",
            side_effect=[user_err, (None, "30.0")],
        ) as upload,
        patch("helpers.slack_write.remember_user_action") as remember,
    ):
        ts, split_ts, posted_as = slack_write_create(
            envelope=_envelope(text="", file_refs=[{"path": "/tmp/a.pdf", "name": "a.pdf"}]),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
        )

    assert (ts, split_ts, posted_as) == ("30.0", None, None)
    assert upload.call_args_list[0].kwargs["bot_token"] == "xoxp-user"
    assert upload.call_args_list[1].kwargs["bot_token"] == "xoxb-bot"
    remember.assert_not_called()
