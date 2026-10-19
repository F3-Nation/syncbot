"""Remember user-token Slack writes so matching inbound events can be skipped.

Used when SyncBot acts as a mapped person (``xoxp``). Slack emits a normal
``reaction_added`` / ``reaction_removed`` or ``file_share`` / ``message`` with
``event.user`` set to that person, not the bot. Call :func:`remember_user_action`
after a successful write (file ids before the file is shared into the Channel).
:func:`take_user_action_echo` is consume-once (reactions).
:func:`has_user_action_echo` peeks without deleting (file ids and message ts). Both run
inside ``run_claimed`` before fan-out.

:func:`slack_message_ts` is the string form (API kwargs and echo fingerprints).
:func:`post_meta_ts` is the Decimal form for ``post_meta.ts``. Do not ``float()``
that column, and do not add a third converter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from db import close_session, get_session, schemas
from logger import log_warning

_TTL = timedelta(minutes=10)
_PENDING_SHARE_KIND = "pending_share"
_PENDING_SHARE_VERSION = "v1"
_REACTION_EVENT_KIND = "reaction_event"


@dataclass(frozen=True)
class PendingFileShare:
    """In-flight target file apply waiting for Slack to emit the share ts."""

    post_id: str
    sync_channel_id: int
    source_user_id: str | None = None
    source_workspace_id: int | None = None
    posted_as_user_id: str | None = None


def slack_message_ts(ts: object) -> str:
    """Six-decimal Slack message ts as a string (API kwargs and echo fingerprints).

    Persist or compare ``post_meta.ts`` with :func:`post_meta_ts`. ``str(Decimal)``
    drops trailing zeros, so fingerprints must not use a raw Decimal or float.
    """
    raw = str(ts).strip()
    if not raw:
        return raw
    if "." in raw:
        whole, frac = raw.split(".", 1)
        return f"{whole}.{(frac + '000000')[:6]}"
    return f"{raw}.000000"


def post_meta_ts(ts: object) -> Decimal:
    """The only value to persist or compare on ``post_meta.ts``.

    That column is DECIMAL(16, 6). A current Slack ts does not fit in a Python
    float, so ``float(ts)`` misses existing rows. Do not add another converter.
    """
    padded = slack_message_ts(ts)
    if not padded:
        raise ValueError("empty Slack message ts")
    try:
        return Decimal(padded)
    except (InvalidOperation, ArithmeticError) as exc:
        raise ValueError(f"invalid Slack message ts {ts!r}") from exc


def reaction_echo_fingerprint(channel_id: str, ts: str, name: str) -> str:
    """Stable key for a native reaction on *channel_id* at *ts*."""
    return f"{channel_id}:{slack_message_ts(ts)}:{name}"


def reaction_event_prefix(channel_id: str, ts: str, name: str) -> str:
    """Prefix for last-write-wins reaction ``event_ts`` rows."""
    return f"{reaction_echo_fingerprint(channel_id, ts, name)}:"


def last_reaction_event_ts(
    team_id: str,
    source_user_id: str,
    channel_id: str,
    ts: str,
    name: str,
) -> str | None:
    """Newest remembered Slack ``event_ts`` for this reaction on the target."""
    if not team_id or not source_user_id or not channel_id or not ts or not name:
        return None
    prefix = reaction_event_prefix(channel_id, ts, name)
    session = get_session()
    try:
        _purge_expired(session)
        rows = (
            session.query(schemas.UserActionEcho)
            .filter(
                schemas.UserActionEcho.team_id == team_id,
                schemas.UserActionEcho.user_id == source_user_id,
                schemas.UserActionEcho.kind == _REACTION_EVENT_KIND,
                schemas.UserActionEcho.fingerprint.startswith(prefix),
            )
            .all()
        )
        session.commit()
        best = None
        for row in rows:
            suffix = (row.fingerprint or "")[len(prefix) :]
            if not suffix:
                continue
            try:
                value = post_meta_ts(suffix)
            except ValueError:
                continue
            if best is None or value > best:
                best = value
        return slack_message_ts(best) if best is not None else None
    except Exception as exc:
        session.rollback()
        log_warning("last_reaction_event_ts_failed", error=str(exc))
        return None
    finally:
        close_session(session)


def remember_reaction_event_ts(
    team_id: str,
    source_user_id: str,
    channel_id: str,
    ts: str,
    name: str,
    event_ts: str,
) -> None:
    """Record that this reaction ``event_ts`` was applied (or was a no-op apply)."""
    if not team_id or not source_user_id or not event_ts:
        return
    remember_user_action(
        team_id,
        source_user_id,
        _REACTION_EVENT_KIND,
        f"{reaction_event_prefix(channel_id, ts, name)}{slack_message_ts(event_ts)}",
    )


def reaction_event_ts_is_stale(
    team_id: str,
    source_user_id: str,
    channel_id: str,
    ts: str,
    name: str,
    event_ts: str,
) -> bool:
    """True when *event_ts* is not newer than the last applied ts for this emoji."""
    last = last_reaction_event_ts(team_id, source_user_id, channel_id, ts, name)
    if last is None:
        return False
    try:
        return post_meta_ts(event_ts) <= post_meta_ts(last)
    except ValueError:
        return False


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
        log_warning("remember_user_action_failed", kind=kind, error=str(exc))
    finally:
        close_session(session)


def _find_echo(session, team_id: str, user_id: str, kind: str, fingerprint: str):
    return (
        session.query(schemas.UserActionEcho)
        .filter(
            schemas.UserActionEcho.team_id == team_id,
            schemas.UserActionEcho.user_id == user_id,
            schemas.UserActionEcho.kind == kind,
            schemas.UserActionEcho.fingerprint == fingerprint,
        )
        .one_or_none()
    )


def has_user_action_echo(team_id: str, user_id: str, kind: str, fingerprint: str) -> bool:
    """True when a remembered row exists. Does not consume it.

    File ids and message ts stay until TTL so Slack can emit more than one
    event for the same write. Reactions still use consume-once.
    """
    if not team_id or not user_id or not kind or not fingerprint:
        return False
    session = get_session()
    try:
        _purge_expired(session)
        row = _find_echo(session, team_id, user_id, kind, fingerprint)
        session.commit()
        return row is not None
    except Exception as exc:
        session.rollback()
        log_warning("has_user_action_echo_failed", kind=kind, error=str(exc))
        return False
    finally:
        close_session(session)


def _encode_pending_share(pending: PendingFileShare) -> str | None:
    """Pack the in-flight apply into ``user_action_echoes.user_id`` (max 100)."""
    parts = [
        _PENDING_SHARE_VERSION,
        pending.post_id,
        str(pending.sync_channel_id),
        pending.source_user_id or "",
        str(pending.source_workspace_id) if pending.source_workspace_id else "",
        pending.posted_as_user_id or "",
    ]
    if any("|" in part for part in parts):
        return None
    payload = "|".join(parts)
    if len(payload) > 100:
        return None
    return payload


def _decode_pending_share(raw: str | None) -> PendingFileShare | None:
    if not raw or not raw.startswith(f"{_PENDING_SHARE_VERSION}|"):
        return None
    parts = raw.split("|")
    if len(parts) != 6 or parts[0] != _PENDING_SHARE_VERSION:
        return None
    _version, post_id, sc_raw, source_user, source_ws, posted_as = parts
    if not post_id or not sc_raw.isdigit():
        return None
    return PendingFileShare(
        post_id=post_id,
        sync_channel_id=int(sc_raw),
        source_user_id=source_user or None,
        source_workspace_id=int(source_ws) if source_ws.isdigit() else None,
        posted_as_user_id=posted_as or None,
    )


def remember_pending_file_share(
    team_id: str,
    channel_id: str,
    file_id: str,
    post_id: str,
    *,
    sync_channel_id: int,
    source_user_id: str | None = None,
    source_workspace_id: int | None = None,
    posted_as_user_id: str | None = None,
) -> None:
    """Remember the in-flight file apply whose share ts was not in the complete response.

    Slack often omits ``shares`` for a few seconds after a successful
    ``files.completeUploadExternal``. The inbound ``file_share`` (own-bot or
    user-token echo) carries the ts. This row is that apply: envelope
    ``post_id``, the target ``sync_channel_id``, and the source fields
    ``apply_target`` would have written. Lookup is Slack ``channel_id`` +
    ``file_id`` (the upload v2 identity), not a membership scan.
    """
    if not team_id or not channel_id or not file_id or not post_id or not sync_channel_id:
        return
    payload = _encode_pending_share(
        PendingFileShare(
            post_id=post_id,
            sync_channel_id=int(sync_channel_id),
            source_user_id=source_user_id,
            source_workspace_id=int(source_workspace_id) if source_workspace_id else None,
            posted_as_user_id=posted_as_user_id,
        )
    )
    if not payload:
        log_warning(
            "pending_share_encode_failed",
            channel_id=channel_id,
            post_id=post_id,
            sync_channel_id=sync_channel_id,
        )
        return
    remember_user_action(team_id, payload, _PENDING_SHARE_KIND, f"{channel_id}:{file_id}")


def _pending_share_row(session, team_id: str, fingerprint: str):
    return (
        session.query(schemas.UserActionEcho)
        .filter(
            schemas.UserActionEcho.team_id == team_id,
            schemas.UserActionEcho.kind == _PENDING_SHARE_KIND,
            schemas.UserActionEcho.fingerprint == fingerprint,
        )
        .order_by(schemas.UserActionEcho.created_at.desc())
        .first()
    )


def find_pending_file_share(team_id: str, channel_id: str, file_id: str) -> PendingFileShare | None:
    """Return the in-flight apply for this Slack file share without consuming it."""
    if not team_id or not channel_id or not file_id:
        return None
    fingerprint = f"{channel_id}:{file_id}"
    session = get_session()
    try:
        _purge_expired(session)
        row = _pending_share_row(session, team_id, fingerprint)
        session.commit()
        return _decode_pending_share(row.user_id) if row is not None else None
    except Exception as exc:
        session.rollback()
        log_warning("find_pending_file_share_failed", error=str(exc))
        return None
    finally:
        close_session(session)


def take_pending_file_share(team_id: str, channel_id: str, file_id: str) -> PendingFileShare | None:
    """Return the in-flight apply for this Slack file share, if any. Consumes the row."""
    if not team_id or not channel_id or not file_id:
        return None
    fingerprint = f"{channel_id}:{file_id}"
    session = get_session()
    try:
        _purge_expired(session)
        row = _pending_share_row(session, team_id, fingerprint)
        if row is None:
            session.commit()
            return None
        pending = _decode_pending_share(row.user_id)
        session.delete(row)
        session.commit()
        return pending
    except Exception as exc:
        session.rollback()
        log_warning("take_pending_file_share_failed", error=str(exc))
        return None
    finally:
        close_session(session)


def take_user_action_echo(team_id: str, user_id: str, kind: str, fingerprint: str) -> bool:
    """If a remembered row exists, delete it and return True (consume-once)."""
    if not team_id or not user_id or not kind or not fingerprint:
        return False
    session = get_session()
    try:
        _purge_expired(session)
        row = _find_echo(session, team_id, user_id, kind, fingerprint)
        if row is None:
            session.commit()
            return False
        session.delete(row)
        session.commit()
        return True
    except Exception as exc:
        session.rollback()
        log_warning("take_user_action_echo_failed", kind=kind, error=str(exc))
        return False
    finally:
        close_session(session)
