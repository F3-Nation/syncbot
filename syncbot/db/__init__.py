"""Database engine, session management, and the :class:`DbManager` CRUD helper.

Key design decisions:

* **Connection pooling** — Uses :class:`~sqlalchemy.pool.QueuePool` with
  ``pool_pre_ping=True`` so that warm Lambda containers reuse connections
  while stale ones are transparently replaced.
* **Automatic retry** — The :func:`_with_retry` decorator retries transient
  :class:`~sqlalchemy.exc.OperationalError` (lost connection, deadlock) up to
  ``_MAX_RETRIES`` times. Schema and syntax errors (unknown column, 1064) are
  not retried — those burned Slack's 3s ack window and the Lambda migrate
  timeout when a migration had not applied.
"""

import logging
import os
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from urllib.parse import quote_plus

from sqlalchemy import and_, create_engine, func, pool, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import sessionmaker

import constants
from db.schemas import BaseClass

_logger = logging.getLogger(__name__)


@dataclass
class DatabaseField:
    name: str
    value: object = None


GLOBAL_ENGINE = None
GLOBAL_SESSION = None
GLOBAL_SCHEMA = None

# Maximum number of times to retry a DB operation on a transient connection error
_MAX_RETRIES = 2
_DB_INIT_MAX_ATTEMPTS = 15
_DB_INIT_RETRY_SECONDS = 2
# pymysql/MySQL errnos that are schema, syntax, or auth — never a stale connection.
_NON_RETRYABLE_DBAPI_ERRNOS = frozenset(
    {
        1044,  # access denied to database
        1045,  # access denied
        1049,  # unknown database
        1054,  # unknown column
        1062,  # duplicate entry
        1064,  # SQL syntax
        1142,  # command denied
        1146,  # table doesn't exist
        1171,  # all parts of a PRIMARY KEY must be NOT NULL
        1215,  # cannot add foreign key
    }
)
_NON_RETRYABLE_SQL_FRAGMENTS = (
    "unknown column",
    "syntax error",
    "you have an error in your sql syntax",
    "doesn't exist",
    "does not exist",
    "undefined column",
    "undefined table",
    "no such table",
    "no such column",
)
# Migrations live next to this package so they are included in the Lambda bundle (SAM CodeUri: syncbot/).
_ALEMBIC_SCRIPT_LOCATION = Path(__file__).resolve().parent / "alembic"

# Repo root locally; Lambda deployment root (/var/task) in AWS — used for relative SQLite paths.
_syncbot_dir = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _syncbot_dir if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") else _syncbot_dir.parent


def _mysql_port() -> str:
    return os.environ.get(constants.DATABASE_PORT, "3306")


def _pg_port() -> str:
    return os.environ.get(constants.DATABASE_PORT, "5432")


def _build_mysql_url(include_schema: bool = False) -> tuple[str, dict]:
    """Build MySQL URL and connect_args from DATABASE_* env vars."""
    host = os.environ[constants.DATABASE_HOST]
    user = quote_plus(os.environ[constants.DATABASE_USER])
    passwd = quote_plus(os.environ[constants.DATABASE_PASSWORD])
    schema = os.environ.get(constants.DATABASE_SCHEMA, "syncbot")
    path = f"/{schema}" if include_schema else ""
    port = _mysql_port()
    db_url = f"mysql+pymysql://{user}:{passwd}@{host}:{port}{path}?charset=utf8mb4"
    connect_args: dict = {}
    if constants.database_tls_enabled():
        ca_path = constants.database_ssl_ca_path()
        if ca_path:
            try:
                ssl_ctx = ssl.create_default_context(cafile=ca_path)
            except (OSError, ssl.SSLError):
                ssl_ctx = ssl.create_default_context()
        else:
            ssl_ctx = ssl.create_default_context()
        connect_args["ssl"] = ssl_ctx
    return db_url, connect_args


def _build_postgresql_url(include_schema: bool = False) -> tuple[str, dict]:
    """Build PostgreSQL URL and connect_args from DATABASE_* env vars."""
    host = os.environ[constants.DATABASE_HOST]
    user = quote_plus(os.environ[constants.DATABASE_USER])
    passwd = quote_plus(os.environ[constants.DATABASE_PASSWORD])
    schema = os.environ.get(constants.DATABASE_SCHEMA, "syncbot")
    port = _pg_port()
    # Target database: schema name maps to PostgreSQL database name (same as MySQL DB name).
    dbname = schema if include_schema else "postgres"
    db_url = f"postgresql+psycopg2://{user}:{passwd}@{host}:{port}/{dbname}"
    connect_args: dict = {}
    if constants.database_tls_enabled():
        ca_path = constants.database_ssl_ca_path()
        connect_args["sslmode"] = "verify-full"
        if ca_path and os.path.isfile(ca_path):
            connect_args["sslrootcert"] = ca_path
    return db_url, connect_args


def _network_sql_connect_args_from_url() -> dict:
    """TLS connect_args when using DATABASE_URL for MySQL or PostgreSQL."""
    connect_args: dict = {}
    if not constants.database_tls_enabled():
        return connect_args
    backend = constants.get_database_backend()
    ca_path = constants.database_ssl_ca_path()
    if backend == "mysql":
        if ca_path:
            try:
                ssl_ctx = ssl.create_default_context(cafile=ca_path)
            except (OSError, ssl.SSLError):
                ssl_ctx = ssl.create_default_context()
        else:
            ssl_ctx = ssl.create_default_context()
        connect_args["ssl"] = ssl_ctx
    elif backend == "postgresql":
        connect_args["sslmode"] = "verify-full"
        if ca_path and os.path.isfile(ca_path):
            connect_args["sslrootcert"] = ca_path
    return connect_args


def _get_database_url_and_args(schema: str = None) -> tuple[str, dict]:
    """Return (url, connect_args) for the configured backend. Dialect-aware."""
    backend = constants.get_database_backend()
    if backend == "sqlite":
        url = os.environ.get(constants.DATABASE_URL) or "sqlite:///db.sqlite3"
        # Ensure path is absolute for SQLite when file path is used
        if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
            path_part = url[10:]
            if not path_part.startswith("/") and ":" not in path_part[:2]:
                url = f"sqlite:///{_PROJECT_ROOT / path_part}"
        connect_args = {"check_same_thread": False}
        return url, connect_args
    if backend == "postgresql":
        if os.environ.get(constants.DATABASE_URL):
            url = os.environ[constants.DATABASE_URL]
            return url, _network_sql_connect_args_from_url()
        return _build_postgresql_url(include_schema=True)
    # mysql
    if os.environ.get(constants.DATABASE_URL):
        url = os.environ[constants.DATABASE_URL]
        return url, _network_sql_connect_args_from_url()
    return _build_mysql_url(include_schema=True)


def _is_sqlite(engine) -> bool:
    return engine.dialect.name == "sqlite"


def _is_network_sql_backend() -> bool:
    return constants.get_database_backend() in ("mysql", "postgresql")


def _alembic_config():
    """Build Alembic config with script_location set to syncbot/db/alembic."""
    from alembic.config import Config  # pyright: ignore[reportMissingImports]

    config = Config()
    config.set_main_option("script_location", str(_ALEMBIC_SCRIPT_LOCATION))
    return config


def _run_alembic_upgrade() -> None:
    """Run Alembic upgrade head to apply pending migrations."""
    from alembic import command  # pyright: ignore[reportMissingImports]

    config = _alembic_config()
    command.upgrade(config, "head")


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """``exc`` plus ``orig`` / ``__cause__``, without loops."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    queue: list[BaseException] = [exc]
    while queue:
        current = queue.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        orig = getattr(current, "orig", None)
        if isinstance(orig, BaseException):
            queue.append(orig)
        if current.__cause__ is not None:
            queue.append(current.__cause__)
    return chain


def _dbapi_errno(exc: BaseException) -> int | None:
    """Return the wrapped DB-API errno when the driver exposes one."""
    orig = getattr(exc, "orig", None)
    target = orig if orig is not None else exc
    args = getattr(target, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _is_retryable_db_error(exc: BaseException) -> bool:
    """True only for transient connectivity/lock errors, not schema or syntax bugs.

    pymysql maps unknown-column (1054) to :class:`~sqlalchemy.exc.OperationalError`,
    so a blanket OperationalError retry would spin Slack's 3s ack budget and
    Lambda's migrate timeout on a migration that never applied. Alembic may wrap
    the driver error, so the whole exception chain is inspected.
    """
    chain = _exception_chain(exc)
    for item in chain:
        if isinstance(item, ProgrammingError | IntegrityError):
            return False
        errno = _dbapi_errno(item)
        if errno in _NON_RETRYABLE_DBAPI_ERRNOS:
            return False
        msg = str(item).lower()
        if any(fragment in msg for fragment in _NON_RETRYABLE_SQL_FRAGMENTS):
            return False
    return any(isinstance(item, OperationalError) for item in chain)


def initialize_database() -> None:
    """Apply Alembic migrations. Does not ``CREATE DATABASE``.

    The operator must create the database and app user before first migrate
    (see docs/DEPLOY.md). SQLite creates the file on connect.
    """
    for attempt in range(1, _DB_INIT_MAX_ATTEMPTS + 1):
        try:
            _run_alembic_upgrade()
            return
        except Exception as exc:
            retryable = _is_retryable_db_error(exc)
            if not retryable or attempt >= _DB_INIT_MAX_ATTEMPTS:
                _logger.error(
                    "db_init_failed",
                    extra={
                        "attempts": attempt,
                        "retryable": retryable,
                        "error": str(exc),
                    },
                )
                if _is_network_sql_backend():
                    raise RuntimeError(
                        "Database init failed. Create the database and app user first "
                        "(see docs/DEPLOY.md), then retry migrate."
                    ) from exc
                raise
            _logger.warning(
                "db_init_retrying",
                extra={"attempt": attempt, "max_attempts": _DB_INIT_MAX_ATTEMPTS, "error": str(exc)},
            )
            time.sleep(_DB_INIT_RETRY_SECONDS)


def _drop_all_tables_dialect_aware(engine) -> None:
    """Drop all tables in the current schema. MySQL / PostgreSQL / SQLite dialect-aware."""
    if _is_sqlite(engine):
        from sqlalchemy import MetaData

        meta = MetaData()
        meta.reflect(bind=engine)
        with engine.begin() as conn:
            for table in reversed(meta.sorted_tables):
                table.drop(conn, checkfirst=True)
        return
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            result = conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
            )
            for (table_name,) in result:
                conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))
        return
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        result = conn.execute(text("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"))
        for (table_name,) in result:
            conn.execute(text(f"DROP TABLE IF EXISTS `{table_name}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


def drop_and_init_db() -> None:
    """Empty the current schema and reinitialize via Alembic. All data is lost.

    Drops all tables dialect-aware, then runs Alembic upgrade head.
    Called from the "Reset Database" UI button (gated by PRIMARY_WORKSPACE + ENABLE_DB_RESET).
    Resets GLOBAL_ENGINE and GLOBAL_SESSION so the next get_engine() uses a fresh DB.
    """
    global GLOBAL_ENGINE, GLOBAL_SESSION, GLOBAL_SCHEMA

    _logger.critical("DB RESET: emptying schema and reinitializing via Alembic. All data will be lost.")

    db_url, connect_args = _get_database_url_and_args()
    engine = create_engine(
        db_url,
        connect_args=connect_args,
        poolclass=pool.NullPool if constants.get_database_backend() == "sqlite" else pool.QueuePool,
        pool_pre_ping=_is_network_sql_backend(),
    )

    _drop_all_tables_dialect_aware(engine)

    engine.dispose()

    GLOBAL_ENGINE = None
    GLOBAL_SESSION = None
    GLOBAL_SCHEMA = None
    # Recreate schema via Alembic upgrade head.
    initialize_database()
    _logger.info("drop_and_init_db: schema emptied and reinitialized via Alembic")


def get_engine(echo: bool = False, schema: str = None):
    """Return the global SQLAlchemy engine, creating it on first call.

    Uses QueuePool with pool_pre_ping for MySQL/PostgreSQL; NullPool for SQLite.
    """
    global GLOBAL_ENGINE, GLOBAL_SCHEMA

    backend = constants.get_database_backend()
    target_schema = (
        (schema or os.environ.get(constants.DATABASE_SCHEMA, "syncbot")) if backend in ("mysql", "postgresql") else ""
    )
    cache_key = target_schema or backend

    if cache_key == GLOBAL_SCHEMA and GLOBAL_ENGINE is not None:
        return GLOBAL_ENGINE

    db_url, connect_args = _get_database_url_and_args(schema=target_schema or None)

    if backend == "sqlite":
        GLOBAL_ENGINE = create_engine(
            db_url,
            echo=echo,
            poolclass=pool.NullPool,
            connect_args=connect_args,
        )
    else:
        GLOBAL_ENGINE = create_engine(
            db_url,
            echo=echo,
            poolclass=pool.QueuePool,
            pool_size=1,
            max_overflow=1,
            pool_recycle=3600,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
    GLOBAL_SCHEMA = cache_key
    return GLOBAL_ENGINE


def get_session(echo: bool = False, schema: str = None):
    if GLOBAL_SESSION:
        return GLOBAL_SESSION
    engine = get_engine(echo=echo, schema=schema)
    return sessionmaker(bind=engine)()


def close_session(session):
    """Close the session (return the connection to the pool)."""
    if session is not None:
        session.close()


T = TypeVar("T")


def _with_retry(fn):
    """Decorator that retries a DB operation on transient OperationalErrors.

    Relies on ``pool_pre_ping=True`` to replace stale connections between
    retries.  Only disposes the engine after all retries are exhausted to
    avoid disrupting other in-flight queries sharing the pool.
    """

    def wrapper(*args, **kwargs):
        global GLOBAL_ENGINE
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except OperationalError as exc:
                retryable = _is_retryable_db_error(exc)
                if retryable and attempt < _MAX_RETRIES:
                    _logger.warning(f"DB operation {fn.__name__} failed (attempt {attempt + 1}), retrying: {exc}")
                    continue
                if retryable:
                    _logger.error(f"DB operation {fn.__name__} failed after {_MAX_RETRIES + 1} attempts")
                    if GLOBAL_ENGINE is not None:
                        GLOBAL_ENGINE.dispose()
                else:
                    _logger.error(f"DB operation {fn.__name__} failed (not retryable): {exc}")
                raise

    wrapper.__name__ = fn.__name__
    return wrapper


class DbManager:
    @staticmethod
    @_with_retry
    def get_record(cls: T, id, schema=None) -> T:
        """Return one row whose ``cls.get_id()`` column equals *id*, or ``None``.

        The lookup column is **not** always the integer primary key. Callers
        must pass the value that matches ``get_id()`` (positional or ``id=``
        only — extra keywords such as ``team_id=`` raise ``TypeError``):

        * ``Workspace`` → Slack ``team_id`` (``T…``)
        * ``SyncChannel`` → Slack ``channel_id`` (``C…``)
        * ``PostMeta`` → Slack ``post_id``
        * most other models → integer ``id``

        Integer PK lookups use :meth:`find_records` (``Model.id == n``) or
        helpers such as ``get_workspace_by_id``.
        """
        session = get_session(schema=schema)
        try:
            x = session.query(cls).filter(cls.get_id() == id).first()
            if x:
                session.expunge(x)
            return x
        finally:
            session.rollback()
            close_session(session)

    @staticmethod
    @_with_retry
    def find_records(cls: T, filters, schema=None) -> list[T]:
        session = get_session(schema=schema)
        try:
            records = session.query(cls).filter(and_(*filters)).all()
            for r in records:
                session.expunge(r)
            return records
        finally:
            session.rollback()
            close_session(session)

    @staticmethod
    @_with_retry
    def count_records(cls: T, filters, schema=None) -> int:
        session = get_session(schema=schema)
        try:
            return session.query(func.count(cls.id)).filter(and_(*filters)).scalar() or 0
        finally:
            session.rollback()
            close_session(session)

    @staticmethod
    @_with_retry
    def find_join_records2(left_cls: T, right_cls: T, filters, schema=None) -> list[tuple[T]]:
        session = get_session(schema=schema)
        try:
            records = session.query(left_cls, right_cls).join(right_cls).filter(and_(*filters)).all()
            session.expunge_all()
            return records
        finally:
            session.rollback()
            close_session(session)

    @staticmethod
    @_with_retry
    def find_join_records3(
        left_cls: T, right_cls1: T, right_cls2: T, filters, schema=None, left_join=False
    ) -> list[tuple[T]]:
        session = get_session(schema=schema)
        try:
            records = (
                session.query(left_cls, right_cls1, right_cls2)
                .select_from(left_cls)
                .join(right_cls1, isouter=left_join)
                .join(right_cls2, isouter=left_join)
                .filter(and_(*filters))
                .all()
            )
            session.expunge_all()
            return records
        finally:
            session.rollback()
            close_session(session)

    @staticmethod
    @_with_retry
    def update_record(cls: T, id, fields, schema=None):
        session = get_session(schema=schema)
        try:
            session.query(cls).filter(cls.get_id() == id).update(fields, synchronize_session="fetch")
            session.flush()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            close_session(session)

    @staticmethod
    @_with_retry
    def update_records(cls: T, filters, fields, schema=None):
        session = get_session(schema=schema)
        try:
            session.query(cls).filter(and_(*filters)).update(fields, synchronize_session="fetch")
            session.flush()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            close_session(session)

    @staticmethod
    @_with_retry
    def create_record(record: BaseClass, schema=None) -> BaseClass:
        session = get_session(schema=schema)
        try:
            session.add(record)
            session.flush()
            session.expunge(record)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            close_session(session)
        return record

    @staticmethod
    @_with_retry
    def merge_record(record: BaseClass, schema=None) -> BaseClass:
        """Insert or update a record based on its primary key."""
        session = get_session(schema=schema)
        try:
            merged = session.merge(record)
            session.flush()
            session.expunge(merged)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            close_session(session)
        return merged

    @staticmethod
    @_with_retry
    def create_records(records: list[BaseClass], schema=None):
        session = get_session(schema=schema)
        try:
            session.add_all(records)
            session.flush()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            close_session(session)

    @staticmethod
    @_with_retry
    def delete_record(cls: T, id, schema=None):
        session = get_session(schema=schema)
        try:
            session.query(cls).filter(cls.get_id() == id).delete()
            session.flush()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            close_session(session)

    @staticmethod
    @_with_retry
    def delete_records(cls: T, filters, schema=None):
        session = get_session(schema=schema)
        try:
            session.query(cls).filter(and_(*filters)).delete()
            session.flush()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            close_session(session)
