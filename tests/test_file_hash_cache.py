"""Tests for hashed /tmp file cache and federation file mailbox."""

from __future__ import annotations

import hashlib
import os
from unittest.mock import MagicMock, patch

import pytest

from helpers.files import (
    cleanup_temp_files,
    download_slack_files,
    hashed_file_path,
    hashed_file_ready,
    is_hashed_cache_path,
    pin_hashed_file,
    remember_slack_file_id,
    write_bytes_hashed,
)


def test_write_bytes_hashed_and_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "syncbot")
    data = b"hello-syncbot-file"
    sha, size, path = write_bytes_hashed(data)
    assert sha == hashlib.sha256(data).hexdigest()
    assert size == len(data)
    assert path == hashed_file_path(sha)
    assert hashed_file_ready(sha, size)
    assert is_hashed_cache_path(path)


def test_cleanup_keeps_hashed_files(tmp_path, monkeypatch):
    data = b"keep-me"
    sha, size, path = write_bytes_hashed(data)
    pin_hashed_file(sha)
    other = "/tmp/sb-nonhash-test.bin"
    with open(other, "wb") as fh:
        fh.write(b"x")
    cleanup_temp_files(None, [{"path": path, "sha256": sha}, {"path": other}])
    assert os.path.isfile(path)
    assert not os.path.isfile(other)


def test_download_slack_files_reuses_slack_file_id(monkeypatch):
    data = b"cached-bytes"
    sha, size, path = write_bytes_hashed(data)
    remember_slack_file_id("F123", sha, size)

    client = MagicMock()
    client.token = "xoxb-test"
    logger = MagicMock()

    with patch("helpers.files._download_and_hash") as download:
        got = download_slack_files(
            [
                {
                    "id": "F123",
                    "url_private": "https://files.slack.com/x",
                    "name": "a.pdf",
                    "mimetype": "application/pdf",
                }
            ],
            client,
            logger,
        )
        download.assert_not_called()
    assert len(got) == 1
    assert got[0]["sha256"] == sha
    assert got[0]["path"] == path
    assert got[0]["name"] == "a.pdf"


def test_offer_have_and_assemble_deletes_parts(tmp_path):
    import db as db_mod
    from db import DbManager, initialize_database, schemas
    from federation.files import materialize_file, offer_have, upsert_file_part
    from helpers._cache import clear_all_caches
    from helpers.files import hashed_file_path

    url = f"sqlite:///{tmp_path / 'file_parts.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_session = db_mod.GLOBAL_SESSION
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SESSION = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            clear_all_caches()

            data = b"part-mailbox-bytes"
            sha = hashlib.sha256(data).hexdigest()
            path = hashed_file_path(sha)
            if os.path.isfile(path):
                os.remove(path)

            assert offer_have(sha, len(data)) is False
            status, body = upsert_file_part(
                sha256=sha,
                part_index=0,
                total=1,
                size=len(data),
                payload=data,
            )
            assert status == 200
            stored_rows = DbManager.find_records(
                schemas.FederationFilePart,
                [schemas.FederationFilePart.sha256 == sha],
            )
            assert len(stored_rows) == 1
            stored = bytes(stored_rows[0].payload or b"")
            assert stored != data
            assert stored.startswith(b"gAAAAA")
            assert offer_have(sha, len(data)) is True
            out = materialize_file(sha, len(data))
            assert out == path
            assert os.path.isfile(path)
            remaining = DbManager.find_records(
                schemas.FederationFilePart,
                [schemas.FederationFilePart.sha256 == sha],
            )
            assert remaining == []
        finally:
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SESSION = old_session
            db_mod.GLOBAL_SCHEMA = old_schema
            clear_all_caches()


def test_encrypted_multipart_file_assembles(tmp_path):
    import db as db_mod
    from db import DbManager, initialize_database, schemas
    from federation.files import materialize_file, offer_have, upsert_file_part
    from helpers._cache import clear_all_caches
    from helpers.files import hashed_file_path

    url = f"sqlite:///{tmp_path / 'file_parts_multi.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_session = db_mod.GLOBAL_SESSION
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SESSION = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            clear_all_caches()

            data = b"abcdef" * 20
            sha = hashlib.sha256(data).hexdigest()
            path = hashed_file_path(sha)
            if os.path.isfile(path):
                os.remove(path)

            mid = len(data) // 2
            status, _ = upsert_file_part(sha256=sha, part_index=0, total=2, size=len(data), payload=data[:mid])
            assert status == 200
            assert offer_have(sha, len(data)) is False
            status, _ = upsert_file_part(sha256=sha, part_index=1, total=2, size=len(data), payload=data[mid:])
            assert status == 200
            stored = DbManager.find_records(
                schemas.FederationFilePart,
                [schemas.FederationFilePart.sha256 == sha],
            )
            assert len(stored) == 2
            assert all(bytes(row.payload or b"").startswith(b"gAAAAA") for row in stored)
            assert offer_have(sha, len(data)) is True
            out = materialize_file(sha, len(data))
            assert out == path
            with open(path, "rb") as fh:
                assert fh.read() == data
        finally:
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SESSION = old_session
            db_mod.GLOBAL_SCHEMA = old_schema
            clear_all_caches()


def test_complete_parts_with_bad_size_raise_assemble_failed(tmp_path):
    import db as db_mod
    from db import DbManager, initialize_database, schemas
    from federation.files import AssembleFailed, materialize_file, upsert_file_part
    from helpers._cache import clear_all_caches

    url = f"sqlite:///{tmp_path / 'file_parts_bad_size.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_session = db_mod.GLOBAL_SESSION
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SESSION = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            clear_all_caches()

            data = b"abcdef"
            sha = hashlib.sha256(data).hexdigest()
            status, _ = upsert_file_part(sha256=sha, part_index=0, total=1, size=len(data), payload=data)
            assert status == 200
            with pytest.raises(AssembleFailed):
                materialize_file(sha, len(data) + 1)
            remaining = DbManager.find_records(
                schemas.FederationFilePart,
                [schemas.FederationFilePart.sha256 == sha],
            )
            assert len(remaining) == 1
        finally:
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SESSION = old_session
            db_mod.GLOBAL_SCHEMA = old_schema
            clear_all_caches()


def test_leftover_plaintext_file_part_still_assembles(tmp_path):
    from datetime import UTC, datetime

    import db as db_mod
    from db import DbManager, initialize_database, schemas
    from federation.files import materialize_file, offer_have
    from helpers._cache import clear_all_caches
    from helpers.files import hashed_file_path

    url = f"sqlite:///{tmp_path / 'file_parts_plain.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_session = db_mod.GLOBAL_SESSION
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SESSION = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            clear_all_caches()

            data = b"leftover-plaintext-part"
            sha = hashlib.sha256(data).hexdigest()
            path = hashed_file_path(sha)
            if os.path.isfile(path):
                os.remove(path)

            DbManager.create_record(
                schemas.FederationFilePart(
                    sha256=sha,
                    part_index=0,
                    part_total=1,
                    payload=data,
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            assert offer_have(sha, len(data)) is True
            out = materialize_file(sha, len(data))
            assert out == path
            assert os.path.isfile(path)
        finally:
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SESSION = old_session
            db_mod.GLOBAL_SCHEMA = old_schema
            clear_all_caches()


def test_offer_have_uses_part_total_and_purges_expired(tmp_path):
    from datetime import UTC, datetime, timedelta

    import db as db_mod
    from db import DbManager, initialize_database, schemas
    from federation.files import offer_have, purge_expired_file_parts, upsert_file_part
    from helpers._cache import clear_all_caches

    url = f"sqlite:///{tmp_path / 'file_parts_total.db'}"
    old_engine = db_mod.GLOBAL_ENGINE
    old_session = db_mod.GLOBAL_SESSION
    old_schema = db_mod.GLOBAL_SCHEMA
    with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": url}, clear=False):
        try:
            db_mod.GLOBAL_ENGINE = None
            db_mod.GLOBAL_SESSION = None
            db_mod.GLOBAL_SCHEMA = None
            initialize_database()
            clear_all_caches()

            data = b"part-zero-of-three"
            sha = hashlib.sha256(data).hexdigest()
            status, _ = upsert_file_part(sha256=sha, part_index=0, total=3, size=len(data), payload=data)
            assert status == 200
            stored = DbManager.find_records(
                schemas.FederationFilePart,
                [schemas.FederationFilePart.sha256 == sha],
            )
            assert len(stored) == 1
            ciphertext = bytes(stored[0].payload or b"")
            assert len(ciphertext) > len(data)
            assert offer_have(sha, len(data)) is False
            bad, body = upsert_file_part(sha256=sha, part_index=3, total=3, size=len(data), payload=data)
            assert bad == 400
            assert body["error"] == "invalid_part"

            stale_sha = hashlib.sha256(b"expired-part").hexdigest()
            DbManager.create_record(
                schemas.FederationFilePart(
                    sha256=stale_sha,
                    part_index=0,
                    part_total=1,
                    payload=b"expired-part",
                    created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=6),
                )
            )
            assert purge_expired_file_parts() == 1
            assert offer_have(stale_sha, len(b"expired-part")) is False
        finally:
            db_mod.GLOBAL_ENGINE = old_engine
            db_mod.GLOBAL_SESSION = old_session
            db_mod.GLOBAL_SCHEMA = old_schema
            clear_all_caches()


def test_file_chunk_mb_reads_injected_hop_cap(monkeypatch):
    import constants
    from constants import file_chunk_bytes, get_file_chunk_mb

    constants._FILE_CHUNK_MB_WARNED = False
    monkeypatch.delenv("FILE_CHUNK_MB", raising=False)
    monkeypatch.delenv("FEDERATION_HTTP_MAX_MB", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert get_file_chunk_mb() == 0
    assert file_chunk_bytes() is None
    monkeypatch.setenv("FILE_CHUNK_MB", "4")
    assert get_file_chunk_mb() == 0
    monkeypatch.setenv("K_SERVICE", "syncbot-test")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "syncbot")
    assert get_file_chunk_mb() == 0
    monkeypatch.setenv("FEDERATION_HTTP_MAX_MB", "32")
    assert get_file_chunk_mb() == 32
    assert file_chunk_bytes() == 32 * 1024 * 1024
    monkeypatch.setenv("FEDERATION_HTTP_MAX_MB", "4")
    assert get_file_chunk_mb() == 4
    assert file_chunk_bytes() == 4 * 1024 * 1024
    monkeypatch.setenv("FEDERATION_HTTP_MAX_MB", "0")
    assert get_file_chunk_mb() == 0
    monkeypatch.setenv("FEDERATION_HTTP_MAX_MB", "nope")
    assert get_file_chunk_mb() == 0


def test_json_chunk_mb_follows_injected_hop_cap(monkeypatch):
    from constants import federation_json_max_bytes, json_chunk_mb

    monkeypatch.delenv("FEDERATION_HTTP_MAX_MB", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert json_chunk_mb() == 0
    assert federation_json_max_bytes() is None
    monkeypatch.setenv("FEDERATION_HTTP_MAX_MB", "4")
    assert json_chunk_mb() == 4
    assert federation_json_max_bytes() == 4 * 1024 * 1024
    monkeypatch.setenv("FEDERATION_HTTP_MAX_MB", "16")
    assert json_chunk_mb() == 16
    assert federation_json_max_bytes() == 16 * 1024 * 1024
