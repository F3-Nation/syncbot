"""Data-at-rest encryption / decryption using Fernet (AES-128-CBC + HMAC-SHA256).

The DATA_ENCRYPTION_KEY env var (legacy: TOKEN_ENCRYPTION_KEY) is stretched
to a 32-byte key using PBKDF2-HMAC-SHA256 with 600,000 iterations.  The
derived Fernet instance is cached so the expensive KDF runs at most once
per key per process.
"""

import base64
import functools
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

import constants

_logger = logging.getLogger(__name__)

_PBKDF2_ITERATIONS = 600_000
_PBKDF2_SALT_PREFIX = b"syncbot-fernet-v1"
_SLACK_TOKEN_PREFIXES = ("xoxb-", "xoxp-", "xoxe-", "xoxa-")


@functools.lru_cache(maxsize=2)
def _get_fernet(key: str) -> Fernet:
    """Derive a Fernet cipher from an arbitrary passphrase via PBKDF2."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt = _PBKDF2_SALT_PREFIX + key.encode()[:16]
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    derived = kdf.derive(key.encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def _resolve_encryption_key() -> str:
    """Return the encryption key from DATA_ENCRYPTION_KEY or legacy TOKEN_ENCRYPTION_KEY."""
    return os.environ.get(constants.DATA_ENCRYPTION_KEY) or os.environ.get(constants._DATA_ENCRYPTION_KEY_LEGACY, "")


def _encryption_enabled() -> bool:
    """Return *True* if data-at-rest encryption is active."""
    return constants._encryption_active()


def encryption_active_for_migration() -> bool:
    """Whether Alembic should encrypt existing plaintext Slack tokens."""
    return _encryption_enabled()


def _looks_like_slack_token(value: str) -> bool:
    return any(value.startswith(prefix) for prefix in _SLACK_TOKEN_PREFIXES)


def _looks_like_fernet(value: str) -> bool:
    return value.startswith("gAAAAA")


def encrypt_bot_token(token: str | None) -> str | None:
    """Encrypt a token before storing it in the database."""
    if not token:
        return token
    if not _encryption_enabled():
        return token
    if _looks_like_fernet(token):
        key = _resolve_encryption_key()
        try:
            _get_fernet(key).decrypt(token.encode())
            return token
        except InvalidToken:
            pass
    key = _resolve_encryption_key()
    return _get_fernet(key).encrypt(token.encode()).decode()


def decrypt_bot_token(encrypted: str | None) -> str | None:
    """Decrypt a token read from the database.

    Raises on failure when encryption is enabled and the value is not plaintext Slack.
    """
    if not encrypted:
        return encrypted
    if not _encryption_enabled():
        return encrypted
    if _looks_like_slack_token(encrypted):
        return encrypted
    key = _resolve_encryption_key()
    try:
        return _get_fernet(key).decrypt(encrypted.encode()).decode()
    except InvalidToken:
        _logger.error("Token decryption failed — refusing to use the token.")
        raise ValueError(
            "Token decryption failed. The token may be plaintext (not yet migrated) or tampered with."
        ) from None
