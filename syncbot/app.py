"""SyncBot — Slack app that syncs messages across workspaces.

This module is the entry point for both AWS Lambda (via :func:`handler`) and
container/local HTTP mode (``python app.py`` / Cloud Run: listens on :envvar:`PORT`
or port 3000 by default).

All incoming Slack events, actions, view submissions, and slash commands are
dispatched through :func:`main_response`.  In production (non-local), view
submissions first run :func:`view_ack` for the HTTP response, then :func:`main_response`
for the work phase (lazy).  Button modals open in :func:`action_ack` and the work
phase fills that view.  Handlers are looked up in :data:`routing.MAIN_MAPPER`
and :data:`routing.VIEW_ACK_MAPPER`.

Federation API endpoints (``/api/federation/*``) handle cross-instance
communication and are dispatched separately from Slack events.
"""

import contextlib
import json
import logging
import os
import re

from dotenv import load_dotenv

# Load .env before any other app imports so env vars are available everywhere.
# In production there is no .env file and this is a harmless no-op.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from http.server import BaseHTTPRequestHandler, HTTPServer

from slack_bolt import App
from slack_bolt.request import BoltRequest
from slack_bolt.response import BoltResponse
from slack_bolt.util.utils import get_boot_message
from sqlalchemy.exc import OperationalError, ProgrammingError

# Optional: Cloud Run / local images built from requirements.txt do not include boto3.
try:
    from slack_bolt.adapter.aws_lambda import SlackRequestHandler
except ImportError:  # pragma: no cover - exercised in tests via subprocess
    SlackRequestHandler = None

from constants import (
    FEDERATION_API_BASE_PATH,
    HAS_REAL_BOT_TOKEN,
    LOCAL_DEVELOPMENT,
    validate_config,
)
from db import initialize_database
from federation.api import dispatch_federation_request
from federation.core import get_or_create_instance_keypair
from helpers import (
    capture_public_base,
    federation_enabled,
    get_oauth_flow,
    get_request_type,
    get_team_id_from_body,
)
from helpers.oauth import capture_public_base_from_lambda_event
from logger import (
    configure_logging,
    emit_metric,
    get_request_duration_ms,
    log_debug,
    log_error,
    log_info,
    set_correlation_id,
)
from logger import (
    redact_sensitive as _redact_sensitive,
)
from routing import MAIN_MAPPER, MODAL_OPEN_ACTIONS, MODAL_PUSH_ACTIONS, VIEW_ACK_MAPPER, VIEW_MAPPER
from slack import actions, orm

if SlackRequestHandler is not None:
    SlackRequestHandler.clear_all_log_handlers()
configure_logging()

validate_config()


def _ensure_instance_identity() -> None:
    """Mint the self Instance when the migrated schema is available."""
    try:
        get_or_create_instance_keypair()
    except (OperationalError, ProgrammingError):
        # ``sqlite:///:memory:`` with NullPool is connection-scoped (used by
        # import-only unit tests). Real runtimes keep the migrated schema.
        log_debug("instance_identity_deferred")


# On Lambda, defer Alembic to a post-deploy invoke (see handler migrate branch) so cold
# starts stay under Slack's 3s ack budget. Cloud Run / local still run migrations here.
if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    initialize_database()
    _ensure_instance_identity()
else:
    # Lambda deploys migrate once post-deploy. On a normal cold start the
    # schema is already ready, so ensure the self Instance without running
    # Alembic in Slack's request path.
    _ensure_instance_identity()

app = App(
    process_before_response=not LOCAL_DEVELOPMENT,
    token_verification_enabled=not LOCAL_DEVELOPMENT or HAS_REAL_BOT_TOKEN,
    oauth_flow=get_oauth_flow(),
)


class _RequestScopedLazyListenerRunner:
    """Run a lazy listener before ``start`` returns.

    Bolt's default runner queues the listener and returns immediately. This
    process answers the HTTP request itself, so the listener has to finish on
    that request. Work queued past the response can run on a later request and
    land out of order. An adapter may replace this runner when it acks first
    and continues the listener on its own.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def start(self, function, request) -> None:  # noqa: ANN001 — Bolt LazyListenerRunner shape
        from slack_bolt.lazy_listener.internals import build_runnable_function

        build_runnable_function(func=function, logger=self.logger, request=request)()


app.listener_runner.lazy_listener_runner = _RequestScopedLazyListenerRunner(app.logger)


@app.middleware
def _capture_public_base_url(req, resp, next):
    """Remember this request's public origin for /slack/install and federation."""
    capture_public_base(getattr(req, "headers", None), req.context)
    return next()


def complete_instance_ready(*, republish_home: bool = False) -> None:
    """Pulse federation peers; optionally republish remembered Home tabs.

    Keep-warm calls this with ``republish_home=False``. Post-deploy ready
    sets ``republish_home=True``. Never raises.
    """
    from federation.core import refresh_instance

    refresh_instance()
    if republish_home:
        try:
            from builders.home import republish_remembered_home_tabs

            republish_remembered_home_tabs()
        except Exception:
            pass


def handler(event: dict, context: dict) -> dict:
    """AWS Lambda entry point.

    Receives a Lambda Function URL event.  Federation API paths
    (``/api/federation/*``) are handled directly; everything else
    is delegated to the Slack Bolt request handler.

    Also handles post-deploy ``{"action": "migrate"}`` (Alembic),
    ``{"action": "ready"}`` (federation pulse and Home republish), and
    EventBridge keep-warm invokes before Slack routing.
    """
    if event.get("action") == "migrate":
        initialize_database()
        _ensure_instance_identity()
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"status": "ok", "action": "migrate"}),
        }

    if event.get("action") == "ready":
        complete_instance_ready(republish_home=True)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"status": "ok", "action": "ready"}),
        }

    if event.get("source") in ("aws.scheduler", "aws.events"):
        complete_instance_ready(republish_home=False)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"status": "ok", "action": "warmup"}),
        }

    path = event.get("path", "") or event.get("rawPath", "")
    capture_public_base_from_lambda_event(event)
    if path.startswith(FEDERATION_API_BASE_PATH):
        if not federation_enabled():
            return {
                "statusCode": 404,
                "headers": {"Content-Type": "text/plain;charset=utf-8"},
                "body": "Not Found",
            }
        return _lambda_federation_handler(event)

    if _lambda_http_method(event) == "GET" and path not in ("/slack/install", "/slack/oauth_redirect"):
        # Bolt's Lambda adapter treats every GET as OAuth install. A browser
        # favicon hit after /slack/install would issue a new state cookie and
        # the real callback would fail with invalid_browser.
        return {
            "statusCode": 404,
            "headers": {"Content-Type": "text/plain;charset=utf-8"},
            "body": "Not Found",
        }

    capture_public_base(event.get("headers") or {})

    if SlackRequestHandler is None:
        raise RuntimeError(
            "AWS Lambda adapter is unavailable (boto3 / slack_bolt.adapter.aws_lambda missing). "
            "handler() is only for Lambda; use python app.py for Cloud Run."
        )

    slack_request_handler = SlackRequestHandler(app=app)
    return _as_function_url_response(slack_request_handler.handle(event, context))


def _lambda_http_method(event: dict) -> str:
    """HTTP method from a Function URL (payload 2.0) or API Gateway (v1) event."""
    request_context = event.get("requestContext") or {}
    http = request_context.get("http") or {}
    return str(http.get("method") or event.get("httpMethod") or "").upper()


def _as_function_url_response(resp: dict) -> dict:
    """Move ``Set-Cookie`` into the Function URL ``cookies`` array.

    Payload format 2.0 ignores ``Set-Cookie`` in ``headers``, so Bolt's OAuth
    state cookie would never reach the browser and Allow would fail with
    ``invalid_browser``.
    """
    if not isinstance(resp, dict):
        return resp
    headers = resp.get("headers")
    if not headers:
        return resp
    cookies = list(resp.get("cookies") or [])
    new_headers = {}
    moved = False
    for key, value in headers.items():
        if str(key).lower() == "set-cookie":
            moved = True
            if isinstance(value, list | tuple):
                cookies.extend(str(item) for item in value if item)
            elif value:
                cookies.append(str(value))
        else:
            new_headers[key] = value
    if not moved:
        return resp
    out = {**resp, "headers": new_headers}
    if cookies:
        out["cookies"] = cookies
    elif "cookies" in out:
        out = {k: v for k, v in out.items() if k != "cookies"}
    return out


def _lambda_federation_handler(event: dict) -> dict:
    """Handle a federation API request inside Lambda."""
    import base64 as _b64

    method = _lambda_http_method(event) or "GET"
    path = event.get("path", "") or event.get("rawPath", "")
    body_raw = event.get("body", "") or ""
    raw_bytes: bytes | None = None
    body_str = ""
    if event.get("isBase64Encoded") and body_raw:
        try:
            raw_bytes = _b64.b64decode(body_raw)
            # JSON routes need a string; /file keeps raw_bytes.
            try:
                body_str = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                body_str = ""
        except Exception:
            raw_bytes = None
            body_str = ""
    elif isinstance(body_raw, str):
        body_str = body_raw
        raw_bytes = body_raw.encode("utf-8")
    raw_headers = event.get("headers", {}) or {}
    headers = {k: v for k, v in raw_headers.items()}

    status, resp = dispatch_federation_request(method, path, body_str, headers, raw_body=raw_bytes)
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(resp),
    }


def open_loading_modal(body: dict, client) -> None:
    """Open a close-only Loading view for this click. No database and no ``users.info``."""
    request_type, request_id = get_request_type(body)
    if request_type != "block_actions" or request_id not in MODAL_OPEN_ACTIONS:
        return
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        return
    team_id = get_team_id_from_body(body) or ""
    external_id = orm.build_modal_external_id(team_id, trigger_id)
    view = {
        "type": "modal",
        "callback_id": actions.LOADING_MODAL_CALLBACK,
        "external_id": external_id,
        "title": {"type": "plain_text", "text": "Loading..."},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "If this doesn't load, please close and try again."},
            },
        ],
    }
    orm.open_or_push_view(
        client,
        trigger_id,
        view,
        new_or_add="add" if request_id in MODAL_PUSH_ACTIONS else "new",
    )


def action_ack(body: dict, client, ack) -> None:
    """Production ack for button clicks: open the Loading view, then ack."""
    try:
        open_loading_modal(body, client)
    finally:
        ack()


def view_ack(body: dict, logger, client, ack, context: dict) -> None:
    """Production ack handler for ``view_submission``: fast response to Slack (3s budget).

    Deferred-ack views use :data:`~routing.VIEW_ACK_MAPPER`; all others get an empty ``ack()``.
    """
    set_correlation_id()
    request_type, request_id = get_request_type(body)
    log_info(
        "request_received",
        request_type=request_type,
        request_id=request_id,
        team_id=get_team_id_from_body(body),
        phase="view_ack",
    )
    log_debug("request_body", body=json.dumps(_redact_sensitive(body)))

    try:
        ack_handler = VIEW_ACK_MAPPER.get(request_id)
        if ack_handler:
            result = ack_handler(body, client, context)
            if isinstance(result, dict):
                ack(**result)
            else:
                ack()
        else:
            ack()
    except Exception:
        # Slack shows "not responding" if the ack never arrives. Schema errors
        # (missing migration columns) used to raise here and hang the modal.
        log_error("view_ack_failed", request_type=request_type, request_id=request_id, exc_info=True)
        with contextlib.suppress(Exception):
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
    from helpers._cache import begin_request_scope

    begin_request_scope()
    request_type, request_id = get_request_type(body)
    modal_opener = request_type == "block_actions" and request_id in MODAL_OPEN_ACTIONS
    if modal_opener:
        orm.reset_modal_updated()
        if LOCAL_DEVELOPMENT:
            open_loading_modal(body, client)

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

    log_info("request_received", request_type=request_type, request_id=request_id, team_id=get_team_id_from_body(body))
    log_debug("request_body", body=json.dumps(_redact_sensitive(body)))

    run_function = MAIN_MAPPER.get(request_type, {}).get(request_id)
    if run_function:
        try:
            run_function(body, client, logger, context)
            if modal_opener and not orm.modal_was_updated():
                trigger_id = body.get("trigger_id")
                if trigger_id:
                    orm.update_denied_modal(client, body, trigger_id)
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
            log_error("no_handler", request_type=request_type, request_id=request_id)


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
if LOCAL_DEVELOPMENT:
    app.action(MATCH_ALL_PATTERN)(main_response)
else:
    app.action(MATCH_ALL_PATTERN)(ack=action_ack, lazy=[main_response])
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

    Serves Slack (``bolt_path``), OAuth install/callback, ``/health``,
    ``/ready`` (post-deploy Home republish), and ``/api/federation/*`` when
    federation is enabled in Settings.
    Mirrors :class:`slack_bolt.app.app.SlackAppDevelopmentServer` routing with
    extra paths for production parity with Lambda Function URL.
    """
    listen_port = port if port is not None else _http_listen_port()
    _bolt_app = app
    _bolt_oauth_flow = app.oauth_flow
    _bolt_endpoint_path = bolt_path
    _http_log = http_server_logger_enabled

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
                complete_instance_ready(republish_home=False)
                self._send_raw(
                    200,
                    {"Content-Type": ["application/json"]},
                    json.dumps({"status": "ok"}),
                )
                return
            if path == "/ready":
                complete_instance_ready(republish_home=True)
                self._send_raw(
                    200,
                    {"Content-Type": ["application/json"]},
                    json.dumps({"status": "ok", "action": "ready"}),
                )
                return
            if federation_enabled() and path.startswith(FEDERATION_API_BASE_PATH):
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
                    capture_public_base(self.headers)
                    bolt_resp = _bolt_oauth_flow.handle_installation(bolt_req)
                    self._send_bolt_response(bolt_resp)
                    return
                if path == _bolt_oauth_flow.redirect_uri_path:
                    bolt_req = BoltRequest(
                        body="",
                        query=query,
                        headers=self.headers,
                    )
                    capture_public_base(self.headers)
                    bolt_resp = _bolt_oauth_flow.handle_callback(bolt_req)
                    self._send_bolt_response(bolt_resp)
                    return
            self._send_raw(404, {})

        def do_POST(self) -> None:
            path = self._path_no_query()
            if federation_enabled() and path.startswith(FEDERATION_API_BASE_PATH):
                self._handle_federation("POST")
                return
            if path != _bolt_endpoint_path:
                self._send_raw(404, {})
                return
            try:
                content_len = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                content_len = 0
            from constants import file_chunk_bytes

            hop_cap = file_chunk_bytes()
            if hop_cap is not None and content_len > hop_cap:
                self._send_raw(
                    413,
                    {"Content-Type": ["application/json"]},
                    json.dumps({"error": "payload_too_large"}),
                )
                return
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
            from constants import federation_json_max_bytes, file_chunk_bytes
            from helpers.files import max_file_bytes

            path = self._path_no_query()
            headers = {k: v for k, v in self.headers.items()}
            from federation.api import federation_preflight

            early = federation_preflight(method, path, headers)
            if early is not None:
                status, resp = early
                self._send_raw(
                    status,
                    {"Content-Type": ["application/json"]},
                    json.dumps(resp),
                )
                return
            is_file_part = path.endswith("/file") and not path.endswith("/file/offer")
            try:
                declared = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                declared = 0
            if is_file_part:
                part_cap = file_chunk_bytes()
                max_body = part_cap if part_cap is not None else max_file_bytes()
            else:
                max_body = federation_json_max_bytes()
            if max_body is not None and declared > max_body:
                self._send_raw(
                    413,
                    {"Content-Type": ["application/json"]},
                    json.dumps({"error": "payload_too_large"}),
                )
                return
            if declared > 0:
                content_len = min(declared, max_body) if max_body is not None else declared
            else:
                content_len = 0
            raw = self.rfile.read(content_len) if content_len else b""
            body_str = ""
            if not is_file_part:
                try:
                    body_str = raw.decode("utf-8")
                except UnicodeDecodeError:
                    body_str = ""
            status, resp = dispatch_federation_request(
                method,
                path,
                body_str,
                headers,
                raw_body=raw,
            )
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
