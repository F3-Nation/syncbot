"""Tests for encrypted OAuth tokens and bot token refresh."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from helpers.encryption import decrypt_bot_token, decrypt_bytes, encrypt_bot_token, encrypt_bytes
from helpers.workspace import _maybe_refresh_bot_token


class TestTokenEncryptionAtRest:
    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "store-test-key-16chars"})
    def test_slack_token_encrypted_not_equal_plaintext(self):
        enc = encrypt_bot_token("xoxp-plaintext-token")
        assert enc != "xoxp-plaintext-token"
        assert enc.startswith("gAAAAA")
        assert decrypt_bot_token(enc) == "xoxp-plaintext-token"

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "changeme"})
    def test_placeholder_key_does_not_encrypt(self):
        assert encrypt_bot_token("xoxp-test") == "xoxp-test"

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "store-test-key-16chars"})
    def test_leftover_plaintext_token_stays_readable(self):
        assert decrypt_bot_token("xoxp-legacy-plaintext") == "xoxp-legacy-plaintext"

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "store-test-key-16chars"})
    def test_does_not_double_encrypt_fernet(self):
        once = encrypt_bot_token("xoxp-plain")
        twice = encrypt_bot_token(once)
        assert twice == once
        assert decrypt_bot_token(twice) == "xoxp-plain"

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "store-test-key-16chars"})
    def test_long_token_round_trip(self):
        token = "xoxp-" + ("a" * 120)
        enc = encrypt_bot_token(token)
        assert enc != token
        assert decrypt_bot_token(enc) == token

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "123"})
    def test_encryption_off_stores_plaintext(self):
        assert encrypt_bot_token("xoxp-local") == "xoxp-local"


class TestBytesEncryptionAtRest:
    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "store-test-key-16chars"})
    def test_payload_encrypted_not_equal_plaintext(self):
        plain = b"federation-file-part-bytes"
        enc = encrypt_bytes(plain)
        assert enc != plain
        assert enc.startswith(b"gAAAAA")
        assert decrypt_bytes(enc) == plain

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "changeme"})
    def test_placeholder_key_does_not_encrypt(self):
        assert encrypt_bytes(b"plain-file-part") == b"plain-file-part"

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "store-test-key-16chars"})
    def test_leftover_plaintext_stays_readable(self):
        assert decrypt_bytes(b"leftover-plaintext-part") == b"leftover-plaintext-part"

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "store-test-key-16chars"})
    def test_does_not_double_encrypt_fernet(self):
        once = encrypt_bytes(b"plain-part")
        twice = encrypt_bytes(once)
        assert twice == once
        assert decrypt_bytes(twice) == b"plain-part"

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "store-test-key-16chars"})
    def test_invalid_fernet_raises(self):
        with pytest.raises(ValueError, match="Payload decryption failed"):
            decrypt_bytes(b"gAAAAA" + b"A" * 80)


class TestBotTokenRefresh:
    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "refresh-test-key-16", "SLACK_CLIENT_ID": "111.222"})
    def test_same_plaintext_does_not_update(self):
        token = "xoxb-same-token-value"
        workspace = SimpleNamespace(id=1, team_id="T1", deleted_at=None, instance_id="a" * 64)
        context = {"bot_token": token}

        with (
            patch("helpers.workspace.get_bot_token", return_value=token),
            patch("helpers.encryption_installation_store.EncryptedSQLAlchemyInstallationStore") as store_cls,
        ):
            store_cls.return_value.save_bot = MagicMock()
            _maybe_refresh_bot_token(workspace, context)
            store_cls.return_value.save_bot.assert_not_called()

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "refresh-test-key-16", "SLACK_CLIENT_ID": "111.222"})
    def test_different_plaintext_updates_once(self):
        workspace = SimpleNamespace(id=1, team_id="T1", deleted_at=None, instance_id="a" * 64)
        context = {"bot_token": "xoxb-new"}
        bot = SimpleNamespace(
            bot_token="xoxb-old",
            app_id=None,
            enterprise_id=None,
            bot_id=None,
            bot_user_id=None,
            bot_scopes=None,
            bot_refresh_token=None,
            bot_token_expires_at=None,
            is_enterprise_install=False,
            installed_at=None,
        )

        with (
            patch("helpers.workspace.get_bot_token", return_value="xoxb-old"),
            patch("db.get_engine"),
            patch("helpers.encryption_installation_store.EncryptedSQLAlchemyInstallationStore") as store_cls,
        ):
            store_cls.return_value.find_bot.return_value = bot
            _maybe_refresh_bot_token(workspace, context)
            store_cls.return_value.save_bot.assert_called_once()


class TestEncryptedInstallationStore:
    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "store-test-key-16chars"})
    def test_encrypt_installation_tokens_copy_not_original(self):
        from slack_sdk.oauth.installation_store.models import Installation

        from helpers.encryption_installation_store import _encrypt_installation_tokens

        installation = Installation(
            app_id="A1",
            enterprise_id=None,
            team_id="T1",
            team_name="Team",
            bot_token="xoxb-plain",
            bot_user_id="B1",
            bot_id="BID",
            user_id="U1",
            user_token="xoxp-plain",
        )
        stored = _encrypt_installation_tokens(installation)
        assert stored.user_token != "xoxp-plain"
        assert stored.user_token.startswith("gAAAAA")
        assert installation.user_token == "xoxp-plain"

    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "store-test-key-16chars"})
    def test_decrypt_installation_tokens(self):
        from slack_sdk.oauth.installation_store.models import Installation

        from helpers.encryption_installation_store import _decrypt_installation_tokens

        installation = Installation(
            app_id="A1",
            enterprise_id=None,
            team_id="T1",
            team_name="Team",
            bot_token=encrypt_bot_token("xoxb-stored"),
            bot_user_id="B1",
            bot_id="BID",
            user_id="U1",
            user_token=encrypt_bot_token("xoxp-stored"),
        )
        _decrypt_installation_tokens(installation)
        assert installation.user_token == "xoxp-stored"
        assert installation.bot_token == "xoxb-stored"


class TestHomeRefreshTokenWrites:
    @patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "refresh-test-key-16"})
    def test_refresh_home_for_acting_user_does_not_rewrite_unchanged_token(self):
        from builders.home import refresh_home_tab_for_workspace

        token = "xoxb-same-token-value"
        workspace = SimpleNamespace(
            id=1,
            team_id="T1",
            deleted_at=None,
        )
        logger = MagicMock()
        context = {"bot_token": token}

        with (
            patch("helpers.export_import.invalidate_home_tab_caches_for_team"),
            patch("builders.home.build_home_tab", return_value=[]) as build,
            patch("builders.home.helpers.get_bot_token", return_value=token),
            patch("builders.home.WebClient"),
            patch("helpers.workspace.DbManager.update_records") as update,
        ):
            refresh_home_tab_for_workspace(workspace, logger, context, user_id="U1")

        update.assert_not_called()
        build.assert_called_once()
        assert build.call_args.kwargs.get("workspace") is workspace
        assert build.call_args.kwargs.get("user_id") == "U1"
