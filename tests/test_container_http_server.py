"""Tests for Cloud Run / container HTTP server helpers in ``app``."""

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest


def test_http_listen_port_from_env() -> None:
    from app import _http_listen_port

    with patch.dict(os.environ, {"PORT": "8080"}):
        assert _http_listen_port() == 8080


def test_http_listen_port_invalid_falls_back() -> None:
    from app import _http_listen_port

    with patch.dict(os.environ, {"PORT": "nope"}):
        assert _http_listen_port() == 3000


def test_health_endpoint_on_container_server() -> None:
    """GET ``/health`` returns 200 and JSON (same server path as Cloud Run)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    def serve() -> None:
        from app import run_syncbot_http_server

        run_syncbot_http_server(port=port, http_server_logger_enabled=False)

    threading.Thread(target=serve, daemon=True).start()

    url = f"http://127.0.0.1:{port}/health"
    last_err: BaseException | None = None
    for _ in range(100):
        try:
            with urllib.request.urlopen(url, timeout=0.3) as r:
                assert r.status == 200
                assert json.loads(r.read().decode()) == {"status": "ok"}
                return
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            time.sleep(0.05)
    pytest.fail(f"/health never became ready: {last_err!r}")


def test_ready_endpoint_on_container_server() -> None:
    """GET ``/ready`` returns 200 JSON after Home republish."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    def serve() -> None:
        from app import run_syncbot_http_server

        run_syncbot_http_server(port=port, http_server_logger_enabled=False)

    threading.Thread(target=serve, daemon=True).start()

    url = f"http://127.0.0.1:{port}/ready"
    last_err: BaseException | None = None
    for _ in range(100):
        try:
            with urllib.request.urlopen(url, timeout=0.3) as r:
                assert r.status == 200
                body = json.loads(r.read().decode())
                assert body["status"] == "ok"
                assert body["action"] == "ready"
                return
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            time.sleep(0.05)
    pytest.fail(f"/ready never became ready: {last_err!r}")


def test_slack_events_oversize_413_on_container_server(monkeypatch) -> None:
    """POST ``/slack/events`` over this hop's injected HTTP cap is 413 before Bolt reads the body."""
    import socket as socklib

    monkeypatch.setenv("FEDERATION_HTTP_MAX_MB", "1")
    monkeypatch.setattr("app.federation_enabled", lambda: False)

    hop_cap = 1024 * 1024

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    def serve() -> None:
        from app import run_syncbot_http_server

        run_syncbot_http_server(port=port, http_server_logger_enabled=False)

    threading.Thread(target=serve, daemon=True).start()

    last_err: BaseException | None = None
    for _ in range(100):
        try:
            probe = socklib.create_connection(("127.0.0.1", port), timeout=0.3)
            probe.close()
            break
        except OSError as e:
            last_err = e
            time.sleep(0.05)
    else:
        pytest.fail(f"HTTP server never became ready: {last_err!r}")

    conn = socklib.create_connection(("127.0.0.1", port), timeout=2)
    request = (
        f"POST /slack/events HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Content-Length: {hop_cap + 1}\r\n"
        f"Content-Type: application/json\r\n"
        f"\r\n"
    )
    conn.sendall(request.encode())
    conn.settimeout(0.2)
    chunks: list[bytes] = []
    deadline = time.monotonic() + 2
    while b"payload_too_large" not in b"".join(chunks) and time.monotonic() < deadline:
        try:
            more = conn.recv(4096)
        except TimeoutError:
            continue
        if not more:
            break
        chunks.append(more)
    conn.close()
    response = b"".join(chunks)
    assert b"413" in response
    assert b"payload_too_large" in response
