"""Tests for sensitive request log redaction."""

from app import _redact_sensitive
from logger import log_debug, redact_sensitive


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


def test_redact_bot_and_user_token_keys():
    redacted = redact_sensitive({"bot_token": "xoxb-live", "user_token": "xoxp-live", "team_id": "T1"})
    assert redacted["bot_token"] == "[REDACTED]"
    assert redacted["user_token"] == "[REDACTED]"
    assert redacted["team_id"] == "T1"


def test_redact_pairing_code_and_connection_code():
    redacted = redact_sensitive({"connection_code": "blob", "code": "FED-AABBCCDD", "channel_id": "C1"})
    assert redacted["connection_code"] == "[REDACTED]"
    assert redacted["code"] == "[REDACTED]"
    assert redacted["channel_id"] == "C1"


def test_redact_slack_token_inside_file_url():
    url = "https://files.slack.com/files-pri/T1-F1/img.png?t=xoxe-1-secret"
    redacted = redact_sensitive({"files": [{"url_private": url, "id": "F1"}]})
    assert redacted["files"][0]["url_private"] == "[REDACTED]"
    assert redact_sensitive({"error": url})["error"] == "[REDACTED]"


def test_redact_private_key_and_fernet():
    pem = "-----BEGIN " + "PRIVATE KEY-----\nMIIB\n-----END " + "PRIVATE KEY-----"
    redacted = redact_sensitive({"private_key": pem, "cipher": "gAAAAA" + ("A" * 40)})
    assert redacted["private_key"] == "[REDACTED]"
    assert redacted["cipher"] == "[REDACTED]"


def test_log_debug_redacts_token_fields(caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger="syncbot"):
        log_debug("bot_token_missing", bot_token="xoxb-should-not-log", team_id="T1")
    record = next(r for r in caplog.records if r.message == "bot_token_missing")
    assert record.__dict__.get("bot_token") == "[REDACTED]"
    assert record.__dict__.get("team_id") == "T1"
