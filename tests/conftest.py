"""Pytest configuration: default DB backend for unit tests (no live DB required)."""

import os

# In-memory SQLite so importing `app` (which calls initialize_database) works without MySQL.
# CI has no .env; production-required Slack vars must be present unless LOCAL_DEVELOPMENT=true.
os.environ.setdefault("LOCAL_DEVELOPMENT", "true")
os.environ.setdefault("DATABASE_BACKEND", "sqlite")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")
os.environ.setdefault("SLACK_CLIENT_ID", "111.222")
os.environ.setdefault("SLACK_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("SLACK_BOT_SCOPES", "chat:write")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "test-encryption-key-16")

import sqlite3  # noqa: E402

from sqlalchemy import event  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
    """Enforce foreign keys on SQLite so FK-ordering bugs fail in tests too.

    SQLite ignores foreign keys unless this pragma is set per connection, which
    is why parent-first deletes that raise MySQL error 1451 in production used
    to pass silently here.
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
