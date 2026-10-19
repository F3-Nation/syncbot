"""Reinstall and migration import rejoin public Channels and leave private paused."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

os.environ.setdefault("LOCAL_DEVELOPMENT", "true")
os.environ.setdefault("DATABASE_BACKEND", "sqlite")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest


@pytest.fixture
def real_db(tmp_path):
    import db as db_mod
    from db import initialize_database
    from helpers._cache import clear_all_caches

    url = f"sqlite:///{tmp_path / 'heal_sync.db'}"
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
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SESSION = old_session
            db_mod.GLOBAL_SCHEMA = old_schema
            clear_all_caches()


def _seed_pair():
    from db import DbManager, schemas
    from federation.core import get_instance_id

    now = datetime.now(UTC).replace(tzinfo=None)
    local = DbManager.create_record(
        schemas.Workspace(team_id="TLOCAL", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    other = DbManager.create_record(
        schemas.Workspace(team_id="TOTHER", workspace_name="Workspace B", instance_id=get_instance_id())
    )
    group = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Shared",
            invite_code="TESTCODE",
            status="active",
            created_at=now,
            uid="11111111-1111-1111-1111-111111111111",
        )
    )
    for workspace in (local, other):
        DbManager.create_record(
            schemas.WorkspaceGroupMember(
                group_id=group.id,
                workspace_id=workspace.id,
                status="active",
                role="member",
                joined_at=now,
            )
        )
    sync = DbManager.create_record(
        schemas.Sync(
            title="Shared",
            group_id=group.id,
            sync_mode="group",
            uid="22222222-2222-2222-2222-222222222222",
        )
    )
    here = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=local.id,
            channel_id="CHERE",
            channel_name="general",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    there = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=other.id,
            channel_id="CTHERE",
            channel_name="announcements",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    return local, other, here, there


def _client_for(is_private: bool, is_member: bool) -> MagicMock:
    client = MagicMock()
    client.conversations_info.return_value = {"channel": {"is_private": is_private, "is_member": is_member}}
    return client


def test_heal_public_channel_pauses_joins_and_resumes(real_db):
    from db import DbManager, schemas
    from helpers.workspace import heal_restored_sync_channels

    local, _other, here, _there = _seed_pair()
    client = _client_for(is_private=False, is_member=False)
    posts: list[tuple[str, str]] = []

    def capture(_slack, channel_ids, message):
        posts.append((channel_ids[0], message))
        return 1

    with (
        patch("helpers.workspace.get_bot_token", return_value="xoxb-test"),
        patch("helpers.notifications.notify_synced_channels", side_effect=capture),
        patch("helpers.notifications.notify_admins_dm"),
        patch("helpers.workspace.WebClient", return_value=MagicMock()),
        patch("federation.replicate.replicate_sync_channel_upsert"),
    ):
        counts = heal_restored_sync_channels(local, client=client, source="reinstall")

    client.conversations_join.assert_called_once_with(channel="CHERE")
    row = DbManager.get_record(schemas.SyncChannel, id=here.id)
    assert row.status == "active"
    assert counts["resumed"] == 1
    assert counts["joined"] == 1
    local_texts = [text for channel_id, text in posts if channel_id == "CHERE"]
    assert any("paused while SyncBot restores" in text for text in local_texts)
    assert any("has been resumed" in text for text in local_texts)
    assert any("joined this Channel to restore" in text for text in local_texts)
    assert any(channel_id == "CTHERE" and "has been resumed" in text for channel_id, text in posts)


def test_heal_private_channel_stays_paused_with_notice(real_db):
    from db import DbManager, schemas
    from helpers.workspace import heal_restored_sync_channels

    local, _other, here, _there = _seed_pair()
    client = _client_for(is_private=True, is_member=False)
    posts: list[tuple[str, str]] = []

    def capture(_slack, channel_ids, message):
        posts.append((channel_ids[0], message))
        return 0 if channel_ids[0] == "CHERE" else 1

    with (
        patch("helpers.workspace.get_bot_token", return_value="xoxb-test"),
        patch("helpers.notifications.notify_synced_channels", side_effect=capture),
        patch("helpers.notifications.notify_admins_dm") as admin_dm,
        patch("helpers.workspace.WebClient", return_value=MagicMock()),
        patch("federation.replicate.replicate_sync_channel_upsert"),
    ):
        counts = heal_restored_sync_channels(local, client=client, source="import")

    client.conversations_join.assert_not_called()
    row = DbManager.get_record(schemas.SyncChannel, id=here.id)
    assert row.status == "paused"
    assert counts["paused_private"] == 1
    assert any(channel_id == "CHERE" and "This Channel is private" in text for channel_id, text in posts)
    assert any(channel_id == "CTHERE" and "is paused" in text for channel_id, text in posts)
    assert admin_dm.called


def test_heal_skips_public_channel_already_joined(real_db):
    from db import DbManager, schemas
    from helpers.workspace import heal_restored_sync_channels

    local, _other, here, _there = _seed_pair()
    client = _client_for(is_private=False, is_member=True)

    with (
        patch("helpers.notifications.notify_synced_channels") as notify,
        patch("helpers.notifications.notify_admins_dm") as admin_dm,
    ):
        counts = heal_restored_sync_channels(local, client=client, source="import")

    client.conversations_join.assert_not_called()
    notify.assert_not_called()
    admin_dm.assert_not_called()
    row = DbManager.get_record(schemas.SyncChannel, id=here.id)
    assert row.status == "active"
    assert counts["skipped"] == 1


def test_inspect_unknown_channel_is_private():
    from helpers.conversations import inspect_bot_channel_access

    client = MagicMock()
    client.conversations_info.side_effect = RuntimeError("nope")
    assert inspect_bot_channel_access(client, "C1") == (True, False)
    assert inspect_bot_channel_access(client, "") == (True, False)


def test_inbound_upsert_resumes_sibling_on_original_instance(real_db):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from db import DbManager, schemas
    from federation.api import handle_sync_channel_upsert
    from federation.core import get_instance_id, public_key_fingerprint

    now = datetime.now(UTC).replace(tzinfo=None)
    peer_public = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    fed = DbManager.create_record(
        schemas.Instance(
            instance_id=public_key_fingerprint(peer_public),
            webhook_url="https://peer.example/api/federation",
            public_key=peer_public,
            status="active",
            trust_status="trusted",
            name="Partner Org",
            created_at=now,
        )
    )
    local = DbManager.create_record(
        schemas.Workspace(team_id="TLOCAL", workspace_name="Workspace A", instance_id=get_instance_id())
    )
    stub = DbManager.create_record(
        schemas.Workspace(team_id="TSTUB", workspace_name="Workspace B", instance_id=fed.instance_id)
    )
    group = DbManager.create_record(
        schemas.WorkspaceGroup(
            name="Shared",
            invite_code="TESTCODE2",
            status="active",
            created_at=now,
            uid="33333333-3333-3333-3333-333333333333",
        )
    )
    for workspace in (local, stub):
        DbManager.create_record(
            schemas.WorkspaceGroupMember(
                group_id=group.id,
                workspace_id=workspace.id,
                status="active",
                role="member",
                joined_at=now,
            )
        )
    sync = DbManager.create_record(
        schemas.Sync(
            title="Shared",
            group_id=group.id,
            sync_mode="group",
            uid="44444444-4444-4444-4444-444444444444",
        )
    )
    stub_channel = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=stub.id,
            channel_id="CSTUB",
            channel_name="general",
            status="paused",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    local_channel = DbManager.create_record(
        schemas.SyncChannel(
            sync_id=sync.id,
            workspace_id=local.id,
            channel_id="CLOCAL",
            channel_name="announcements",
            status="active",
            publishes=True,
            subscribes=True,
            created_at=now,
        )
    )
    posts: list[tuple[str, str]] = []

    def capture(_slack, channel_ids, message):
        posts.append((channel_ids[0], message))
        return 1

    def token_for(workspace):
        if workspace and workspace.team_id == "TLOCAL":
            return "xoxb-local"
        return None

    with (
        patch("helpers.workspace.get_bot_token", side_effect=token_for),
        patch("helpers.notifications.notify_synced_channels", side_effect=capture),
        patch("helpers.workspace.WebClient", return_value=MagicMock()),
    ):
        status, resp = handle_sync_channel_upsert(
            {
                "sync_uid": sync.uid,
                "team_id": "TSTUB",
                "channel_id": "CSTUB",
                "status": "active",
                "publishes": True,
                "subscribes": True,
            },
            fed,
        )

    assert status == 200
    assert resp["ok"] is True
    row = DbManager.get_record(schemas.SyncChannel, id=stub_channel.id)
    assert row.status == "active"
    assert row.deleted_at is None
    assert any(channel_id == local_channel.channel_id and "has been resumed" in text for channel_id, text in posts)
    assert any("Workspace B" in text for _channel_id, text in posts)

    posts.clear()
    with (
        patch("helpers.workspace.get_bot_token", side_effect=token_for),
        patch("helpers.notifications.notify_synced_channels", side_effect=capture),
        patch("helpers.workspace.WebClient", return_value=MagicMock()),
    ):
        status, _resp = handle_sync_channel_upsert(
            {
                "sync_uid": sync.uid,
                "team_id": "TSTUB",
                "channel_id": "CSTUB",
                "status": "active",
                "publishes": True,
                "subscribes": True,
            },
            fed,
        )
    assert status == 200
    assert posts == []
