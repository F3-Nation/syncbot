"""Alembic env: use SyncBot's engine from db.get_engine().

Run from repo root: ``alembic -c alembic.ini upgrade head``
(with ``syncbot/`` on ``PYTHONPATH`` via ``prepend_sys_path`` in alembic.ini).
"""

import sys
from pathlib import Path

# syncbot/db/alembic/env.py -> syncbot/ (directory that must be on PYTHONPATH for ``import db``)
_SYNCBOT_DIR = Path(__file__).resolve().parent.parent.parent
_REPO_ROOT = _SYNCBOT_DIR.parent
if str(_SYNCBOT_DIR) not in sys.path:
    sys.path.insert(0, str(_SYNCBOT_DIR))

# Load .env when running via CLI (alembic upgrade head)
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

from logging.config import fileConfig  # noqa: E402

from alembic import context  # noqa: E402

from db import get_engine  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use SyncBot's engine (from env vars / DATABASE_URL). Do not use sqlalchemy.url from alembic.ini.
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_engine().url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = get_engine()
    with connectable.connect() as connection:
        # SQLite column drops need a table rebuild (CREATE, copy, DROP, RENAME),
        # and DROP TABLE trips foreign key enforcement from referencing tables
        # even though the table comes straight back. The MySQL equivalent is
        # SET FOREIGN_KEY_CHECKS = 0, which db._drop_all_tables_dialect_aware
        # already relies on. `PRAGMA foreign_keys` is ignored inside a
        # transaction, so it has to be set here rather than in a migration.
        # Driven through the raw DBAPI cursor on purpose: going through the
        # SQLAlchemy connection would open a transaction here, and Alembic's own
        # commit would then nest inside it and be rolled back at close, losing
        # the alembic_version stamp.
        is_sqlite = connection.dialect.name == "sqlite"
        had_foreign_keys = _set_sqlite_foreign_keys(connection, False) if is_sqlite else False

        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            if is_sqlite and had_foreign_keys:
                _set_sqlite_foreign_keys(connection, True)


def _set_sqlite_foreign_keys(connection, enabled: bool) -> bool:
    """Set ``PRAGMA foreign_keys`` and return whether it was previously on."""
    cursor = connection.connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys")
        row = cursor.fetchone()
        was_enabled = bool(row[0]) if row else False
        cursor.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")
        return was_enabled
    finally:
        cursor.close()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
