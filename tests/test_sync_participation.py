"""Publish/subscribe participation, fan-in, dedupe, and copy-guard tests."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from db import DbManager, schemas
from helpers.post_meta import get_post_records, get_publishing_post_records, post_meta_exists_for_channel_ts
from helpers.sync_apply import ApplyOutcome
from helpers.sync_participation import (
    already_subscribed_to_source,
    channel_has_membership,
    channel_subscribes,
    get_origin_sync_channel,
    invalidate_channel_memberships,
    invalidate_sync_fanout_for_syncs,
    iter_publish_targets,
    origin_publishes_anywhere,
    parse_participation_flags,
    participation_label,
)
from helpers.sync_pipeline import run_sync_pipeline
from helpers.user_action_echo import post_meta_ts


def test_participation_flags():
    assert parse_participation_flags("publish_only") == (True, False)
    assert parse_participation_flags("subscribe_only") == (False, True)
    assert parse_participation_flags("publish_and_subscribe") == (True, True)
    assert parse_participation_flags("subscribe_and_publish") == (True, True)
    assert parse_participation_flags(None) == (True, True)


def test_participation_label():
    assert participation_label(SimpleNamespace(publishes=True, subscribes=True)) == "Publish and Subscribe"
    assert participation_label(SimpleNamespace(publishes=True, subscribes=False)) == "Publish only"
    assert participation_label(SimpleNamespace(publishes=False, subscribes=True)) == "Subscribe only"
    assert participation_label(None) == "Publish and Subscribe"


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database
    from helpers._cache import clear_all_caches

    url = f"sqlite:///{tmp_path / 'participation.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_session = db_mod.GLOBAL_SESSION
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SESSION = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            from federation import core as federation_core

            federation_core._INSTANCE_ID = None
            federation_core._cached_private_key = None
            federation_core._cached_public_pem = None
            federation_core.get_or_create_instance_keypair()
            clear_all_caches()
            yield
        finally:
            clear_all_caches()
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SESSION = old_session
            db_mod.GLOBAL_SCHEMA = old_schema


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def _workspace(team_id: str):
    from federation.core import get_instance_id

    return DbManager.create_record(
        schemas.Workspace(
            team_id=team_id,
            workspace_name=team_id,
            instance_id=get_instance_id(),
        )
    )


def _sync(publisher, title: str):
    return DbManager.create_record(
        schemas.Sync(
            title=title,
            sync_mode="group",
            uid=str(uuid4()),
        )
    )


def _channel(sync, workspace, channel_id: str, *, publishes: bool, subscribes: bool, status="active"):
    return DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=workspace.id,
            channel_id=channel_id,
            status=status,
            publishes=publishes,
            subscribes=subscribes,
            created_at=_now(),
        )
    )


def _target_ids(channel_id: str) -> list[str]:
    return [sc.channel_id for sc, _workspace in iter_publish_targets(channel_id)]


def test_subscribe_only_never_originates_messages_or_reactions(real_db):
    ws = _workspace("T_SUB")
    sync = _sync(ws, "subscribe-only")
    _channel(sync, ws, "C_SUB", publishes=False, subscribes=True)

    assert not origin_publishes_anywhere("C_SUB")
    assert get_origin_sync_channel("C_SUB") is None
    assert iter_publish_targets("C_SUB") == []


def test_publish_only_does_not_receive_from_another_publisher(real_db):
    a, b = _workspace("T_A"), _workspace("T_B")
    sync = _sync(a, "publishers")
    _channel(sync, a, "C_A", publishes=True, subscribes=False)
    b_channel = _channel(sync, b, "C_B", publishes=True, subscribes=False)

    assert not channel_subscribes(b_channel)
    assert "C_B" not in _target_ids("C_A")


def test_publish_and_subscribe_receives(real_db):
    a, b = _workspace("T_A"), _workspace("T_B")
    sync = _sync(a, "both")
    _channel(sync, a, "C_A", publishes=True, subscribes=False)
    _channel(sync, b, "C_B", publishes=True, subscribes=True)

    assert _target_ids("C_A") == ["C_B"]


def test_pause_invalidates_cached_publish_targets(real_db):
    a, b = _workspace("T_A"), _workspace("T_B")
    sync = _sync(a, "live")
    _channel(sync, a, "C_A", publishes=True, subscribes=False)
    b_channel = _channel(sync, b, "C_B", publishes=False, subscribes=True)

    assert _target_ids("C_A") == ["C_B"]
    DbManager.update_record(schemas.SyncChannel, b_channel.id, {"status": "paused"})
    assert _target_ids("C_A") == ["C_B"]
    invalidate_channel_memberships("C_A")
    assert _target_ids("C_A") == []


def test_invalidate_sync_fanout_clears_publisher_cache_when_subscriber_pauses(real_db):
    a, b = _workspace("T_A"), _workspace("T_B")
    sync = _sync(a, "live")
    _channel(sync, a, "C_A", publishes=True, subscribes=False)
    b_channel = _channel(sync, b, "C_B", publishes=False, subscribes=True)

    assert _target_ids("C_A") == ["C_B"]
    DbManager.update_record(schemas.SyncChannel, b_channel.id, {"status": "paused"})
    invalidate_sync_fanout_for_syncs([sync.id])
    assert _target_ids("C_A") == []


def test_apply_target_skips_paused_membership_even_with_stale_cache(real_db):
    from helpers.sync_apply import apply_target

    a, b = _workspace("T_A"), _workspace("T_B")
    sync = _sync(a, "live")
    _channel(sync, a, "C_A", publishes=True, subscribes=False)
    b_channel = _channel(sync, b, "C_B", publishes=False, subscribes=True)
    stale, workspace = iter_publish_targets("C_A")[0]
    assert stale.status == "active"
    DbManager.update_record(schemas.SyncChannel, b_channel.id, {"status": "paused"})
    with patch("helpers.sync_apply.slack_write_create") as write:
        created = apply_target(
            {"kind": "message", "action": "create", "post_id": "P1", "source_workspace_id": a.id},
            stale,
            workspace,
        )
    assert created.created == []
    write.assert_not_called()


def test_purge_subscriber_invalidates_publisher_publish_targets(real_db):
    from helpers.sync_cleanup import purge_sync_channels

    a, b = _workspace("T_A"), _workspace("T_B")
    sync = _sync(a, "live")
    _channel(sync, a, "C_A", publishes=True, subscribes=False)
    b_channel = _channel(sync, b, "C_B", publishes=False, subscribes=True)

    assert _target_ids("C_A") == ["C_B"]
    purge_sync_channels([b_channel])
    assert _target_ids("C_A") == []


def test_two_publishers_fan_into_one_subscriber_without_cross_posts(real_db):
    a, b, c = _workspace("T_A"), _workspace("T_B"), _workspace("T_C")
    sync = _sync(a, "fan-in")
    _channel(sync, a, "C_A", publishes=True, subscribes=False)
    _channel(sync, b, "C_B", publishes=True, subscribes=False)
    _channel(sync, c, "C_C", publishes=False, subscribes=True)

    assert _target_ids("C_A") == ["C_C"]
    assert _target_ids("C_B") == ["C_C"]


def test_one_channel_can_subscribe_to_two_distinct_sources(real_db):
    a, b, c = _workspace("T_A"), _workspace("T_B"), _workspace("T_C")
    first, second = _sync(a, "first"), _sync(c, "second")
    _channel(first, a, "C_A", publishes=True, subscribes=False)
    _channel(first, b, "C_B", publishes=False, subscribes=True)
    _channel(second, c, "C_C", publishes=True, subscribes=False)
    _channel(second, b, "C_B", publishes=False, subscribes=True)

    assert _target_ids("C_A") == ["C_B"]
    assert _target_ids("C_C") == ["C_B"]
    assert not already_subscribed_to_source(
        workspace_id=b.id,
        source_workspace_id=c.id,
        source_channel_id="C_OTHER",
    )


def test_duplicate_published_source_is_rejected(real_db):
    a, b = _workspace("T_A"), _workspace("T_B")
    sync = _sync(a, "source")
    _channel(sync, a, "C_A", publishes=True, subscribes=False)
    _channel(sync, b, "C_B", publishes=False, subscribes=True)

    assert already_subscribed_to_source(
        workspace_id=b.id,
        source_workspace_id=a.id,
        source_channel_id="C_A",
    )
    assert not already_subscribed_to_source(
        workspace_id=b.id,
        source_workspace_id=a.id,
        source_channel_id="C_A",
        exclude_sync_id=sync.id,
    )


@pytest.mark.parametrize(
    ("kind", "action", "extra"),
    [
        ("message", "create", {"text": "hello"}),
        ("reaction", "add", {"reaction": "thumbsup"}),
    ],
)
def test_pipeline_does_not_hop_from_target_into_its_other_sync(real_db, kind, action, extra):
    a, b, c = _workspace("T_A"), _workspace("T_B"), _workspace("T_C")
    first, second = _sync(a, "A to B"), _sync(b, "B to C")
    source = _channel(first, a, "C_A", publishes=True, subscribes=False)
    b_first = _channel(first, b, "C_B", publishes=False, subscribes=True)
    _channel(second, b, "C_B", publishes=True, subscribes=False)
    _channel(second, c, "C_C", publishes=False, subscribes=True)
    envelope = {
        "kind": kind,
        "action": action,
        "post_id": "P1",
        "source_workspace_id": a.id,
        **extra,
    }
    if action != "create":
        DbManager.create_record(schemas.PostMeta(post_id="P1", sync_channel_id=source.id, ts=1.0))
        DbManager.create_record(schemas.PostMeta(post_id="P1", sync_channel_id=b_first.id, ts=2.0))

    with patch("helpers.sync_pipeline.apply_target", return_value=ApplyOutcome()) as apply:
        run_sync_pipeline(
            envelope,
            source_channel_id="C_A",
            source_sync_channel=source,
        )

    assert [call.args[1].channel_id for call in apply.call_args_list] == ["C_B"]


def test_thread_reply_stays_on_original_post_records_not_sibling_sync(real_db):
    """A reply on a copy in a Channel that also publishes elsewhere must not unthread."""
    ws_a, ws_b, ws_c = _workspace("T_A"), _workspace("T_B"), _workspace("T_C")
    sync_a, sync_b = _sync(ws_a, "a to b"), _sync(ws_c, "c to a")
    _ch_a1 = _channel(sync_a, ws_a, "C_A", publishes=True, subscribes=True)
    _channel(sync_a, ws_b, "C_B", publishes=True, subscribes=True)
    ch_a2 = _channel(sync_b, ws_a, "C_A", publishes=True, subscribes=True)
    ch_c = _channel(sync_b, ws_c, "C_C", publishes=True, subscribes=True)
    DbManager.create_record(schemas.PostMeta(post_id="PARENT", sync_channel_id=ch_c.id, ts=post_meta_ts("10.000000")))
    DbManager.create_record(schemas.PostMeta(post_id="PARENT", sync_channel_id=ch_a2.id, ts=post_meta_ts("20.000000")))

    envelope = {
        "kind": "message",
        "action": "create",
        "post_id": "REPLY",
        "source_workspace_id": ws_a.id,
        "source_sync_channel_id": ch_a2.id,
        "thread_post_id": "PARENT",
        "text": "reply in the copy thread",
    }

    with patch("helpers.sync_pipeline.apply_target", return_value=ApplyOutcome()) as apply:
        run_sync_pipeline(
            envelope,
            source_channel_id="C_A",
            source_sync_channel=ch_a2,
        )

    assert [call.args[1].channel_id for call in apply.call_args_list] == ["C_C"]
    assert apply.call_args.kwargs["thread_ts"] == "10.000000"
    assert "C_B" not in [call.args[1].channel_id for call in apply.call_args_list]
    assert _ch_a1.id != ch_a2.id


def _federated_announcements():
    local = _workspace("T_LOCAL")
    now = _now()
    peer = DbManager.create_record(
        schemas.Instance(
            instance_id="a" * 64,
            webhook_url="https://peer.example/api/federation",
            public_key="pem",
            private_key_encrypted=None,
            status="active",
            trust_status="trusted",
            name="Partner Org",
            created_at=now,
        )
    )
    stub = DbManager.create_record(
        schemas.Workspace(team_id="T_STUB", workspace_name="Workspace B", instance_id=peer.instance_id)
    )
    sync = _sync(local, "Announcements")
    local_sc = _channel(sync, local, "C_LOCAL", publishes=True, subscribes=True)
    remote_sc = _channel(sync, stub, "C_REMOTE", publishes=True, subscribes=True)
    return local, local_sc, remote_sc


def test_thread_reply_delivers_to_stub_without_parent_post_meta(real_db):
    """Peer looks up the parent; inbound 409s if that PostMeta is missing."""
    _local, local_sc, remote_sc = _federated_announcements()
    DbManager.create_record(
        schemas.PostMeta(post_id="PARENT", sync_channel_id=local_sc.id, ts=post_meta_ts("10.000000"))
    )

    envelope = {
        "kind": "message",
        "action": "create",
        "post_id": "REPLY",
        "source_channel_id": "C_LOCAL",
        "source_workspace_id": local_sc.workspace_id,
        "source_sync_channel_id": local_sc.id,
        "thread_post_id": "PARENT",
        "text": "reply",
    }
    with (
        patch("federation.deliver.deliver_remote", return_value=[]) as deliver,
        patch("helpers.sync_pipeline.apply_target") as apply,
    ):
        run_sync_pipeline(envelope, source_channel_id="C_LOCAL", source_sync_channel=local_sc)

    deliver.assert_called_once()
    assert deliver.call_args.args[2].channel_id == "C_REMOTE"
    assert deliver.call_args.args[0]["thread_post_id"] == "PARENT"
    assert deliver.call_args.args[0].get("target_ts") is None
    apply.assert_not_called()
    assert remote_sc.channel_id == "C_REMOTE"


def test_thread_reply_delivers_to_stub_with_parent_post_meta(real_db):
    _local, local_sc, remote_sc = _federated_announcements()
    DbManager.create_record(
        schemas.PostMeta(post_id="PARENT", sync_channel_id=local_sc.id, ts=post_meta_ts("10.000000"))
    )
    DbManager.create_record(
        schemas.PostMeta(post_id="PARENT", sync_channel_id=remote_sc.id, ts=post_meta_ts("20.000000"))
    )

    envelope = {
        "kind": "message",
        "action": "create",
        "post_id": "REPLY",
        "source_channel_id": "C_LOCAL",
        "source_workspace_id": local_sc.workspace_id,
        "source_sync_channel_id": local_sc.id,
        "thread_post_id": "PARENT",
        "text": "reply",
    }
    with (
        patch("federation.deliver.deliver_remote", return_value=[]) as deliver,
        patch("helpers.sync_pipeline.apply_target") as apply,
    ):
        run_sync_pipeline(envelope, source_channel_id="C_LOCAL", source_sync_channel=local_sc)

    deliver.assert_called_once()
    assert deliver.call_args.args[2].channel_id == "C_REMOTE"
    assert deliver.call_args.args[0]["thread_post_id"] == "PARENT"
    assert deliver.call_args.args[0].get("target_ts") == "20.000000"
    apply.assert_not_called()


def test_edit_and_reaction_deliver_to_stub_without_copy_post_meta(real_db):
    """Edits and reactions still push; inbound 409s if the peer has no PostMeta."""
    _local, local_sc, remote_sc = _federated_announcements()
    DbManager.create_record(
        schemas.PostMeta(post_id="PARENT", sync_channel_id=local_sc.id, ts=post_meta_ts("10.000000"))
    )

    edit = {
        "kind": "message",
        "action": "edit",
        "post_id": "PARENT",
        "source_channel_id": "C_LOCAL",
        "text": "edited",
    }
    reaction = {
        "kind": "reaction",
        "action": "add",
        "post_id": "PARENT",
        "source_channel_id": "C_LOCAL",
        "reaction": "eyes",
    }
    with (
        patch("federation.deliver.deliver_remote", return_value=[]) as deliver,
        patch("helpers.sync_pipeline.apply_target") as apply,
    ):
        run_sync_pipeline(edit, source_channel_id="C_LOCAL", source_sync_channel=local_sc)
        run_sync_pipeline(reaction, source_channel_id="C_LOCAL", source_sync_channel=local_sc)

    assert deliver.call_count == 2
    assert {call.args[0]["action"] for call in deliver.call_args_list} == {"edit", "add"}
    assert all(call.args[2].channel_id == "C_REMOTE" for call in deliver.call_args_list)
    assert all(call.args[0].get("target_ts") is None for call in deliver.call_args_list)
    assert all("thread_post_id" not in call.args[0] for call in deliver.call_args_list)
    apply.assert_not_called()
    assert remote_sc.channel_id == "C_REMOTE"


def test_edit_and_reaction_deliver_to_stub_with_parent_post_meta(real_db):
    _local, local_sc, remote_sc = _federated_announcements()
    DbManager.create_record(
        schemas.PostMeta(post_id="PARENT", sync_channel_id=local_sc.id, ts=post_meta_ts("10.000000"))
    )
    DbManager.create_record(
        schemas.PostMeta(post_id="PARENT", sync_channel_id=remote_sc.id, ts=post_meta_ts("20.000000"))
    )

    edit = {
        "kind": "message",
        "action": "edit",
        "post_id": "PARENT",
        "source_channel_id": "C_LOCAL",
        "text": "edited",
    }
    reaction = {
        "kind": "reaction",
        "action": "add",
        "post_id": "PARENT",
        "source_channel_id": "C_LOCAL",
        "reaction": "eyes",
    }
    with (
        patch("federation.deliver.deliver_remote", return_value=[]) as deliver,
        patch("helpers.sync_pipeline.apply_target") as apply,
    ):
        run_sync_pipeline(edit, source_channel_id="C_LOCAL", source_sync_channel=local_sc)
        run_sync_pipeline(reaction, source_channel_id="C_LOCAL", source_sync_channel=local_sc)

    assert deliver.call_count == 2
    assert {call.args[0]["action"] for call in deliver.call_args_list} == {"edit", "add"}
    assert deliver.call_args_list[0].args[0].get("target_ts") == "20.000000"
    assert all("thread_post_id" not in call.args[0] for call in deliver.call_args_list)
    apply.assert_not_called()


def test_synced_copy_is_detected_and_does_not_need_to_originate(real_db):
    a, b = _workspace("T_A"), _workspace("T_B")
    sync = _sync(a, "copy")
    _channel(sync, a, "C_A", publishes=True, subscribes=False)
    target = _channel(sync, b, "C_B", publishes=False, subscribes=True)
    DbManager.create_record(
        schemas.PostMeta(post_id="P1", sync_channel_id=target.id, ts=22.000001, posted_as_user_id="U_B")
    )

    assert post_meta_exists_for_channel_ts("C_B", "22.000001")
    assert not post_meta_exists_for_channel_ts("C_B", "22.000002")
    assert not origin_publishes_anywhere("C_B")


def test_publishing_copy_originates_follow_ups(real_db):
    a, b = _workspace("T_A"), _workspace("T_B")
    sync = _sync(a, "two-way")
    _channel(sync, a, "C_A", publishes=True, subscribes=True)
    target = _channel(sync, b, "C_B", publishes=True, subscribes=True)
    DbManager.create_record(
        schemas.PostMeta(
            post_id="P1",
            sync_channel_id=target.id,
            ts=22.000001,
            posted_as_user_id="U_B",
            source_workspace_id=a.id,
        )
    )

    records = get_post_records("22.000001")
    rows = get_publishing_post_records(records, "C_B")
    assert len(rows) == 1
    assert rows[0][0].post_id == "P1"


def test_get_post_records_matches_six_decimal_slack_ts(real_db):
    slack_ts = "1757529600.123456"
    a = _workspace("T_A")
    sync = _sync(a, "old-post")
    ch = _channel(sync, a, "C_A", publishes=True, subscribes=True)
    DbManager.create_record(
        schemas.PostMeta(
            post_id="P_OLD",
            sync_channel_id=ch.id,
            ts=post_meta_ts(slack_ts),
        )
    )

    records = get_post_records(slack_ts)
    assert len(records) == 1
    assert records[0][0].post_id == "P_OLD"
    assert records[0][0].ts == post_meta_ts(slack_ts)


def test_bot_token_copy_originates_follow_ups(real_db):
    a, b = _workspace("T_A"), _workspace("T_B")
    sync = _sync(a, "two-way")
    _channel(sync, a, "C_A", publishes=True, subscribes=True)
    target = _channel(sync, b, "C_B", publishes=True, subscribes=True)
    DbManager.create_record(
        schemas.PostMeta(
            post_id="P1",
            sync_channel_id=target.id,
            ts=22.000001,
            source_workspace_id=a.id,
        )
    )

    records = get_post_records("22.000001")
    rows = get_publishing_post_records(records, "C_B")
    assert len(rows) == 1
    assert rows[0][0].post_id == "P1"


def test_federation_inbound_copy_originates_follow_ups(real_db):
    b = _workspace("T_B")
    sync = _sync(b, "fed")
    target = _channel(sync, b, "C_B", publishes=True, subscribes=True)
    DbManager.create_record(
        schemas.PostMeta(
            post_id="P1",
            sync_channel_id=target.id,
            ts=22.000001,
            source_workspace_id=None,
        )
    )

    records = get_post_records("22.000001")
    rows = get_publishing_post_records(records, "C_B")
    assert len(rows) == 1
    assert rows[0][0].post_id == "P1"


def test_reaction_notice_does_not_originate_follow_ups(real_db):
    b = _workspace("T_B")
    sync = _sync(b, "notices")
    target = _channel(sync, b, "C_B", publishes=True, subscribes=True)
    DbManager.create_record(
        schemas.PostMeta(
            post_id="rxn-1",
            sync_channel_id=target.id,
            ts=22.000001,
            kind="reaction_notice",
            source_workspace_id=1,
        )
    )

    records = get_post_records("22.000001")
    assert get_publishing_post_records(records, "C_B") == []


def test_origin_post_still_originates_follow_ups(real_db):
    a, b = _workspace("T_A"), _workspace("T_B")
    sync = _sync(a, "two-way")
    origin_ch = _channel(sync, a, "C_A", publishes=True, subscribes=True)
    _channel(sync, b, "C_B", publishes=True, subscribes=True)
    DbManager.create_record(
        schemas.PostMeta(
            post_id="P1",
            sync_channel_id=origin_ch.id,
            ts=22.000001,
            source_workspace_id=a.id,
        )
    )

    records = get_post_records("22.000001")
    rows = get_publishing_post_records(records, "C_A")
    assert len(rows) == 1
    assert rows[0][0].post_id == "P1"


def test_duplicate_target_across_syncs_is_written_once(real_db):
    a, b = _workspace("T_A"), _workspace("T_B")
    first, second = _sync(a, "first"), _sync(a, "second")
    for sync in (first, second):
        _channel(sync, a, "C_A", publishes=True, subscribes=False)
        _channel(sync, b, "C_B", publishes=False, subscribes=True)

    with patch("helpers.sync_pipeline.apply_target", return_value=ApplyOutcome()) as apply:
        run_sync_pipeline(
            {"kind": "message", "action": "create", "post_id": "P1", "source_workspace_id": a.id},
            source_channel_id="C_A",
        )

    apply.assert_called_once()
    assert apply.call_args.args[1].channel_id == "C_B"


def test_membership_survives_stop_pause_and_one_way_modes(real_db):
    a = _workspace("T_A")
    first, second, third = _sync(a, "first"), _sync(a, "second"), _sync(a, "third")
    active = _channel(first, a, "C_SHARED", publishes=True, subscribes=False)
    paused = _channel(second, a, "C_SHARED", publishes=False, subscribes=True, status="paused")
    deleted = _channel(third, a, "C_SHARED", publishes=True, subscribes=True)
    DbManager.update_record(schemas.SyncChannel, deleted.id, {"deleted_at": _now()})

    assert channel_has_membership("C_SHARED")
    DbManager.delete_records(schemas.SyncChannel, [schemas.SyncChannel.id == active.id])
    assert channel_has_membership("C_SHARED")
    assert not channel_has_membership("C_UNCONFIGURED")
    assert paused.status == "paused"


def test_all_subscribers_receive_when_listed_on_the_sync(real_db):
    """Participation (publishes/subscribes) owns fan-out; leftover direct mode does not."""
    a, b, c = _workspace("T_A"), _workspace("T_B"), _workspace("T_C")
    sync = DbManager.create_record(
        schemas.Sync(
            title="group",
            sync_mode="group",
            uid=str(uuid4()),
        )
    )
    _channel(sync, a, "C_A", publishes=True, subscribes=False)
    _channel(sync, b, "C_B", publishes=False, subscribes=True)
    _channel(sync, c, "C_C", publishes=False, subscribes=True)

    assert sorted(_target_ids("C_A")) == ["C_B", "C_C"]
