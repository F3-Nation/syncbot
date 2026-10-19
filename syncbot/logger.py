"""Structured logging and observability helpers.

Provides:

* **Structured JSON formatter** — Every log entry is emitted as a single
  JSON object with consistent fields (``timestamp``, ``level``,
  ``correlation_id``, ``module``, ``message``).  This makes CloudWatch
  Logs Insights queries fast and reliable.
* **Correlation IDs** — A unique ``correlation_id`` is generated at the
  start of each incoming Slack request and automatically included in
  every log line emitted during that request.
* **Metrics helpers** — Lightweight functions that emit metric events as
  structured log entries.  CloudWatch Logs Insights or a metric filter
  can aggregate these into numeric dashboards.

Usage::

    from logger import configure_logging, set_correlation_id, emit_metric, log_debug, log_info

    configure_logging()          # call once at module level
    set_correlation_id()         # call at the start of each request
    emit_metric("messages_synced", 3, sync_id="abc")
    log_debug("message_skip", reason="file_echo", channel="C123")
    log_info("heal_stub", result="converted", team_id="T1")
    # or log("heal_stub", "INFO", result="converted", team_id="T1")
"""

import json
import logging
import re
import time as _time
import uuid
from datetime import UTC
from typing import Any

# ---------------------------------------------------------------------------
# Correlation-ID storage (thread-local not needed — one Slack request at a time)
# ---------------------------------------------------------------------------

_correlation_id: str | None = None
_request_start: float | None = None

_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = frozenset(
    {
        "token",
        "bot_token",
        "user_token",
        "access_token",
        "bot_refresh_token",
        "user_refresh_token",
        "refresh_token",
        "shared_secret",
        "public_key",
        "private_key",
        "private_key_encrypted",
        "connection_code",
        "pairing_code",
        "raw_code",
        "signature",
        "sig",
        "authorization",
        "cookie",
        "password",
        "client_secret",
        "signing_secret",
        "slack_signing_secret",
        "database_password",
        "url_private",
        "url_private_download",
    }
)
_SENSITIVE_KEY_SUFFIXES = ("_token", "_secret", "_password")
_SLACK_TOKEN_RE = re.compile(r"xox[a-z](?:\.[a-z]+)?-[\w-]+", re.IGNORECASE)
_PAIRING_CODE_RE = re.compile(r"^FED-[0-9A-Fa-f]{8}$")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SENSITIVE_KEYS:
        return True
    return any(lowered.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES)


def _looks_like_secret(value: str) -> bool:
    if _SLACK_TOKEN_RE.search(value):
        return True
    if "BEGIN" in value and "PRIVATE KEY" in value:
        return True
    if value.startswith("gAAAAA") and len(value) > 40:
        return True
    return bool(_PAIRING_CODE_RE.match(value.strip()))


def _redact_value(key: str, value: Any, *, depth: int = 0) -> Any:
    if depth > 10:
        return value
    if _is_sensitive_key(key):
        return _REDACTED
    if isinstance(value, dict):
        return {k: _redact_value(k, v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, item, depth=depth + 1) for item in value]
    if isinstance(value, str) and _looks_like_secret(value):
        return _REDACTED
    return value


def redact_sensitive(obj: Any, _depth: int = 0) -> Any:
    """Return a copy of *obj* with tokens, keys, and pairing codes redacted."""
    if isinstance(obj, dict):
        return {k: _redact_value(k, v, depth=_depth) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_sensitive(item, _depth + 1) for item in obj]
    if isinstance(obj, str) and _looks_like_secret(obj):
        return _REDACTED
    return obj


def set_correlation_id(value: str | None = None) -> str:
    """Set and return a correlation ID for the current request.

    If *value* is ``None`` a new UUID-4 is generated.  Also resets the
    internal request-start timer used by :func:`get_request_duration_ms`.
    """
    global _correlation_id, _request_start
    _correlation_id = value or uuid.uuid4().hex[:12]
    _request_start = _time.monotonic()
    return _correlation_id


def get_correlation_id() -> str:
    """Return the current correlation ID, or ``"none"`` if unset."""
    return _correlation_id or "none"


def get_request_duration_ms() -> float:
    """Milliseconds elapsed since :func:`set_correlation_id` was called."""
    if _request_start is None:
        return 0.0
    return (_time.monotonic() - _request_start) * 1000


# ---------------------------------------------------------------------------
# Structured JSON formatter
# ---------------------------------------------------------------------------


class StructuredFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object.

    Fields included in every entry:

    * ``timestamp`` — ISO-8601 UTC
    * ``level`` — e.g. INFO, WARNING, ERROR
    * ``correlation_id`` — request-scoped ID set by :func:`set_correlation_id`
    * ``module`` — Python module that emitted the log
    * ``function`` — function name
    * ``message`` — the formatted log message

    Extra keys passed via ``logging.info("msg", extra={...})`` are merged
    into the top-level JSON object.
    """

    # Keys that belong to the stdlib LogRecord and should not be forwarded.
    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "correlation_id": get_correlation_id(),
            "module": record.module,
            "function": record.funcName,
            "message": record.getMessage(),
        }

        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)

        # Merge any extra fields the caller passed.
        for key, val in record.__dict__.items():
            if key not in self._RESERVED and key not in entry:
                entry[key] = _redact_value(key, val)

        return json.dumps(entry, default=str)

    def formatTime(self, record, datefmt=None):  # noqa: N802 — override
        from datetime import datetime

        dt = datetime.fromtimestamp(record.created, tz=UTC)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


class DevFormatter(logging.Formatter):
    """Human-readable colorized formatter for local development.

    Outputs logs like::

        17:14:05 INFO  [app.main_response] (9dab20ac) request_received
                request_type=event_callback  request_id=app_home_opened

        17:14:06 ERROR [listener_error_handler.handle] (9dab20ac) Something broke
                Traceback (most recent call last):
                  ...
    """

    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())

    _COLORS = {
        "DEBUG": "\033[90m",  # grey
        "INFO": "\033[32m",  # green
        "WARNING": "\033[33m",  # yellow
        "ERROR": "\033[31m",  # red
        "CRITICAL": "\033[1;31m",  # bold red
    }
    _RESET = "\033[0m"
    _DIM = "\033[90m"

    def format(self, record: logging.LogRecord) -> str:
        from datetime import datetime

        dt = datetime.fromtimestamp(record.created, tz=UTC)
        time_str = dt.strftime("%H:%M:%S")

        color = self._COLORS.get(record.levelname, "")
        level = f"{color}{record.levelname:<5}{self._RESET}"

        corr = get_correlation_id()
        corr_str = f" {self._DIM}({corr}){self._RESET}" if corr != "none" else ""

        location = f"{record.module}.{record.funcName}"
        msg = record.getMessage()

        line = f"{self._DIM}{time_str}{self._RESET} {level} [{location}]{corr_str} {msg}"

        extras = {}
        for key, val in record.__dict__.items():
            if key not in self._RESERVED and key not in ("message", "correlation_id"):
                extras[key] = _redact_value(key, val)

        if extras:
            pairs = "  ".join(f"{k}={v}" for k, v in extras.items())
            line += f"\n{' ' * 15}{self._DIM}{pairs}{self._RESET}"

        if record.exc_info and record.exc_info[1]:
            exc_text = self.formatException(record.exc_info)
            indented = "\n".join(f"{' ' * 15}{line_}" for line_ in exc_text.splitlines())
            line += f"\n{indented}"

        return line


# ---------------------------------------------------------------------------
# One-time logging configuration
# ---------------------------------------------------------------------------

_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """Replace the root logger's handlers with a single structured-JSON handler.

    The effective level is determined by the ``LOG_LEVEL`` environment variable
    (e.g. ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``).  If the
    variable is unset or invalid the *level* parameter is used as a fallback.

    Uses :class:`DevFormatter` (human-readable, colorized) when
    ``LOCAL_DEVELOPMENT`` is enabled, otherwise :class:`StructuredFormatter`
    (single-line JSON for CloudWatch).

    Safe to call multiple times — subsequent calls are no-ops.
    """
    import os

    global _configured
    if _configured:
        return
    _configured = True

    env_level = os.environ.get("LOG_LEVEL", "").strip().upper()
    effective_level = getattr(logging, env_level, None) if env_level else None
    if not isinstance(effective_level, int):
        effective_level = level

    root = logging.getLogger()
    root.setLevel(effective_level)

    # Remove any existing handlers (e.g. Slack Bolt's defaults).
    for h in list(root.handlers):
        root.removeHandler(h)

    local_dev = os.environ.get("LOCAL_DEVELOPMENT", "false").lower() == "true"

    handler = logging.StreamHandler()
    handler.setFormatter(DevFormatter() if local_dev else StructuredFormatter())
    root.addHandler(handler)


# ---------------------------------------------------------------------------
# Metric-event helper
# ---------------------------------------------------------------------------

_metrics_logger = logging.getLogger("syncbot.metrics")
_event_logger = logging.getLogger("syncbot")

_LEVEL_NAMES = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        try:
            return _LEVEL_NAMES[level.strip().upper()]
        except KeyError:
            raise ValueError(f"unknown log level: {level!r}") from None
    raise TypeError(f"level must be str or int, not {type(level).__name__}")


def log(event: str, level: str | int, *, _stacklevel: int = 2, exc_info: bool = False, **fields: Any) -> None:
    """Structured event. *event* is the stable CloudWatch ``message``.

    *level* is required (``"DEBUG"`` / ``"INFO"`` / ``"WARNING"`` / ``"ERROR"``
    / ``"CRITICAL"``, or the ``logging`` int). Prefer ``log_debug`` /
    ``log_info`` / ``log_warning`` / ``log_error`` / ``log_critical``.
    Never pass tokens. Decision points only, not per-loop chatter.
    """
    _event_logger.log(
        _coerce_level(level),
        event,
        extra=redact_sensitive(fields),
        stacklevel=_stacklevel,
        exc_info=exc_info,
    )


def log_debug(event: str, *, exc_info: bool = False, **fields: Any) -> None:
    """``log(event, logging.DEBUG, **fields)``."""
    log(event, logging.DEBUG, _stacklevel=3, exc_info=exc_info, **fields)


def log_info(event: str, *, exc_info: bool = False, **fields: Any) -> None:
    """``log(event, logging.INFO, **fields)``."""
    log(event, logging.INFO, _stacklevel=3, exc_info=exc_info, **fields)


def log_warning(event: str, *, exc_info: bool = False, **fields: Any) -> None:
    """``log(event, logging.WARNING, **fields)``."""
    log(event, logging.WARNING, _stacklevel=3, exc_info=exc_info, **fields)


def log_error(event: str, *, exc_info: bool = False, **fields: Any) -> None:
    """``log(event, logging.ERROR, **fields)``."""
    log(event, logging.ERROR, _stacklevel=3, exc_info=exc_info, **fields)


def log_critical(event: str, *, exc_info: bool = False, **fields: Any) -> None:
    """``log(event, logging.CRITICAL, **fields)``."""
    log(event, logging.CRITICAL, _stacklevel=3, exc_info=exc_info, **fields)


def emit_metric(
    metric_name: str,
    value: float = 1,
    unit: str = "Count",
    **dimensions: Any,
) -> None:
    """Emit a metric as a structured log entry.

    CloudWatch Logs Insights can aggregate these with queries like::

        filter metric_name = "messages_synced"
        | stats sum(metric_value) as total by bin(5m)

    Parameters
    ----------
    metric_name:
        Short snake_case identifier, e.g. ``messages_synced``.
    value:
        Numeric value (default ``1`` for counter-style metrics).
    unit:
        CloudWatch-compatible unit string (``Count``, ``Milliseconds``, …).
    **dimensions:
        Arbitrary key/value pairs attached to the metric event.
    """
    _metrics_logger.info(
        metric_name,
        extra=redact_sensitive(
            {
                "metric_name": metric_name,
                "metric_value": value,
                "metric_unit": unit,
                **dimensions,
            }
        ),
    )
