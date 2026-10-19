"""Tests for user-token echo remember/take helpers."""

import os
from unittest.mock import patch

import pytest
from sqlalchemy import inspect

from helpers.user_action_echo import (
    find_pending_file_share,
    has_user_action_echo,
    post_meta_ts,
    reaction_echo_fingerprint,
    remember_pending_file_share,
    remember_user_action,
    slack_message_ts,
    take_pending_file_share,
    take_user_action_echo,
)


class TestReactionEchoFingerprint:
    def test_fingerprint_pads_short_fraction(self):
        assert reaction_echo_fingerprint("C1", "100.0", "thumbsup") == "C1:100.000000:thumbsup"
        assert slack_message_ts("100.000001") == "100.000001"
        assert slack_message_ts(100.0) == "100.000000"

    def test_post_meta_ts_is_six_decimal_not_float(self):
        from decimal import Decimal

        slack_ts = "1757529600.123456"
        exact = Decimal("1757529600.123456")
        assert post_meta_ts(slack_ts) == exact
        assert post_meta_ts(slack_ts) != float(slack_ts)
        edge = "9999999999.123456"
        assert post_meta_ts(edge) == Decimal(edge)
        assert post_meta_ts(edge) != Decimal(str(float(edge)))


class TestRememberAndTake:
    @pytest.fixture
    def echo_db(self, tmp_path):
        import db as db_mod
        from db import get_engine, initialize_database

        url = f"sqlite:///{tmp_path / 'echo.db'}"
        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            yield get_engine()
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema

    def test_remember_then_take_consume_once(self, echo_db):
        assert inspect(echo_db).has_table("user_action_echoes")
        fp = reaction_echo_fingerprint("C_TGT", "200.0", "thumbsup")
        remember_user_action("T2", "U_MAPPED", "reaction_added", fp)
        assert take_user_action_echo("T2", "U_MAPPED", "reaction_added", fp) is True
        assert take_user_action_echo("T2", "U_MAPPED", "reaction_added", fp) is False

    def test_take_misses_different_fingerprint(self, echo_db):
        remember_user_action("T2", "U1", "reaction_added", "C1:1.0:a")
        assert take_user_action_echo("T2", "U1", "reaction_added", "C1:1.0:b") is False

    def test_file_echo_peek_does_not_consume(self, echo_db):
        remember_user_action("T2", "U_MAPPED", "file", "F99")
        assert has_user_action_echo("T2", "U_MAPPED", "file", "F99") is True
        assert has_user_action_echo("T2", "U_MAPPED", "file", "F99") is True
        assert take_user_action_echo("T2", "U_MAPPED", "message", "C_TGT:200.000000") is False

    def test_message_echo_peek_does_not_consume(self, echo_db):
        remember_user_action("T2", "U_MAPPED", "message", "C_TGT:200.000000")
        assert has_user_action_echo("T2", "U_MAPPED", "message", "C_TGT:200.000000") is True
        assert has_user_action_echo("T2", "U_MAPPED", "message", "C_TGT:200.000000") is True

    def test_pending_file_share_find_then_take(self, echo_db):
        remember_pending_file_share("T2", "C_TGT", "F1", "postabc", sync_channel_id=7)
        pending = find_pending_file_share("T2", "C_TGT", "F1")
        assert pending is not None
        assert pending.post_id == "postabc"
        assert pending.sync_channel_id == 7
        assert find_pending_file_share("T2", "C_TGT", "F1").post_id == "postabc"
        taken = take_pending_file_share("T2", "C_TGT", "F1")
        assert taken is not None
        assert taken.post_id == "postabc"
        assert take_pending_file_share("T2", "C_TGT", "F1") is None


class TestReactionEventLastWriteWins:
    @pytest.fixture
    def echo_db(self, tmp_path):
        import db as db_mod
        from db import get_engine, initialize_database

        url = f"sqlite:///{tmp_path / 'echo_lww.db'}"
        old_engine = db_mod.GLOBAL_ENGINE
        old_schema = db_mod.GLOBAL_SCHEMA
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            yield get_engine()
            if db_mod.GLOBAL_ENGINE:
                db_mod.GLOBAL_ENGINE.dispose()
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SCHEMA = old_schema

    def test_newer_remove_makes_older_add_stale(self, echo_db):
        from helpers.user_action_echo import (
            reaction_event_ts_is_stale,
            remember_reaction_event_ts,
        )

        remember_reaction_event_ts("T_TGT", "U_SRC", "C_TGT", "100.0", "thumbsup", "2.000000")
        assert reaction_event_ts_is_stale("T_TGT", "U_SRC", "C_TGT", "100.0", "thumbsup", "1.000001") is True
        assert reaction_event_ts_is_stale("T_TGT", "U_SRC", "C_TGT", "100.0", "thumbsup", "3.000000") is False


class TestCompleteCopyPendingShare:
    @pytest.fixture
    def real_db(self, tmp_path):
        import db as db_mod
        from db import initialize_database
        from helpers._cache import clear_all_caches

        url = f"sqlite:///{tmp_path / 'pending_copy.db'}"
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

    def _channel(self, channel_id="C_TGT"):
        from datetime import UTC, datetime
        from uuid import uuid4

        from db import DbManager, schemas
        from federation.core import get_instance_id

        workspace = DbManager.create_record(
            schemas.Workspace(
                team_id="T_TGT",
                workspace_name="Workspace A",
                instance_id=get_instance_id(),
            )
        )
        sync = DbManager.create_record(schemas.Sync(title="pending-copy", sync_mode="group", uid=str(uuid4())))
        sync_channel = DbManager.create_record(
            schemas.SyncChannel(
                sync_id=sync.id,
                workspace_id=workspace.id,
                channel_id=channel_id,
                status="active",
                publishes=True,
                subscribes=True,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        return workspace, sync_channel

    def _remember(self, sync_channel, file_id, post_id, **fields):
        from helpers.user_action_echo import remember_pending_file_share

        remember_pending_file_share(
            "T_TGT",
            sync_channel.channel_id,
            file_id,
            post_id,
            sync_channel_id=sync_channel.id,
            **fields,
        )

    def test_writes_copy_without_local_origin_records(self, real_db):
        from helpers.post_meta import complete_copy_ts_from_pending_share, get_post_records
        from helpers.user_action_echo import find_pending_file_share

        workspace, sync_channel = self._channel()
        self._remember(
            sync_channel,
            "F1",
            "postfed",
            source_user_id="U_SRC",
            source_workspace_id=workspace.id,
            posted_as_user_id="U_MAPPED",
        )
        assert complete_copy_ts_from_pending_share("T_TGT", "C_TGT", "200.000001", ["F1"]) is True
        rows = get_post_records("200.000001")
        assert len(rows) == 1
        post_meta, sc, _ws = rows[0]
        assert post_meta.post_id == "postfed"
        assert sc.id == sync_channel.id
        assert post_meta.source_user_id == "U_SRC"
        assert post_meta.source_workspace_id == workspace.id
        assert post_meta.posted_as_user_id == "U_MAPPED"
        assert find_pending_file_share("T_TGT", "C_TGT", "F1") is None

    def test_writes_only_the_remembered_sync_channel(self, real_db):
        from datetime import UTC, datetime
        from uuid import uuid4

        from db import DbManager, schemas
        from helpers.post_meta import complete_copy_ts_from_pending_share

        workspace, first = self._channel()
        other_sync = DbManager.create_record(schemas.Sync(title="other-sync", sync_mode="group", uid=str(uuid4())))
        second = DbManager.create_record(
            schemas.SyncChannel(
                sync_id=other_sync.id,
                workspace_id=workspace.id,
                channel_id="C_TGT",
                status="active",
                publishes=True,
                subscribes=True,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        self._remember(first, "F1", "postfed")
        assert complete_copy_ts_from_pending_share("T_TGT", "C_TGT", "200.000001", ["F1"]) is True
        rows = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "postfed"])
        assert [pm.sync_channel_id for pm in rows] == [first.id]
        assert second.id not in {pm.sync_channel_id for pm in rows}

    def test_writes_file_ts_when_parent_row_already_exists(self, real_db):
        from db import DbManager, schemas
        from helpers.post_meta import complete_copy_ts_from_pending_share, get_post_records
        from helpers.user_action_echo import post_meta_ts

        _workspace, sync_channel = self._channel()
        DbManager.create_record(
            schemas.PostMeta(
                post_id="postsplit",
                sync_channel_id=sync_channel.id,
                ts=post_meta_ts("100.000001"),
            )
        )
        self._remember(sync_channel, "F2", "postsplit")
        assert complete_copy_ts_from_pending_share("T_TGT", "C_TGT", "200.000002", ["F2"]) is True
        rows = DbManager.find_records(schemas.PostMeta, [schemas.PostMeta.post_id == "postsplit"])
        assert {str(pm.ts) for pm in rows} == {"100.000001", "200.000002"}
        assert [pm.post_id for pm, _sc, _ws in get_post_records("200.000002")] == ["postsplit"]

    def test_releases_claim_when_sync_channel_is_missing(self, real_db):
        from helpers.post_meta import complete_copy_ts_from_pending_share
        from helpers.user_action_echo import find_pending_file_share, remember_pending_file_share

        remember_pending_file_share("T_TGT", "C_NONE", "F3", "postnone", sync_channel_id=99999)
        assert complete_copy_ts_from_pending_share("T_TGT", "C_NONE", "200.000003", ["F3"]) is False
        pending = find_pending_file_share("T_TGT", "C_NONE", "F3")
        assert pending is not None
        assert pending.post_id == "postnone"
