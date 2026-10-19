"""Full EventContext builders for target-post unit tests."""

from __future__ import annotations

from typing import Any

from handlers._common import EventContext


def make_event_context(**overrides: Any) -> EventContext:
    """Return a complete :class:`EventContext` with sensible defaults for tests."""
    ctx: EventContext = {
        "team_id": "T1",
        "channel_id": "C_SRC",
        "user_id": "U1",
        "msg_text": "",
        "mentioned_users": [],
        "thread_ts": None,
        "ts": "1.000000",
        "event_subtype": None,
        "reply_broadcast": False,
        "content_blocks": [],
    }
    ctx.update(overrides)  # type: ignore[typeddict-item]
    return ctx
