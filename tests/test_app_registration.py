"""Guardrails: Slack Bolt listener wiring for view ack + lazy main_response."""

from pathlib import Path


def test_app_py_view_listener_has_ack_and_lazy_in_prod_branch():
    """Production registers view with view_ack + lazy main_response."""
    root = Path(__file__).resolve().parents[1]
    app_py = root / "syncbot" / "app.py"
    text = app_py.read_text(encoding="utf-8")
    assert "ack=view_ack" in text
    assert "lazy=[main_response]" in text
    assert "app.event(MATCH_ALL_PATTERN)(*ARGS, **LAZY_KWARGS)" in text
    assert "app.action(MATCH_ALL_PATTERN)(ack=action_ack, lazy=[main_response])" in text


def test_bolt_view_listener_uses_view_ack_when_not_local_dev():
    """Bolt view listener should use view_ack as ack_function when not LOCAL_DEVELOPMENT."""
    import app as app_module

    if app_module.LOCAL_DEVELOPMENT:
        return
    bolt_app = app_module.app
    view_ack_listeners = [
        li
        for li in bolt_app._listeners
        if getattr(li.ack_function, "__name__", None) == "view_ack"
        and li.lazy_functions
        and any(getattr(f, "__name__", None) == "main_response" for f in li.lazy_functions)
    ]
    assert view_ack_listeners, "expected view listener with ack_function=view_ack and lazy main_response"


def test_lazy_listener_finishes_on_the_calling_thread():
    """Listener work finishes inside ``start``, on the request thread."""
    import threading

    from slack_bolt.request import BoltRequest

    from app import app as bolt_app

    ran: list[bool] = []

    def _listener(body: dict) -> None:
        ran.append(threading.current_thread() is threading.main_thread())
        assert body["type"] == "event_callback"

    bolt_app.listener_runner.lazy_listener_runner.start(
        _listener,
        BoltRequest(body='{"type": "event_callback"}'),
    )
    assert ran == [True]


def test_bolt_event_or_action_uses_lazy_main_response_in_prod_mode():
    """When not LOCAL_DEVELOPMENT, event/action listeners should defer work to lazy main_response."""
    import app as app_module

    if app_module.LOCAL_DEVELOPMENT:
        return
    bolt_app = app_module.app
    lazy = [
        li
        for li in bolt_app._listeners
        if li.lazy_functions and any(getattr(f, "__name__", None) == "main_response" for f in li.lazy_functions)
    ]
    assert lazy, "expected lazy listeners with main_response when LOCAL_DEVELOPMENT is false"
