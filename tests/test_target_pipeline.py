"""Tests for source envelopes and target Slack writes."""

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from helpers.envelope import build_envelope, get_post_id_for_post_records
from helpers.slack_write import (
    pick_write_token,
    slack_write_create,
    slack_write_delete,
    slack_write_edit,
)
from helpers.sync_apply import ApplyOutcome, apply_target
from helpers.sync_pipeline import run_sync_pipeline


def _workspace():
    return SimpleNamespace(id=2, team_id="T_TARGET")


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
        patch("helpers.slack_write.get_bot_token") as get_bot_token,
    ):
        assert pick_write_token(workspace, "U_TARGET") == ("xoxp-user", "U_TARGET")

    get_token.assert_called_once_with("T_TARGET", "U_TARGET")
    get_bot_token.assert_not_called()


def test_pick_write_token_falls_back_to_bot_customize():
    workspace = _workspace()
    with (
        patch("helpers.slack_write.get_user_token", return_value=None),
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
    ):
        assert pick_write_token(workspace, "U_TARGET") == ("xoxb-bot", None)


def test_user_token_create_posts_natively_and_remembers_echo():
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.post_message", return_value={"ts": "10.000001"}) as post,
        patch("helpers.slack_write.remember_user_action") as remember,
    ):
        ts, posted_as = slack_write_create(
            envelope=_envelope(),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
        )

    assert (ts, posted_as) == ("10.000001", "U_TARGET")
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
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value=None),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.post_message", return_value={"ts": "10.0"}) as post,
        patch("helpers.slack_write.remember_user_action") as remember,
    ):
        _ts, posted_as = slack_write_create(
            envelope=_envelope(),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
        )

    assert posted_as is None
    assert post.call_args.kwargs["bot_token"] == "xoxb-bot"
    assert post.call_args.kwargs["user_name"] == "Ada"
    assert post.call_args.kwargs["user_profile_url"] == "https://example/icon.png"
    remember.assert_not_called()


def test_user_owned_delete_skips_when_sticky_token_is_missing():
    meta = SimpleNamespace(ts=10.0, posted_as_user_id="U_TARGET")
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value=None),
        patch("helpers.slack_write.delete_message") as write,
    ):
        result = slack_write_delete(
            sync_channel=_sync_channel(),
            workspace=_workspace(),
            target_post_meta=meta,
        )

    assert result is False
    write.assert_not_called()


def test_user_owned_edit_falls_back_to_bot_when_sticky_token_is_missing():
    meta = SimpleNamespace(ts=10.0, posted_as_user_id="U_TARGET")
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value=None),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.slack_write.post_message") as write,
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.remember_user_action") as remember,
    ):
        result = slack_write_edit(
            envelope=_envelope(action="edit"),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
            target_post_meta=meta,
        )

    assert result is True
    assert write.call_args.kwargs["bot_token"] == "xoxb-bot"
    remember.assert_not_called()


def test_sticky_user_token_is_used_for_edit_and_delete_and_echoed():
    meta = SimpleNamespace(ts=10.0, posted_as_user_id="U_TARGET")
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
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
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
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

    assert result == ("20.0", "U_TARGET")
    post.assert_not_called()
    upload.assert_called_once()
    assert upload.call_args.kwargs["bot_token"] == "xoxp-user"
    assert upload.call_args.kwargs["thread_ts"] is None
    assert upload.call_args.kwargs["initial_comment"] is None
    assert upload.call_args.kwargs["username"] is None


def test_user_token_text_plus_file_embeds_caption_on_the_same_message():
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.post_message") as post,
        patch("helpers.slack_write.upload_files_to_slack", return_value=(None, "20.0")) as upload,
        patch("helpers.slack_write.remember_user_action"),
    ):
        result = slack_write_create(
            envelope=_envelope(text="Check this out", file_refs=[{"path": "/tmp/a.png", "name": "a.png"}]),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
        )

    assert result == ("20.0", "U_TARGET")
    post.assert_not_called()
    upload.assert_called_once()
    assert upload.call_args.kwargs["bot_token"] == "xoxp-user"
    assert upload.call_args.kwargs["initial_comment"] == "Check this out"
    assert upload.call_args.kwargs["username"] is None
    assert upload.call_args.kwargs["thread_ts"] is None


def test_user_token_thread_reply_file_embeds_on_the_parent_thread():
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.post_message") as post,
        patch("helpers.slack_write.upload_files_to_slack", return_value=(None, "35.0")) as upload,
        patch("helpers.slack_write.remember_user_action"),
    ):
        result = slack_write_create(
            envelope=_envelope(text="see attached", file_refs=[{"path": "/tmp/a.pdf", "name": "a.pdf"}]),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
            thread_ts="20.000000",
        )

    assert result == ("35.0", "U_TARGET")
    post.assert_not_called()
    assert upload.call_args.kwargs["thread_ts"] == "20.000000"
    assert upload.call_args.kwargs["initial_comment"] == "see attached"


def test_bot_block_body_with_file_is_one_share():
    source_client = MagicMock()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hello"}}]
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
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

    assert result == ("20.0", None)
    post.assert_not_called()
    assert upload.call_args.kwargs["thread_ts"] is None
    assert upload.call_args.kwargs["reply_broadcast"] is False
    assert upload.call_args.kwargs["initial_comment"] is None
    assert upload.call_args.kwargs["blocks"] == blocks
    assert upload.call_args.kwargs["username"] == "Ada"


def test_user_token_file_with_blocks_is_one_share():
    source_client = MagicMock()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hello"}}]
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.parse_mentioned_users", return_value=[]),
        patch("helpers.slack_write.apply_mentioned_users", side_effect=lambda text, *_a, **_k: text),
        patch("helpers.slack_write.resolve_channel_references", side_effect=lambda text, *_a, **_k: text),
        patch("helpers.slack_write.build_target_blocks", return_value=blocks),
        patch("helpers.slack_write.post_message") as post,
        patch("helpers.slack_write.upload_files_to_slack", return_value=(None, "20.0")) as upload,
        patch("helpers.slack_write.remember_user_action"),
    ):
        result = slack_write_create(
            envelope=_envelope(
                blocks=blocks,
                file_refs=[{"path": "/tmp/a.pdf", "name": "a.pdf"}],
            ),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
            source_client=source_client,
        )

    assert result == ("20.0", "U_TARGET")
    upload.assert_called_once()
    assert upload.call_args.kwargs["initial_comment"] is None
    assert upload.call_args.kwargs["blocks"] == blocks
    assert upload.call_args.kwargs["username"] is None
    post.assert_not_called()


def test_user_token_thread_broadcast_file_with_blocks_keeps_broadcast():
    source_client = MagicMock()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hello"}}]
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.parse_mentioned_users", return_value=[]),
        patch("helpers.slack_write.apply_mentioned_users", side_effect=lambda text, *_a, **_k: text),
        patch("helpers.slack_write.resolve_channel_references", side_effect=lambda text, *_a, **_k: text),
        patch("helpers.slack_write.build_target_blocks", return_value=blocks),
        patch("helpers.slack_write.post_message") as post,
        patch("helpers.slack_write.upload_files_to_slack", return_value=(None, "35.0")) as upload,
        patch("helpers.slack_write.remember_user_action"),
    ):
        result = slack_write_create(
            envelope=_envelope(
                blocks=blocks,
                file_refs=[{"path": "/tmp/a.pdf", "name": "a.pdf"}],
                reply_broadcast=True,
            ),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
            source_client=source_client,
            thread_ts="20.000000",
        )

    assert result == ("35.0", "U_TARGET")
    assert upload.call_args.kwargs["thread_ts"] == "20.000000"
    assert upload.call_args.kwargs["reply_broadcast"] is True
    assert upload.call_args.kwargs["blocks"] == blocks
    assert upload.call_args.kwargs["initial_comment"] is None
    assert upload.call_args.kwargs["username"] is None
    post.assert_not_called()


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
    if kind == "reaction":
        dropped = build_envelope(
            kind=kind,
            action=action,
            post_id="P1",
            source_channel_id="C_SOURCE",
            source_workspace_id=1,
            thread_post_id="PARENT",
            **extra,
        )
        assert "thread_post_id" not in dropped


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
    assert get_post_id_for_post_records(envelope) == "PARENT"
    assert get_post_id_for_post_records(_envelope()) is None
    assert get_post_id_for_post_records(_envelope(action="edit")) == "P1"
    assert get_post_id_for_post_records(_envelope(kind="reaction", action="add", reaction="eyes")) == "P1"


def test_apply_target_does_not_unthread_a_reply():
    with (
        patch("helpers.sync_apply.get_live_sync_channel", side_effect=lambda sc: sc),
        patch("helpers.sync_apply.slack_write_create") as write,
        patch("helpers.sync_apply.DbManager.create_records"),
    ):
        created = apply_target(
            _envelope(thread_post_id="PARENT"),
            _sync_channel(),
            _workspace(),
        )

    assert created.created == []
    write.assert_not_called()


def test_apply_target_records_sticky_posted_as_user():
    with (
        patch("helpers.sync_apply.get_live_sync_channel", side_effect=lambda sc: sc),
        patch(
            "helpers.sync_apply.slack_write_create",
            return_value=("10.0", "U_TARGET"),
        ),
        patch("helpers.sync_apply.DbManager.create_records") as persist,
    ):
        created = apply_target(
            _envelope(),
            _sync_channel(),
            _workspace(),
        )

    assert len(created.created) == 1
    assert created.created[0].posted_as_user_id == "U_TARGET"
    assert created.created[0].source_user_id == "U_SOURCE"
    persist.assert_called_once()


def test_run_sync_pipeline_skips_peer_when_origin_not_allowed():
    stub_ws = SimpleNamespace(id=99, team_id="TSTUB", deleted_at=None, instance_id="peer")
    fed = SimpleNamespace(instance_id="peer", trust_status="trusted")
    targets = [(_sync_channel("CREMOTE", row_id=3), stub_ws)]
    with (
        patch("helpers.sync_pipeline.iter_publish_targets", return_value=targets),
        patch("helpers.sync_pipeline.is_stub_workspace", return_value=True),
        patch("helpers.sync_pipeline.is_local_workspace", return_value=False),
        patch("helpers.sync_pipeline.peer_for_workspace", return_value=fed),
        patch("helpers.sync_pipeline.peer_is_trusted", return_value=True),
        patch("helpers.sync_pipeline.workspace_allowed_for_peer", return_value=False),
        patch("helpers.workspace.get_workspace_by_id", return_value=SimpleNamespace(id=1)),
        patch("helpers.sync_pipeline.get_post_records_for_post_id", return_value=[]),
        patch("federation.deliver.deliver_remote") as deliver,
    ):
        result = run_sync_pipeline(
            _envelope(source_workspace_id=1),
            source_channel_id="C_SOURCE",
        )

    assert result == []
    deliver.assert_not_called()


def test_run_sync_pipeline_applies_once_per_unique_target():
    targets = [
        (_sync_channel("C_ONE", row_id=1), SimpleNamespace(id=10, team_id="T1", deleted_at=None)),
        (_sync_channel("C_TWO", row_id=2), SimpleNamespace(id=20, team_id="T2", deleted_at=None)),
    ]
    with (
        patch("helpers.sync_pipeline.iter_publish_targets", return_value=targets),
        patch("helpers.sync_pipeline.is_local_workspace", return_value=True),
        patch("helpers.sync_pipeline.get_post_records_for_post_id", return_value=[]),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch(
            "helpers.sync_pipeline.apply_target",
            side_effect=[ApplyOutcome(created=["one"]), ApplyOutcome(created=["two"])],
        ) as apply,
    ):
        result = run_sync_pipeline(
            _envelope(),
            source_channel_id="C_SOURCE",
        )

    assert result == ["one", "two"]
    assert [item.args[1].channel_id for item in apply.call_args_list] == ["C_ONE", "C_TWO"]


def test_federation_images_accept_block_kit_or_url_payload():
    from federation.deliver import federation_image_payloads

    blocks = [{"type": "image", "image_url": "https://gif.example/a.gif", "alt_text": "gif"}]
    payload = [{"url": "https://gif.example/a.gif", "alt_text": "gif"}]
    assert federation_image_payloads(blocks) == payload
    assert federation_image_payloads(payload) == payload


def test_inbound_images_restore_slack_image_blocks():
    from federation.api import _inbound_image_blocks

    payload = [{"url": "https://gif.example/a.gif", "alt_text": "gif"}]
    assert _inbound_image_blocks(payload) == [
        {"type": "image", "image_url": "https://gif.example/a.gif", "alt_text": "gif"}
    ]


def test_user_token_file_create_remembers_file_ids_from_after_upload():
    def fake_upload(**kwargs):
        after = kwargs.get("after_upload")
        res = {"file": {"id": "F99"}, "files": [{"id": "F99"}]}
        if after:
            after(["F99"])
        after_ts = kwargs.get("after_share_ts")
        if after_ts:
            after_ts("200.000000")
        return res, "200.000000"

    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.post_message") as post,
        patch("helpers.slack_write.upload_files_to_slack", side_effect=fake_upload),
        patch("helpers.slack_write.remember_user_action") as remember,
    ):
        slack_write_create(
            envelope=_envelope(text="Check this out", file_refs=[{"path": "/tmp/a.png", "name": "a.png"}]),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
        )

    post.assert_not_called()
    assert remember.call_args_list == [
        call("T_TARGET", "U_TARGET", "file", "F99"),
        call("T_TARGET", "U_TARGET", "message", "C_TARGET:200.000000"),
    ]


def test_caption_only_file_falls_back_to_bot_upload_when_user_token_fails():
    from slack_sdk.errors import SlackApiError

    user_err = SlackApiError("denied", {"ok": False, "error": "invalid_auth"})
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch(
            "helpers.slack_write.upload_files_to_slack",
            side_effect=[user_err, (None, "30.0")],
        ) as upload,
        patch("helpers.slack_write.remember_user_action") as remember,
    ):
        ts, posted_as = slack_write_create(
            envelope=_envelope(text="", file_refs=[{"path": "/tmp/a.pdf", "name": "a.pdf"}]),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
        )

    assert (ts, posted_as) == ("30.0", None)
    assert upload.call_args_list[0].kwargs["bot_token"] == "xoxp-user"
    assert upload.call_args_list[0].kwargs["username"] is None
    assert upload.call_args_list[1].kwargs["bot_token"] == "xoxb-bot"
    assert upload.call_args_list[1].kwargs["username"] == "Ada"
    assert upload.call_args_list[1].kwargs["icon_url"] == "https://example/icon.png"
    assert upload.call_args_list[1].kwargs["initial_comment"] is None
    remember.assert_not_called()


def test_empty_create_does_not_post():
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value=None),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch(
            "helpers.slack_write.get_display_name_and_icon_for_synced_message",
            return_value=("Ada", "https://icon", True, None),
        ),
        patch("helpers.slack_write.post_message") as post,
        patch("helpers.slack_write.notify_source_user_error") as notify,
    ):
        result = slack_write_create(
            envelope=_envelope(text="", mapped_user_id=None),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
        )

    assert result == (None, None)
    post.assert_not_called()
    notify.assert_not_called()


def test_omitted_file_share_ts_does_not_dm():
    def fake_upload(**kwargs):
        after = kwargs.get("after_upload")
        if after:
            after(["F99"])
        return None, None

    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.post_message") as post,
        patch("helpers.slack_write.upload_files_to_slack", side_effect=fake_upload),
        patch("helpers.slack_write.remember_user_action") as remember,
        patch("helpers.slack_write.remember_pending_file_share") as pending,
        patch("helpers.slack_write.notify_source_user_error") as notify,
    ):
        result = slack_write_create(
            envelope=_envelope(text="", file_refs=[{"path": "/tmp/a.pdf", "name": "a.pdf"}]),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
        )

    assert result == (None, "U_TARGET")
    post.assert_not_called()
    notify.assert_not_called()
    pending.assert_called_once_with(
        "T_TARGET",
        "C_TARGET",
        "F99",
        "P1",
        sync_channel_id=22,
        source_user_id="U_SOURCE",
        source_workspace_id=1,
        posted_as_user_id="U_TARGET",
    )
    assert call("T_TARGET", "U_TARGET", "file", "F99") in remember.call_args_list


def test_file_upload_failure_dms_source_user():
    source_client = MagicMock()
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value="xoxp-user"),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.post_message") as post,
        patch(
            "helpers.slack_write.upload_files_to_slack",
            side_effect=RuntimeError("file upload POST returned 500"),
        ),
        patch("helpers.slack_write.notify_source_user_error") as notify,
    ):
        result = slack_write_create(
            envelope=_envelope(text="", file_refs=[{"path": "/tmp/a.pdf", "name": "a.pdf"}]),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
            source_client=source_client,
        )

    assert result == (None, None)
    post.assert_not_called()
    notify.assert_called_once()
    assert notify.call_args.kwargs["source_client"] is source_client
    assert notify.call_args.kwargs["source_user_id"] == "U_SOURCE"


def test_file_upload_slack_error_uses_error_code():
    from slack_sdk.errors import SlackApiError

    source_client = MagicMock()
    with (
        patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"),
        patch("helpers.slack_write.get_user_token", return_value=None),
        patch("helpers.slack_write.WebClient"),
        patch("helpers.workspace.get_workspace_by_id", return_value=None),
        patch("helpers.slack_write.post_message") as post,
        patch(
            "helpers.slack_write.upload_files_to_slack",
            side_effect=SlackApiError("denied", {"ok": False, "error": "storage_limit_reached"}),
        ),
        patch("helpers.slack_write.notify_source_user_error") as notify,
    ):
        result = slack_write_create(
            envelope=_envelope(text="", file_refs=[{"path": "/tmp/a.pdf", "name": "a.pdf"}]),
            sync_channel=_sync_channel(),
            workspace=_workspace(),
            source_client=source_client,
        )

    assert result == (None, None)
    post.assert_not_called()
    notify.assert_called_once()
    assert notify.call_args.kwargs["details"]["error"] == "storage_limit_reached"
    assert "file storage is full" in notify.call_args.kwargs["summary"]
