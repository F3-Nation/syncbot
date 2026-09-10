"""Atomic claim/complete/release for Slack Events API ``event_id`` dedup.

Slack delivers events at least once (retries, queued cold starts). Handlers
claim ``(team_id, event_id)`` from the envelope before side effects.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from db.schemas import ProcessedEvent

_logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_COMPLETE = "complete"
_STALE_PENDING = timedelta(seconds=60)
_COMPLETED_TTL = timedelta(days=7)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def slack_event_identity(body: dict) -> tuple[str, str] | None:
    """Return ``(team_id, event_id)`` from the Slack envelope, or None if missing.

    Local fixtures often omit ``event_id``; those events are processed without
    claiming. Production Events API always includes ``event_id``.
    """
    event_id = body.get("event_id")
    if event_id is None or not str(event_id).strip():
        return None
    team_id = body.get("team_id")
    return (str(team_id or ""), str(event_id))


def claim_event(team_id: str, event_id: str) -> bool:
    """Insert a pending row. Return True if this caller should process the event.

    Unique-constraint races return False (another worker claimed it). A pending
    row older than 60s is treated as abandoned (process crash / timeout) and
    reclaimed so Slack retries can recover.
    """
    from db import close_session, get_session

    now = _utcnow()
    for _ in range(2):
        session = get_session()
        try:
            session.add(
                ProcessedEvent(
                    team_id=team_id,
                    event_id=event_id,
                    status=STATUS_PENDING,
                    created_at=now,
                )
            )
            session.commit()
            return True
        except IntegrityError:
            session.rollback()
            row = (
                session.query(ProcessedEvent)
                .filter(ProcessedEvent.team_id == team_id, ProcessedEvent.event_id == event_id)
                .one_or_none()
            )
            if row is None or row.status == STATUS_COMPLETE:
                return False
            created = row.created_at
            if created is not None and (now - created) >= _STALE_PENDING:
                session.delete(row)
                session.commit()
                continue
            return False
        finally:
            close_session(session)
    return False


def complete_event(team_id: str, event_id: str) -> None:
    from db import close_session, get_session

    now = _utcnow()
    session = get_session()
    try:
        session.query(ProcessedEvent).filter(
            ProcessedEvent.team_id == team_id,
            ProcessedEvent.event_id == event_id,
        ).update(
            {ProcessedEvent.status: STATUS_COMPLETE, ProcessedEvent.completed_at: now},
            synchronize_session="fetch",
        )
        cutoff = now - _COMPLETED_TTL
        session.query(ProcessedEvent).filter(
            ProcessedEvent.status == STATUS_COMPLETE,
            ProcessedEvent.completed_at.isnot(None),
            ProcessedEvent.completed_at < cutoff,
        ).delete(synchronize_session=False)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        close_session(session)


def release_event(team_id: str, event_id: str) -> None:
    """Delete a pending claim so a retry can re-run. Completed rows are kept."""
    from db import close_session, get_session

    session = get_session()
    try:
        session.query(ProcessedEvent).filter(
            ProcessedEvent.team_id == team_id,
            ProcessedEvent.event_id == event_id,
            ProcessedEvent.status == STATUS_PENDING,
        ).delete(synchronize_session=False)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        close_session(session)


def run_claimed(body: dict, work: Callable[[], None]) -> None:
    """Run *work* once per Slack ``event_id``. Skip duplicates; release on failure.

    If ``event_id`` is missing, *work* always runs (local fixtures).
    """
    ident = slack_event_identity(body)
    if ident is None:
        work()
        return
    team_id, event_id = ident
    if not claim_event(team_id, event_id):
        _logger.info(
            "skipping_duplicate_slack_event",
            extra={"team_id": team_id, "event_id": event_id},
        )
        return
    try:
        work()
        complete_event(team_id, event_id)
    except Exception:
        release_event(team_id, event_id)
        raise
