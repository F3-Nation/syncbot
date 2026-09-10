"""Wrap Bolt's SQLAlchemy installation store with at-rest token encryption."""

from __future__ import annotations

import copy

from slack_sdk.oauth.installation_store.models import Installation
from slack_sdk.oauth.installation_store.sqlalchemy import SQLAlchemyInstallationStore

from helpers.encryption import decrypt_bot_token, encrypt_bot_token

_TOKEN_FIELDS = ("bot_token", "bot_refresh_token", "user_token", "user_refresh_token")


def _encrypt_installation_tokens(installation: Installation) -> Installation:
    stored = copy.copy(installation)
    for field in _TOKEN_FIELDS:
        raw = getattr(stored, field, None)
        if raw:
            setattr(stored, field, encrypt_bot_token(raw))
    return stored


def _decrypt_installation_tokens(installation: Installation | None) -> Installation | None:
    if installation is None:
        return None
    for field in _TOKEN_FIELDS:
        raw = getattr(installation, field, None)
        if raw:
            setattr(installation, field, decrypt_bot_token(raw))
    return installation


class EncryptedSQLAlchemyInstallationStore(SQLAlchemyInstallationStore):
    """Encrypt OAuth token columns on write; decrypt on read."""

    def save(self, installation: Installation):
        return super().save(_encrypt_installation_tokens(installation))

    def save_bot(self, bot):
        encrypted = copy.copy(bot)
        for field in ("bot_token", "bot_refresh_token"):
            raw = getattr(encrypted, field, None)
            if raw:
                setattr(encrypted, field, encrypt_bot_token(raw))
        return super().save_bot(encrypted)

    def find_installation(self, **kwargs):
        return _decrypt_installation_tokens(super().find_installation(**kwargs))

    def find_bot(self, **kwargs):
        bot = super().find_bot(**kwargs)
        if bot is None:
            return None
        for field in ("bot_token", "bot_refresh_token"):
            raw = getattr(bot, field, None)
            if raw:
                setattr(bot, field, decrypt_bot_token(raw))
        return bot
