"""Tests for user-token echo remember/take helpers."""

import os
from unittest.mock import patch

import pytest
from sqlalchemy import inspect

from helpers.user_action_echo import (
    reaction_echo_fingerprint,
    remember_user_action,
    slack_message_ts,
    take_user_action_echo,
)


class TestReactionEchoFingerprint:
    def test_fingerprint_pads_short_fraction(self):
        assert reaction_echo_fingerprint("C1", "100.0", "thumbsup") == "C1:100.000000:thumbsup"
        assert slack_message_ts("100.000001") == "100.000001"
        assert slack_message_ts(100.0) == "100.000000"


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
