"""Remember user-token Slack writes so matching inbound events can be skipped.

Used when SyncBot acts as a mapped person (``xoxp``). Slack emits a normal
``reaction_added`` / ``reaction_removed`` with ``event.user`` set to that person,
not the bot. Call :func:`remember_user_action` after a successful write and
:func:`take_user_action_echo` inside ``run_claimed`` before fan-out.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from db import close_session, get_session, schemas

_logger = logging.getLogger(__name__)

_TTL = timedelta(minutes=10)


def slack_message_ts(ts: object) -> str:
    """Normalize a Slack message timestamp to six fractional digits.

    Echo remember uses target ``PostMeta.ts`` (Decimal); inbound ``event.item.ts`` is
    Slack's string. ``str(Decimal)`` drops trailing zeros, so the fingerprint must
    not use either form raw.
    """
    raw = str(ts).strip()
    if not raw:
        return raw
    if "." in raw:
        whole, frac = raw.split(".", 1)
        return f"{whole}.{(frac + '000000')[:6]}"
    return f"{raw}.000000"


def reaction_echo_fingerprint(channel_id: str, ts: str, name: str) -> str:
    """Stable key for a native reaction on *channel_id* at *ts*."""
    return f"{channel_id}:{slack_message_ts(ts)}:{name}"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _purge_expired(session) -> None:
    cutoff = _utcnow() - _TTL
    session.query(schemas.UserActionEcho).filter(schemas.UserActionEcho.created_at < cutoff).delete(
        synchronize_session=False
    )


def remember_user_action(team_id: str, user_id: str, kind: str, fingerprint: str) -> None:
    """Record a user-token side effect on *team_id* so the matching event can be ignored."""
    if not team_id or not user_id or not kind or not fingerprint:
        return
    session = get_session()
    try:
        _purge_expired(session)
        session.add(
            schemas.UserActionEcho(
                team_id=team_id,
                user_id=user_id,
                kind=kind,
                fingerprint=fingerprint,
                created_at=_utcnow(),
            )
        )
        session.commit()
    except IntegrityError:
        session.rollback()
    except Exception as exc:
        session.rollback()
        _logger.warning(
            "remember_user_action_failed",
            extra={"kind": kind, "error": str(exc)},
        )
    finally:
        close_session(session)


def take_user_action_echo(team_id: str, user_id: str, kind: str, fingerprint: str) -> bool:
    """If a remembered row exists, delete it and return True (consume-once)."""
    if not team_id or not user_id or not kind or not fingerprint:
        return False
    session = get_session()
    try:
        _purge_expired(session)
        row = (
            session.query(schemas.UserActionEcho)
            .filter(
                schemas.UserActionEcho.team_id == team_id,
                schemas.UserActionEcho.user_id == user_id,
                schemas.UserActionEcho.kind == kind,
                schemas.UserActionEcho.fingerprint == fingerprint,
            )
            .one_or_none()
        )
        if row is None:
            session.commit()
            return False
        session.delete(row)
        session.commit()
        return True
    except Exception as exc:
        session.rollback()
        _logger.warning(
            "take_user_action_echo_failed",
            extra={"kind": kind, "error": str(exc)},
        )
        return False
    finally:
        close_session(session)
