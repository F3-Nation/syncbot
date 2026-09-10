"""Store the full federation endpoint in federated_workspaces.webhook_url.

Older rows stored only the peer's public origin, and the outbound client
appended the hardcoded ``/api/federation`` mount path. The client now appends
only resource subpaths (``/message``, ``/pair``) to whatever the connection
code advertised, so already-paired rows are upgraded here to the full endpoint.
Every pre-1.5.1 deployment served federation at ``/api/federation``; that is the
one-time assumption applied to legacy rows, not an ongoing code assumption.

Revision ID: 012_federation_webhook_endpoint
Revises: 011_user_mapping_map_method
Create Date: 2026-09-03
"""

from collections.abc import Sequence
from urllib.parse import urlparse, urlunparse

import sqlalchemy as sa
from alembic import op

revision: str = "012_federation_webhook_endpoint"
down_revision: str | None = "011_user_mapping_map_method"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "federated_workspaces"
_COLUMN = "webhook_url"
_MOUNT_PATH = "/api/federation"


def _has_mount_path(url: str) -> bool:
    """Return True when *url* already carries a non-empty path component."""
    try:
        path = urlparse(url).path
    except ValueError:
        return True
    return path not in ("", "/")


def _add_mount_path(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=_MOUNT_PATH))


def _strip_mount_path(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path.rstrip("/") != _MOUNT_PATH:
        return url
    return urlunparse(parsed._replace(path=""))


def _rewrite(transform, predicate) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        return
    table = sa.table(_TABLE, sa.column("id", sa.Integer), sa.column(_COLUMN, sa.String))
    rows = bind.execute(sa.select(table.c.id, table.c.webhook_url)).fetchall()
    for row_id, url in rows:
        if not url or not predicate(url):
            continue
        bind.execute(sa.update(table).where(table.c.id == row_id).values(webhook_url=transform(url)))


def upgrade() -> None:
    _rewrite(_add_mount_path, lambda url: not _has_mount_path(url))


def downgrade() -> None:
    _rewrite(_strip_mount_path, _has_mount_path)
