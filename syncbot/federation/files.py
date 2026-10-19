"""Federation file mailbox: offer, parts, assemble into hashed /tmp."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import DataError, IntegrityError

from constants import file_chunk_bytes, get_file_chunk_mb
from db import close_session, get_session, schemas
from helpers.encryption import decrypt_bytes, encrypt_bytes
from helpers.files import (
    assemble_parts_to_hashed,
    hashed_file_path,
    hashed_file_ready,
    max_file_bytes,
    pin_hashed_file,
)
from logger import log_warning

_PARTS_TTL = timedelta(minutes=5)
_assemble_locks: dict[str, threading.Lock] = {}
_assemble_locks_guard = threading.Lock()


class AssembleFailed(Exception):
    """The part set was complete and decrypt or assemble did not produce the file."""


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _lock_for(sha256: str) -> threading.Lock:
    with _assemble_locks_guard:
        lock = _assemble_locks.get(sha256)
        if lock is None:
            lock = threading.Lock()
            _assemble_locks[sha256] = lock
        return lock


def purge_expired_file_parts(session=None) -> int:
    """Delete abandoned incomplete parts older than TTL. Returns rows deleted."""
    own_session = session is None
    if own_session:
        session = get_session()
    try:
        cutoff = _utcnow() - _PARTS_TTL
        deleted = (
            session.query(schemas.FederationFilePart)
            .filter(schemas.FederationFilePart.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        if own_session:
            session.commit()
        return int(deleted or 0)
    except Exception:
        if own_session:
            session.rollback()
        raise
    finally:
        if own_session:
            close_session(session)


def delete_parts_for_sha(sha256: str, session=None) -> None:
    own_session = session is None
    if own_session:
        session = get_session()
    try:
        session.query(schemas.FederationFilePart).filter(schemas.FederationFilePart.sha256 == sha256).delete(
            synchronize_session=False
        )
        if own_session:
            session.commit()
    except Exception:
        if own_session:
            session.rollback()
        raise
    finally:
        if own_session:
            close_session(session)


def _parts_complete_from_rows(rows: list) -> bool:
    """True when *rows* are ``(part_index, part_total)`` covering ``0..total-1``.

    ``part_total`` comes from the sender. Inferring total from max(index)+1 would
    treat part 0 of N as a complete one-part file. Leftover rows with a null
    total are incomplete until they expire or are rewritten.
    """
    if not rows:
        return False
    indexes = [int(r[0]) for r in rows]
    totals = [int(r[1]) for r in rows if r[1] is not None]
    if not totals or len(set(totals)) != 1:
        return False
    total = totals[0]
    if total < 1:
        return False
    return set(indexes) == set(range(total))


def parts_complete(sha256: str, expected_total: int | None = None) -> bool:
    """True when every part index from 0..n-1 is present (and not expired)."""
    session = get_session()
    try:
        purge_expired_file_parts(session)
        rows = (
            session.query(
                schemas.FederationFilePart.part_index,
                schemas.FederationFilePart.part_total,
            )
            .filter(schemas.FederationFilePart.sha256 == sha256)
            .all()
        )
        session.commit()
        if expected_total is not None:
            indexes = {int(r[0]) for r in rows}
            return bool(indexes) and indexes == set(range(int(expected_total)))
        return _parts_complete_from_rows(rows)
    except Exception:
        session.rollback()
        raise
    finally:
        close_session(session)


def offer_have(sha256: str, size: int) -> bool:
    """True if /tmp hit or complete in-flight parts for *sha256*."""
    if hashed_file_ready(sha256, size):
        pin_hashed_file(sha256)
        return True
    session = get_session()
    try:
        purge_expired_file_parts(session)
        rows = (
            session.query(
                schemas.FederationFilePart.part_index,
                schemas.FederationFilePart.part_total,
            )
            .filter(schemas.FederationFilePart.sha256 == sha256)
            .all()
        )
        session.commit()
        return _parts_complete_from_rows(rows)
    except Exception:
        session.rollback()
        raise
    finally:
        close_session(session)


def upsert_file_part(
    *,
    sha256: str,
    part_index: int,
    total: int,
    size: int,
    payload: bytes,
) -> tuple[int, dict]:
    """Store one part. Returns ``(status_code, body)``."""
    chunk_cap = file_chunk_bytes()
    if chunk_cap is not None and len(payload) > chunk_cap:
        return 413, {"error": "payload_too_large", "file_chunk_mb": get_file_chunk_mb()}
    if len(payload) > max_file_bytes() or int(size) > max_file_bytes():
        return 413, {"error": "payload_too_large", "file_chunk_mb": get_file_chunk_mb()}
    if part_index < 0 or total < 1 or part_index >= total:
        return 400, {"error": "invalid_part"}

    session = get_session()
    try:
        purge_expired_file_parts(session)
        existing = (
            session.query(schemas.FederationFilePart)
            .filter(
                schemas.FederationFilePart.sha256 == sha256,
                schemas.FederationFilePart.part_index == part_index,
            )
            .one_or_none()
        )
        now = _utcnow()
        stored = encrypt_bytes(payload)
        if existing:
            existing.payload = stored
            existing.part_total = total
            existing.created_at = now
        else:
            session.add(
                schemas.FederationFilePart(
                    sha256=sha256,
                    part_index=part_index,
                    part_total=total,
                    payload=stored,
                    created_at=now,
                )
            )
        session.commit()
        return 200, {"ok": True}
    except IntegrityError:
        session.rollback()
        return 200, {"ok": True}
    except DataError:
        session.rollback()
        return 413, {"error": "payload_too_large", "file_chunk_mb": get_file_chunk_mb()}
    except Exception:
        session.rollback()
        raise
    finally:
        close_session(session)


def materialize_file(sha256: str, size: int) -> str | None:
    """Ensure ``/tmp/sb-file-{sha256}`` exists; assemble from parts if needed.

    Returns path or None when the part set is incomplete. Deletes parts after a
    good assemble. Raises ``AssembleFailed`` when the set is complete and
    decrypt or assemble does not produce the file.
    """
    lock = _lock_for(sha256)
    with lock:
        if hashed_file_ready(sha256, size):
            pin_hashed_file(sha256)
            return hashed_file_path(sha256)

        session = get_session()
        try:
            purge_expired_file_parts(session)
            index_rows = (
                session.query(
                    schemas.FederationFilePart.part_index,
                    schemas.FederationFilePart.part_total,
                )
                .filter(schemas.FederationFilePart.sha256 == sha256)
                .all()
            )
            if not _parts_complete_from_rows(index_rows):
                session.commit()
                return None
            total = int(index_rows[0][1])

            def _iter_payloads():
                for index in range(total):
                    row = (
                        session.query(schemas.FederationFilePart)
                        .filter(
                            schemas.FederationFilePart.sha256 == sha256,
                            schemas.FederationFilePart.part_index == index,
                        )
                        .one()
                    )
                    payload = decrypt_bytes(bytes(row.payload or b""))
                    session.expire(row)
                    yield payload or b""

            try:
                path = assemble_parts_to_hashed(_iter_payloads(), expected_sha256=sha256, expected_size=size)
            except Exception:
                session.rollback()
                log_warning("materialize_file_failed", sha256=sha256)
                raise AssembleFailed(sha256) from None
            session.query(schemas.FederationFilePart).filter(schemas.FederationFilePart.sha256 == sha256).delete(
                synchronize_session=False
            )
            session.commit()
            return path
        except AssembleFailed:
            raise
        except Exception:
            session.rollback()
            log_warning("materialize_file_failed", sha256=sha256)
            return None
        finally:
            close_session(session)


def file_chunk_mb_response() -> int:
    return get_file_chunk_mb()
