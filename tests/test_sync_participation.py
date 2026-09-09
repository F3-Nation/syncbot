"""Publish/subscribe participation, fan-in, dedupe, and copy-guard tests."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from db import DbManager, schemas
from helpers.post_meta import post_meta_exists_for_channel_ts
from helpers.sync_participation import (
    already_subscribed_to_source,
    channel_has_membership,
    channel_subscribes,
    find_origin_sync_channel,
    iter_publish_targets,
    origin_publishes_anywhere,
    participation_flags,
    participation_label,
)
from helpers.sync_pipeline import run_sync_pipeline


def test_participation_flags():
    assert participation_flags("publish_only") == (True, False)
    assert participation_flags("subscribe_only") == (False, True)
    assert participation_flags("publish_and_subscribe") == (True, True)
    assert participation_flags("subscribe_and_publish") == (True, True)
    assert participation_flags(None) == (True, True)


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
    return DbManager.create_record(
        schemas.Workspace(team_id=team_id, workspace_name=team_id, bot_token=f"token-{team_id}")
    )


def _sync(publisher, title: str):
    return DbManager.create_record(schemas.Sync(title=title, sync_mode="group", publisher_workspace_id=publisher.id))


def _channel(sync, workspace, channel_id: str, *, publishes: bool, subscribes: bool, status="active"):
    return DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=workspace.id,
            channel_id=channel_id,
            status=status,
            publishes=publishes,
            subscribes=subscribes,
            reaction_direction="both",
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
    assert find_origin_sync_channel("C_SUB") is None
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

    with (
        patch("helpers.sync_pipeline.get_federated_workspace_for_sync", return_value=None),
        patch("helpers.sync_pipeline.apply_target", return_value=[]) as apply,
    ):
        run_sync_pipeline(
            envelope,
            source_channel_id="C_A",
            source_sync_channel=source,
            origin_ts="1.000000",
        )

    assert [call.args[1].channel_id for call in apply.call_args_list] == ["C_B"]


def test_thread_reply_stays_on_original_post_records_not_sibling_sync(real_db):
    """A reply on a copy in a Channel that also publishes elsewhere must not unthread."""
    hub, ao, blackops = _workspace("T_HUB"), _workspace("T_AO"), _workspace("T_BLACK")
    sync_a, sync_b = _sync(hub, "hub to ao"), _sync(blackops, "blackops to hub")
    hub_a = _channel(sync_a, hub, "C_HUB", publishes=True, subscribes=True)
    _channel(sync_a, ao, "C_AO", publishes=True, subscribes=True)
    hub_b = _channel(sync_b, hub, "C_HUB", publishes=True, subscribes=True)
    black = _channel(sync_b, blackops, "C_BLACK", publishes=True, subscribes=True)
    DbManager.create_record(schemas.PostMeta(post_id="PARENT", sync_channel_id=black.id, ts=10.0))
    DbManager.create_record(schemas.PostMeta(post_id="PARENT", sync_channel_id=hub_b.id, ts=20.0))

    envelope = {
        "kind": "message",
        "action": "create",
        "post_id": "REPLY",
        "source_workspace_id": hub.id,
        "source_sync_channel_id": hub_b.id,
        "thread_post_id": "PARENT",
        "text": "reply in the copy thread",
    }

    with (
        patch("helpers.sync_pipeline.get_federated_workspace_for_sync", return_value=None),
        patch("helpers.sync_pipeline.apply_target", return_value=[]) as apply,
    ):
        run_sync_pipeline(
            envelope,
            source_channel_id="C_HUB",
            source_sync_channel=hub_b,
            origin_ts="30.000000",
        )

    assert [call.args[1].channel_id for call in apply.call_args_list] == ["C_BLACK"]
    assert apply.call_args.kwargs["thread_ts"] == "10.000000"
    assert "C_AO" not in [call.args[1].channel_id for call in apply.call_args_list]
    assert hub_a.id != hub_b.id


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


def test_duplicate_target_across_syncs_is_written_once(real_db):
    a, b = _workspace("T_A"), _workspace("T_B")
    first, second = _sync(a, "first"), _sync(a, "second")
    for sync in (first, second):
        _channel(sync, a, "C_A", publishes=True, subscribes=False)
        _channel(sync, b, "C_B", publishes=False, subscribes=True)

    with (
        patch("helpers.sync_pipeline.get_federated_workspace_for_sync", return_value=None),
        patch("helpers.sync_pipeline.apply_target", return_value=[]) as apply,
    ):
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


def test_direct_sync_does_not_fan_out_to_extra_group_members(real_db):
    a, b, c = _workspace("T_A"), _workspace("T_B"), _workspace("T_C")
    sync = DbManager.create_record(
        schemas.Sync(
            title="direct",
            sync_mode="direct",
            publisher_workspace_id=a.id,
            target_workspace_id=b.id,
        )
    )
    _channel(sync, a, "C_A", publishes=True, subscribes=False)
    _channel(sync, b, "C_B", publishes=False, subscribes=True)
    _channel(sync, c, "C_C", publishes=False, subscribes=True)

    assert _target_ids("C_A") == ["C_B"]
