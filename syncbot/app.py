"""SyncBot — Slack app that syncs messages across workspaces.

This module is the entry point for both AWS Lambda (via :func:`handler`) and
container/local HTTP mode (``python app.py`` / Cloud Run: listens on :envvar:`PORT`
or port 3000 by default).

All incoming Slack events, actions, view submissions, and slash commands are
dispatched through :func:`main_response`.  In production (non-local), view
submissions first run :func:`view_ack` for the HTTP response, then :func:`main_response`
for the work phase (lazy).  Handlers are looked up in :data:`routing.MAIN_MAPPER`
and :data:`routing.VIEW_ACK_MAPPER`.

Federation API endpoints (``/api/federation/*``) handle cross-instance
communication and are dispatched separately from Slack events.
"""

import json
import logging
import os
import re
from importlib.metadata import PackageNotFoundError, version

from dotenv import load_dotenv

try:
    __version__ = version("syncbot")
except PackageNotFoundError:
    __version__ = "dev"

# Load .env before any other app imports so env vars are available everywhere.
# In production (Lambda) there is no .env file and this is a harmless no-op.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from http.server import BaseHTTPRequestHandler, HTTPServer

from slack_bolt import App
from slack_bolt.request import BoltRequest
from slack_bolt.response import BoltResponse
from slack_bolt.util.utils import get_boot_message

# SlackRequestHandler requires boto3, which is pre-installed on AWS Lambda but
# not in generic containers (e.g. Cloud Run).  Defer the import so non-Lambda
# deployments don't crash at startup.
try:
    from slack_bolt.adapter.aws_lambda import SlackRequestHandler

    _HAS_LAMBDA_ADAPTER = True
except ImportError:
    _HAS_LAMBDA_ADAPTER = False

from constants import (
    FEDERATION_ENABLED,
    HAS_REAL_BOT_TOKEN,
    LOCAL_DEVELOPMENT,
    validate_config,
)
from db import initialize_database
from federation.api import dispatch_federation_request
from helpers import get_oauth_flow, get_request_type, safe_get
from logger import (
    configure_logging,
    emit_metric,
    get_request_duration_ms,
    set_correlation_id,
)
from routing import MAIN_MAPPER, VIEW_ACK_MAPPER, VIEW_MAPPER

_SENSITIVE_KEYS = frozenset(
    {
        "token",
        "bot_token",
        "access_token",
        "shared_secret",
        "public_key",
        "private_key",
        "private_key_encrypted",
    }
)


def _redact_sensitive(obj, _depth=0):
    """Return a copy of *obj* with sensitive keys replaced by ``"[REDACTED]"``."""
    if _depth > 10:
        return obj
    if isinstance(obj, dict):
        return {k: "[REDACTED]" if k in _SENSITIVE_KEYS else _redact_sensitive(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_sensitive(v, _depth + 1) for v in obj]
    return obj


if _HAS_LAMBDA_ADAPTER:
    SlackRequestHandler.clear_all_log_handlers()
configure_logging()

validate_config()
initialize_database()

app = App(
    process_before_response=not LOCAL_DEVELOPMENT,
    token_verification_enabled=not LOCAL_DEVELOPMENT or HAS_REAL_BOT_TOKEN,
    oauth_flow=get_oauth_flow(),
)


@app.middleware
def _capture_slack_retry_num(req, resp, next):
    """Expose ``X-Slack-Retry-Num`` on context so message handlers can drop retries."""
    headers = getattr(req, "headers", None) or {}
    vals = headers.get("x-slack-retry-num")
    if vals:
        try:
            v = vals[0] if isinstance(vals, (list, tuple)) else vals
            req.context["slack_retry_num"] = int(v)
        except (ValueError, TypeError, IndexError):
            pass
    return next()


def handler(event: dict, context: dict) -> dict:
    """AWS Lambda entry point.

    Receives an API Gateway proxy event.  Federation API paths
    (``/api/federation/*``) are handled directly; everything else
    is delegated to the Slack Bolt request handler.
    """
    path = event.get("path", "") or event.get("rawPath", "")
    if path.startswith("/api/federation"):
        return _lambda_federation_handler(event)

    if not _HAS_LAMBDA_ADAPTER:
        raise RuntimeError("Lambda handler called but boto3/slack_bolt Lambda adapter not installed")
    slack_request_handler = SlackRequestHandler(app=app)
    return slack_request_handler.handle(event, context)


def _lambda_federation_handler(event: dict) -> dict:
    """Handle a federation API request inside Lambda."""
    method = event.get("httpMethod", "GET")
    path = event.get("path", "")
    body_str = event.get("body", "") or ""
    raw_headers = event.get("headers", {}) or {}
    headers = {k: v for k, v in raw_headers.items()}

    status, resp = dispatch_federation_request(method, path, body_str, headers)
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(resp),
    }


_logger = logging.getLogger(__name__)


def view_ack(body: dict, logger, client, ack, context: dict) -> None:
    """Production ack handler for ``view_submission``: fast response to Slack (3s budget).

    Deferred-ack views use :data:`~routing.VIEW_ACK_MAPPER`; all others get an empty ``ack()``.
    """
    set_correlation_id()
    request_type, request_id = get_request_type(body)
    _logger.info(
        "request_received",
        extra={
            "request_type": request_type,
            "request_id": request_id,
            "team_id": safe_get(body, "team_id"),
            "phase": "view_ack",
        },
    )
    _logger.debug("request_body", extra={"body": json.dumps(_redact_sensitive(body))})

    ack_handler = VIEW_ACK_MAPPER.get(request_id)
    if ack_handler:
        result = ack_handler(body, client, context)
        if isinstance(result, dict):
            ack(**result)
        else:
            ack()
    else:
        ack()


def main_response(body: dict, logger, client, ack, context: dict) -> None:
    """Central dispatcher for every Slack request (lazy work phase in production).

    In production, ``view_submission`` HTTP ack is sent by :func:`view_ack` first;
    this function runs afterward and must not call ``ack()`` again for views.

    In local development, view ack + work run in one invocation: deferred views
    call the ack handler from :data:`~routing.VIEW_ACK_MAPPER`, then the work handler.

    A unique correlation ID is assigned to every incoming request and
    attached to all log entries emitted while processing it.
    """
    set_correlation_id()
    request_type, request_id = get_request_type(body)

    if request_type == "view_submission":
        if LOCAL_DEVELOPMENT:
            ack_handler = VIEW_ACK_MAPPER.get(request_id)
            if ack_handler:
                result = ack_handler(body, client, context)
                if isinstance(result, dict):
                    ack(**result)
                else:
                    ack()
            else:
                ack()
        # Production: ack already sent by view_ack
    else:
        ack()

    _logger.info(
        "request_received",
        extra={
            "request_type": request_type,
            "request_id": request_id,
            "team_id": safe_get(body, "team_id"),
        },
    )
    _logger.debug("request_body", extra={"body": json.dumps(_redact_sensitive(body))})

    run_function = MAIN_MAPPER.get(request_type, {}).get(request_id)
    if run_function:
        try:
            run_function(body, client, logger, context)
            emit_metric(
                "request_handled",
                duration_ms=round(get_request_duration_ms(), 1),
                request_type=request_type,
                request_id=request_id,
            )
        except Exception:
            emit_metric(
                "request_error",
                request_type=request_type,
                request_id=request_id,
            )
            raise
    else:
        if not (request_type == "view_submission" and request_id in VIEW_ACK_MAPPER and request_id not in VIEW_MAPPER):
            _logger.error(
                "no_handler",
                extra={
                    "request_type": request_type,
                    "request_id": request_id,
                },
            )


if LOCAL_DEVELOPMENT:
    ARGS = [main_response]
    LAZY_KWARGS = {}
else:
    ARGS = []
    LAZY_KWARGS = {
        "ack": lambda ack: ack(),
        "lazy": [main_response],
    }

MATCH_ALL_PATTERN = re.compile(".*")
app.event(MATCH_ALL_PATTERN)(*ARGS, **LAZY_KWARGS)
app.action(MATCH_ALL_PATTERN)(*ARGS, **LAZY_KWARGS)
if LOCAL_DEVELOPMENT:
    app.view(MATCH_ALL_PATTERN)(main_response)
else:
    app.view(MATCH_ALL_PATTERN)(ack=view_ack, lazy=[main_response])


def _http_listen_port() -> int:
    """Port for Bolt container mode (Cloud Run sets ``PORT``; local default 3000)."""
    raw = os.environ.get("PORT", "3000").strip()
    try:
        return int(raw)
    except ValueError:
        return 3000


def run_syncbot_http_server(
    *,
    port: int | None = None,
    bolt_path: str = "/slack/events",
    http_server_logger_enabled: bool = True,
) -> None:
    """Start the HTTP server used by Cloud Run and ``python app.py``.

    Serves Slack (``bolt_path``), OAuth install/callback, ``/health``, and
    ``/api/federation/*`` when :data:`~constants.FEDERATION_ENABLED` is true.
    Mirrors :class:`slack_bolt.app.app.SlackAppDevelopmentServer` routing with
    extra paths for production parity with API Gateway + Lambda.
    """
    listen_port = port if port is not None else _http_listen_port()
    _bolt_app = app
    _bolt_oauth_flow = app.oauth_flow
    _bolt_endpoint_path = bolt_path
    _fed_enabled = FEDERATION_ENABLED
    _http_log = http_server_logger_enabled
    _fed_max_body = 1_048_576  # 1 MB

    class SyncBotHTTPHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            if _http_log:
                super().log_message(fmt, *args)

        def _path_no_query(self) -> str:
            return self.path.partition("?")[0]

        def _send_raw(
            self,
            status: int,
            headers: dict[str, list[str]],
            body: str | bytes = "",
        ) -> None:
            if isinstance(body, str):
                body_bytes = body.encode("utf-8")
            else:
                body_bytes = body
            self.send_response(status)
            for k, vs in headers.items():
                for v in vs:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        def _send_bolt_response(self, bolt_resp: BoltResponse) -> None:
            self._send_raw(
                status=bolt_resp.status,
                headers={k: list(vs) for k, vs in bolt_resp.headers.items()},
                body=bolt_resp.body,
            )

        def do_GET(self) -> None:
            path = self._path_no_query()
            if path == "/health":
                self._send_raw(
                    200,
                    {"Content-Type": ["application/json"]},
                    json.dumps({"status": "ok"}),
                )
                return
            if _fed_enabled and path.startswith("/api/federation"):
                self._handle_federation("GET")
                return
            if _bolt_oauth_flow:
                query = self.path.partition("?")[2]
                if path == _bolt_oauth_flow.install_path:
                    bolt_req = BoltRequest(
                        body="",
                        query=query,
                        headers=self.headers,
                    )
                    bolt_resp = _bolt_oauth_flow.handle_installation(bolt_req)
                    self._send_bolt_response(bolt_resp)
                    return
                if path == _bolt_oauth_flow.redirect_uri_path:
                    bolt_req = BoltRequest(
                        body="",
                        query=query,
                        headers=self.headers,
                    )
                    bolt_resp = _bolt_oauth_flow.handle_callback(bolt_req)
                    self._send_bolt_response(bolt_resp)
                    return
            self._send_raw(404, {})

        def do_POST(self) -> None:
            path = self._path_no_query()
            if _fed_enabled and path.startswith("/api/federation"):
                self._handle_federation("POST")
                return
            if path != _bolt_endpoint_path:
                self._send_raw(404, {})
                return
            try:
                content_len = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                content_len = 0
            query = self.path.partition("?")[2]
            request_body = self.rfile.read(content_len).decode("utf-8")
            bolt_req = BoltRequest(
                body=request_body,
                query=query,
                headers=self.headers,
            )
            bolt_resp = _bolt_app.dispatch(bolt_req)
            self._send_bolt_response(bolt_resp)

        def _handle_federation(self, method: str) -> None:
            try:
                content_len = min(
                    int(self.headers.get("Content-Length", 0)),
                    _fed_max_body,
                )
            except (TypeError, ValueError):
                content_len = 0
            body_str = self.rfile.read(content_len).decode() if content_len else ""
            headers = {k: v for k, v in self.headers.items()}
            status, resp = dispatch_federation_request(method, self._path_no_query(), body_str, headers)
            self._send_raw(
                status,
                {"Content-Type": ["application/json"]},
                json.dumps(resp),
            )

    server = HTTPServer(("0.0.0.0", listen_port), SyncBotHTTPHandler)
    if _bolt_app.logger.level > logging.INFO:
        print(get_boot_message(development_server=True))
    else:
        _bolt_app.logger.info(
            "http_server_started",
            extra={"port": listen_port, "bolt_path": bolt_path},
        )
    try:
        server.serve_forever(0.05)
    finally:
        server.server_close()


if __name__ == "__main__":
    run_syncbot_http_server(http_server_logger_enabled=LOCAL_DEVELOPMENT)
