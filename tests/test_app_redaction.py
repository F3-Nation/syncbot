"""Tests for sensitive request log redaction."""

from app import _redact_sensitive


def test_redact_refresh_token_keys():
    payload = {
        "user": {
            "bot_refresh_token": "secret",
            "nested": {"refresh_token": "also-secret"},
        },
        "user_refresh_token": "xoxe-abc",
    }
    redacted = _redact_sensitive(payload)
    assert redacted["user"]["bot_refresh_token"] == "[REDACTED]"
    assert redacted["user"]["nested"]["refresh_token"] == "[REDACTED]"
    assert redacted["user_refresh_token"] == "[REDACTED]"
