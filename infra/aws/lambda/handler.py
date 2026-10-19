"""Lambda entry: SQLite + Litestream when LITESTREAM_S3_BUCKET is set, else app.handler.

Litestream lives here (infra), not in syncbot/. Existing MySQL/TiDB skips restore
and still relies on the post-deploy {"action":"migrate"} invoke.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

_logger = logging.getLogger(__name__)

_DB_PATH = Path("/tmp/syncbot.db")
_LITESTREAM_BIN = os.environ.get("LITESTREAM_BIN", "/var/task/litestream")
_LITESTREAM_CONFIG = os.environ.get("LITESTREAM_CONFIG", "/var/task/litestream.yml")

_lock = threading.Lock()
_sqlite_ready = False


def _bootstrap_sqlite() -> None:
    """Restore (once per execution environment), migrate, then replicate."""
    global _sqlite_ready
    with _lock:
        if _sqlite_ready:
            return
        if not _DB_PATH.exists():
            _logger.info("litestream: restoring from S3 (if replica exists)")
            subprocess.run(
                [
                    _LITESTREAM_BIN,
                    "restore",
                    "-if-replica-exists",
                    "-config",
                    _LITESTREAM_CONFIG,
                    str(_DB_PATH),
                ],
                check=True,
            )
        from db import initialize_database

        initialize_database()
        _logger.info("litestream: starting replicate")
        subprocess.Popen(
            [_LITESTREAM_BIN, "replicate", "-config", _LITESTREAM_CONFIG],
            start_new_session=True,
        )
        _sqlite_ready = True


def handler(event, context):
    if os.environ.get("LITESTREAM_S3_BUCKET"):
        _bootstrap_sqlite()
    import app

    return app.handler(event, context)
