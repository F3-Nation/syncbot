"""Wrap Bolt's SQLAlchemy installation store with at-rest token encryption."""

from __future__ import annotations

import copy

from slack_bolt.error import BoltError
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


class WorkspaceBlockedError(BoltError):
    """OAuth persist refused because the Team ID is on the Workspace Block List.

    Subclasses ``BoltError`` so Bolt's OAuth callback catches it and runs the
    failure handler instead of returning a 500.
    """

    def __init__(self, team_id: str | None):
        self.team_id = team_id
        super().__init__("workspace_blocked")


def _reject_if_blocked(team_id: str | None, token: str | None) -> None:
    from helpers.settings import team_id_is_blocked
    from helpers.workspace import slack_apps_uninstall

    if not team_id_is_blocked(team_id):
        return
    slack_apps_uninstall(token)
    raise WorkspaceBlockedError(team_id)


class EncryptedSQLAlchemyInstallationStore(SQLAlchemyInstallationStore):
    """Encrypt OAuth token columns on write; decrypt on read."""

    def save(self, installation: Installation):
        _reject_if_blocked(
            getattr(installation, "team_id", None),
            getattr(installation, "bot_token", None) or getattr(installation, "user_token", None),
        )
        return super().save(_encrypt_installation_tokens(installation))

    def save_bot(self, bot):
        _reject_if_blocked(getattr(bot, "team_id", None), getattr(bot, "bot_token", None))
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
