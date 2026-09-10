"""SQLAlchemy ORM models for the SyncBot database.

Tables:

* **workspaces** — One row per Slack workspace that has installed SyncBot.
* **workspace_groups** — Named groups of workspaces that can sync channels.
* **workspace_group_members** — Membership records linking workspaces to groups.
* **syncs** — Named sync groups (e.g. "East Coast AOs").
* **sync_channels** — Links a Slack channel to a sync group via its workspace.
  Supports soft deletes via ``deleted_at``.
* **post_meta** — Maps each synced message to its channel-specific
  timestamp so edits, deletes, and thread replies can be propagated.
* **user_directory** — Cached copy of each workspace's user profiles,
  used for cross-workspace name-based mapping.
* **user_mappings** — Cross-workspace user map results (including
  confirmed maps, name-based maps, manual admin maps, and
  explicit "no map" records to avoid redundant lookups).
* **processed_events** — Slack Events API ``event_id`` claims (at-least-once
  dedup). Ephemeral; not included in full-instance backup.
* **user_action_echoes** — Remembered user-token Slack writes so inbound echo
  events can be skipped. Ephemeral; not included in full-instance backup.
"""

from typing import Any

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.types import DECIMAL

BaseClass = declarative_base()


class GetDBClass:
    """Mixin providing helper accessors for ORM model classes."""

    _column_keys: frozenset[str] | None = None

    @classmethod
    def _get_column_keys(cls) -> frozenset[str]:
        if cls._column_keys is None:
            cls._column_keys = frozenset(c.key for c in cls.__table__.columns)
        return cls._column_keys

    def get_id(self) -> Any:
        return self.id

    def get(self, attr: str) -> Any:
        if attr in self._get_column_keys():
            return getattr(self, attr)
        return None

    def to_json(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self._get_column_keys()}

    def __repr__(self) -> str:
        return str(self.to_json())


class Workspace(BaseClass, GetDBClass):
    __tablename__ = "workspaces"
    id = Column(Integer, primary_key=True)
    team_id = Column(String(100), unique=True)
    workspace_name = Column(String(100))
    bot_token = Column(Text)
    deleted_at = Column(DateTime, nullable=True, default=None)

    def get_id():
        """Slack ``team_id``, not the integer primary key."""
        return Workspace.team_id


class WorkspaceGroup(BaseClass, GetDBClass):
    """A named group of workspaces that can sync channels together."""

    __tablename__ = "workspace_groups"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    invite_code = Column(String(20), unique=True, nullable=False)
    status = Column(String(20), nullable=False, default="active")
    created_at = Column(DateTime, nullable=False)

    def get_id():
        return WorkspaceGroup.id


class WorkspaceGroupMember(BaseClass, GetDBClass):
    """Membership record linking a workspace (or federated workspace) to a group.

    ``role`` is one of ``owner`` or ``member``. ``admin`` is reserved in the
    vocabulary but is deliberately never written, because no permission attaches
    to it yet and an unused value invites people to set it and expect behavior.

    Only ``owner`` is load-bearing: owners may promote another workspace, may
    leave only while another active owner remains, and may disband a group they
    solely own and solely publish into. ``member`` is otherwise descriptive — it
    grants no restrictions beyond the owner-gated actions above, and all
    per-user authorization still runs through ``helpers.is_workspace_manager``.
    """

    __tablename__ = "workspace_group_members"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("workspace_groups.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    federated_workspace_id = Column(Integer, ForeignKey("federated_workspaces.id"), nullable=True)
    status = Column(String(20), nullable=False, default="active")
    role = Column(String(20), nullable=False, default="member")
    joined_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True, default=None)
    dm_messages = Column(Text, nullable=True)
    invited_by_slack_user_id = Column(String(32), nullable=True)
    invited_by_workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)

    group = relationship("WorkspaceGroup", backref="members")
    workspace = relationship(
        "Workspace",
        backref="group_memberships",
        foreign_keys=[workspace_id],
    )

    def get_id():
        return WorkspaceGroupMember.id


class Sync(BaseClass, GetDBClass):
    __tablename__ = "syncs"
    id = Column(Integer, primary_key=True)
    title = Column(String(100))
    description = Column(String(100))
    group_id = Column(Integer, ForeignKey("workspace_groups.id"), nullable=True)
    sync_mode = Column(String(20), nullable=False, default="group")
    target_workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    publisher_workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)

    def get_id():
        return Sync.id


class SyncChannel(BaseClass, GetDBClass):
    __tablename__ = "sync_channels"
    id = Column(Integer, primary_key=True)
    sync_id = Column(Integer, ForeignKey("syncs.id"))
    workspace_id = Column(Integer, ForeignKey("workspaces.id"))
    workspace = relationship("Workspace", backref="sync_channels")
    channel_id = Column(String(100))
    status = Column(String(20), nullable=False, default="active")
    reaction_direction = Column(String(32), nullable=False, default="both")
    reaction_style = Column(String(32), nullable=True)
    publishes = Column(Boolean, nullable=False, default=True)
    subscribes = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False)
    deleted_at = Column(DateTime, nullable=True, default=None)

    def get_id():
        """Integer primary key (a Slack channel may appear on several syncs)."""
        return SyncChannel.id


class PostMeta(BaseClass, GetDBClass):
    __tablename__ = "post_meta"
    id = Column(Integer, primary_key=True)
    post_id = Column(String(100))
    sync_channel_id = Column(Integer, ForeignKey("sync_channels.id"))
    ts = Column(DECIMAL(16, 6))
    kind = Column(String(32), nullable=False, default="message")
    parent_post_id = Column(String(100), nullable=True)
    reaction = Column(String(100), nullable=True)
    source_user_id = Column(String(100), nullable=True)
    source_workspace_id = Column(Integer, nullable=True)
    posted_as_user_id = Column(String(100), nullable=True)

    def get_id():
        """Slack ``post_id``, not the integer primary key."""
        return PostMeta.post_id


class UserDirectory(BaseClass, GetDBClass):
    """Cached user profile from a Slack workspace, used for name mapping."""

    __tablename__ = "user_directory"
    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"))
    slack_user_id = Column(String(100), nullable=False)
    email = Column(String(320), nullable=True)
    real_name = Column(String(200), nullable=True)
    display_name = Column(String(200), nullable=True)
    normalized_name = Column(String(200), nullable=True)
    updated_at = Column(DateTime, nullable=False)
    deleted_at = Column(DateTime, nullable=True, default=None)

    def get_id():
        return UserDirectory.id


class UserMapping(BaseClass, GetDBClass):
    """Cross-workspace user map result (or explicit no-map stub)."""

    __tablename__ = "user_mappings"
    id = Column(Integer, primary_key=True)
    source_workspace_id = Column(Integer, ForeignKey("workspaces.id"))
    source_user_id = Column(String(100), nullable=False)
    target_workspace_id = Column(Integer, ForeignKey("workspaces.id"))
    target_user_id = Column(String(100), nullable=True)
    map_method = Column(String(20), nullable=False, default="none")
    source_display_name = Column(String(200), nullable=True)
    mapped_at = Column(DateTime, nullable=False)
    group_id = Column(Integer, ForeignKey("workspace_groups.id"), nullable=True)

    def get_id():
        return UserMapping.id


class InstanceKey(BaseClass, GetDBClass):
    """This instance's Ed25519 keypair, auto-generated on first boot.

    The private key is stored Fernet-encrypted using DATA_ENCRYPTION_KEY.
    The public key is shared with federated workspaces during connection setup.
    ``instance_id`` is the SHA-256 hex fingerprint of ``public_key`` (raw
    32-byte Ed25519 key, 64 characters). Leftover ``SYNCBOT_INSTANCE_ID`` is
    ignored.
    """

    __tablename__ = "instance_keys"
    id = Column(Integer, primary_key=True)
    public_key = Column(Text, nullable=False)
    private_key_encrypted = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
    instance_id = Column(String(64), nullable=True)

    def get_id():
        return InstanceKey.id


class FederatedWorkspace(BaseClass, GetDBClass):
    """A remote SyncBot instance that this instance can communicate with.

    Each federated workspace has a unique ``instance_id`` (public-key
    fingerprint, or a legacy UUID from an older peer), a ``webhook_url``
    (the peer's full federation endpoint, e.g. ``https://host/api/federation``;
    resource subpaths like ``/message`` are appended when pushing), and a
    ``public_key`` (Ed25519 PEM) used to verify inbound request signatures.
    ``primary_team_id`` and ``primary_workspace_name`` are optional and set
    when the connection is from a workspace that migrated to the remote instance.
    """

    __tablename__ = "federated_workspaces"
    id = Column(Integer, primary_key=True)
    instance_id = Column(String(64), unique=True, nullable=False)
    webhook_url = Column(String(500), nullable=False)
    public_key = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="active")
    name = Column(String(200), nullable=True)
    primary_team_id = Column(String(100), nullable=True)
    primary_workspace_name = Column(String(100), nullable=True)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=True)

    def get_id():
        return FederatedWorkspace.id


class WorkspaceSetting(BaseClass, GetDBClass):
    """Per-workspace policy edited through the Settings modal.

    Key/value storage scoped to one installed workspace. Values are strings;
    typed accessors in ``helpers.workspace_settings`` parse them. Keys include
    ``allow_private_channels`` and ``extra_manager_user_ids`` (JSON list of ``U…``
    IDs).
    """

    __tablename__ = "workspace_settings"

    workspace_id = Column(Integer, ForeignKey("workspaces.id"), primary_key=True)
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=True)

    def get_id():
        return (WorkspaceSetting.workspace_id, WorkspaceSetting.key)


class InstanceSetting(BaseClass, GetDBClass):
    """Operator-managed instance policy, edited through the Settings modal.

    Key/value on purpose, so adding a setting never needs a migration. Values
    are stored as strings; the typed accessors in ``helpers.settings`` parse
    them and apply the database-then-default precedence (leftover env vars for
    these keys are ignored and warned; see ``helpers.settings``).

    Only operational policy lives here. Secrets, connection details, and
    break-glass switches (``ENABLE_DB_RESET``) stay in environment variables.
    """

    __tablename__ = "instance_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=True)

    def get_id():
        return InstanceSetting.key


class ProcessedEvent(BaseClass, GetDBClass):
    """Dedup record for Slack Events API at-least-once delivery.

    Unique on ``(team_id, event_id)`` from the Slack envelope (not ``event.ts``).
    """

    __tablename__ = "processed_events"
    __table_args__ = (UniqueConstraint("team_id", "event_id", name="uq_processed_events_team_event"),)

    id = Column(Integer, primary_key=True)
    team_id = Column(String(100), nullable=False)
    event_id = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    created_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    def get_id():
        return ProcessedEvent.id


class UserActionEcho(BaseClass, GetDBClass):
    """Remembered user-token side effect so the matching inbound event is skipped."""

    __tablename__ = "user_action_echoes"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "user_id",
            "kind",
            "fingerprint",
            name="uq_user_action_echoes_team_user_kind_fp",
        ),
    )

    id = Column(Integer, primary_key=True)
    team_id = Column(String(100), nullable=False)
    user_id = Column(String(100), nullable=False)
    kind = Column(String(64), nullable=False)
    fingerprint = Column(String(256), nullable=False)
    created_at = Column(DateTime, nullable=False)

    def get_id():
        return UserActionEcho.id
