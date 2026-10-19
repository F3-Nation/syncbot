"""Structured ``log`` / ``log_debug`` helpers."""

import logging

import pytest

from logger import log, log_critical, log_debug, log_error, log_info, log_warning


def test_log_requires_level():
    with pytest.raises(TypeError):
        log("message_skip")  # type: ignore[call-arg]


def test_log_rejects_unknown_level_name():
    with pytest.raises(ValueError, match="unknown log level"):
        log("message_skip", "verbose")


def test_log_debug_emits_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger="syncbot"):
        log_debug("message_skip", reason="file_echo")
    record = next(r for r in caplog.records if r.message == "message_skip")
    assert record.levelno == logging.DEBUG
    assert record.__dict__.get("reason") == "file_echo"
    assert record.funcName == "test_log_debug_emits_debug"


def test_level_wrappers(caplog):
    with caplog.at_level(logging.DEBUG, logger="syncbot"):
        log_info("federation_pair", direction="inbound")
        log_warning("federation_pair", reason="code_expired")
        log_error("federation_pair", reason="exhausted")
        log_critical("federation_pair", reason="unusable")
    by_message_level = {(r.message, r.levelno) for r in caplog.records if r.message == "federation_pair"}
    assert ("federation_pair", logging.INFO) in by_message_level
    assert ("federation_pair", logging.WARNING) in by_message_level
    assert ("federation_pair", logging.ERROR) in by_message_level
    assert ("federation_pair", logging.CRITICAL) in by_message_level


def test_log_error_exc_info(caplog):
    with caplog.at_level(logging.ERROR, logger="syncbot"):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            log_error("restore_failed", exc_info=True)
    record = next(r for r in caplog.records if r.message == "restore_failed")
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError
