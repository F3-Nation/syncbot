"""SQLAlchemy ORM models for the SyncBot database.

Tables:

* **instances** — This SyncBot install (self keypair) and trusted peers.
* **workspaces** — One row per Slack team: live on this instance or a stub for a peer.
* **workspace_groups** — Named groups of workspaces that can sync channels.
* **workspace_group_members** — Membership records linking workspaces to groups.
* **syncs** — Named sync groups (e.g. "Regional Sync").
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
* **federation_file_parts** — In-flight federation file mailbox (~5 min TTL).
  Payload is Fernet-encrypted when ``DATA_ENCRYPTION_KEY`` is set. Omitted
  from backup and migration export.
"""

from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import MEDIUMBLOB
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


class Instance(BaseClass, GetDBClass):
    """This SyncBot install or a trusted peer.

    PK ``instance_id`` is the SHA-256 hex fingerprint of the Ed25519 public key
    (64 characters). The self row has ``private_key_encrypted`` and a null
    ``webhook_url``. Peer rows have a webhook and no private key.
    """

    __tablename__ = "instances"
    instance_id = Column(String(64), primary_key=True)
    public_key = Column(Text, nullable=False)
    private_key_encrypted = Column(Text, nullable=True)
    webhook_url = Column(String(500), nullable=True)
    status = Column(String(20), nullable=False, default="active")
    trust_status = Column(String(20), nullable=False, default="trusted", server_default="trusted")
    name = Column(String(200), nullable=True)
    primary_team_id = Column(String(32), nullable=True)
    primary_workspace_name = Column(String(100), nullable=True)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=True)

    def get_id():
        """Fingerprint PK, not an integer."""
        return Instance.instance_id


class Workspace(BaseClass, GetDBClass):
    """A Slack workspace on this instance (live install) or a stub for a peer team.

    Live iff ``instance_id`` is this install's fingerprint and ``deleted_at`` is
    null. Stub iff ``instance_id`` is a peer fingerprint and ``deleted_at`` is
    null. Never both a live install and a stub for the same ``team_id``.
    Bot tokens live in Bolt ``slack_bots`` (see ``helpers.workspace.get_bot_token``).
    """

    __tablename__ = "workspaces"
    __table_args__ = (Index("ix_workspaces_instance_id", "instance_id"),)
    id = Column(Integer, primary_key=True)
    team_id = Column(String(32), unique=True)
    workspace_name = Column(String(100))
    instance_id = Column(String(64), ForeignKey("instances.instance_id"), nullable=False)
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
    uid = Column(String(36), unique=True, nullable=False)

    def get_id():
        return WorkspaceGroup.id


class WorkspaceGroupMember(BaseClass, GetDBClass):
    """Membership record linking a workspace to a group.

    ``role`` is one of ``owner`` or ``member``. ``admin`` is reserved in the
    vocabulary but is deliberately never written, because no permission attaches
    to it yet and an unused value invites people to set it and expect behavior.

    Only ``owner`` is load-bearing: owners may promote another workspace, may
    leave only while another active owner remains, and may disband a group they
    solely own and solely publish into. ``member`` is otherwise descriptive — it
    grants no restrictions beyond the owner-gated actions above, and all
    per-user authorization still runs through ``helpers.is_workspace_manager``.

    Federated peers appear as stub ``Workspace`` rows (``instance_id`` points at
    a peer), not as a separate member FK.
    """

    __tablename__ = "workspace_group_members"
    __table_args__ = (
        Index("ix_workspace_group_members_group_id", "group_id"),
        Index("ix_workspace_group_members_workspace_id", "workspace_id"),
    )
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("workspace_groups.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
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
    __table_args__ = (Index("ix_syncs_group_id", "group_id"),)
    id = Column(Integer, primary_key=True)
    title = Column(String(100))
    description = Column(String(100))
    group_id = Column(Integer, ForeignKey("workspace_groups.id"), nullable=True)
    sync_mode = Column(String(20), nullable=False, default="group")
    uid = Column(String(36), unique=True, nullable=False)

    def get_id():
        return Sync.id


class SyncChannel(BaseClass, GetDBClass):
    __tablename__ = "sync_channels"
    __table_args__ = (
        Index("ix_sync_channels_channel_id", "channel_id"),
        Index("ix_sync_channels_sync_id", "sync_id"),
        Index("ix_sync_channels_workspace_id", "workspace_id"),
    )
    id = Column(Integer, primary_key=True)
    sync_id = Column(Integer, ForeignKey("syncs.id"))
    workspace_id = Column(Integer, ForeignKey("workspaces.id"))
    workspace = relationship("Workspace", backref="sync_channels")
    channel_id = Column(String(100))
    channel_name = Column(String(100), nullable=True)
    status = Column(String(20), nullable=False, default="active")
    reaction_style = Column(String(32), nullable=True)
    publishes = Column(Boolean, nullable=False, default=True)
    subscribes = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False)
    deleted_at = Column(DateTime, nullable=True, default=None)

    def get_id():
        """Integer primary key (a Slack channel may appear on several syncs)."""
        return SyncChannel.id


class PostMeta(BaseClass, GetDBClass):
    """Origin or copy of a synced message. Persist ``ts`` with ``post_meta_ts``."""

    __tablename__ = "post_meta"
    __table_args__ = (
        Index("ix_post_meta_post_channel", "post_id", "sync_channel_id"),
        Index("ix_post_meta_channel_ts", "sync_channel_id", "ts"),
        Index("ix_post_meta_ts", "ts"),
        Index("ix_post_meta_notice_lookup", "parent_post_id", "reaction", "source_user_id"),
    )
    id = Column(Integer, primary_key=True)
    post_id = Column(String(100))
    sync_channel_id = Column(Integer, ForeignKey("sync_channels.id"))
    ts = Column(DECIMAL(16, 6))  # post_meta_ts(); never float
    kind = Column(String(32), nullable=False, default="message")
    parent_post_id = Column(String(100), nullable=True)
    reaction = Column(String(100), nullable=True)
    source_user_id = Column(String(100), nullable=True)
    source_workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    posted_as_user_id = Column(String(100), nullable=True)

    def get_id():
        """Slack ``post_id``, not the integer primary key."""
        return PostMeta.post_id


class UserDirectory(BaseClass, GetDBClass):
    """Cached user profile from a Slack workspace, used for name mapping."""

    __tablename__ = "user_directory"
    __table_args__ = (UniqueConstraint("workspace_id", "slack_user_id", name="uq_user_directory_workspace_slack_user"),)
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
    __table_args__ = (
        Index(
            "ix_user_mappings_source_target",
            "source_workspace_id",
            "source_user_id",
            "target_workspace_id",
        ),
    )
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


class FederationPairingCode(BaseClass, GetDBClass):
    """Pairing-only FED- code. Does not create a WorkspaceGroup.

    ``subject_team_id`` null = operator External Connections code. Set = workspace-
    scoped migration code (allowlist that Slack team). Both appear as waiting
    External Connections until consumed or expired.
    """

    __tablename__ = "federation_pairing_codes"
    id = Column(Integer, primary_key=True)
    code = Column(String(20), unique=True, nullable=False)
    created_at = Column(DateTime, nullable=False)
    subject_team_id = Column(String(32), nullable=True)
    label = Column(String(200), nullable=True)
    allowed_workspace_ids = Column(Text, nullable=True)

    def get_id():
        return FederationPairingCode.id


class FederationPairingRequest(BaseClass, GetDBClass):
    """Leaving-workspace admin asked primary admins for a pairing code.

    At most one ``pending`` row per ``subject_team_id`` (enforced in handlers).
    """

    __tablename__ = "federation_pairing_requests"

    id = Column(Integer, primary_key=True)
    subject_team_id = Column(String(32), nullable=False)
    requested_by_user_id = Column(String(100), nullable=False)
    requested_at = Column(DateTime, nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    resolved_by_user_id = Column(String(100), nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    pairing_code_id = Column(
        Integer,
        ForeignKey("federation_pairing_codes.id", ondelete="SET NULL"),
        nullable=True,
    )

    def get_id():
        return FederationPairingRequest.id


class FederationPendingStub(BaseClass, GetDBClass):
    """Team is still live here; convert to stub after uninstall when peer is trusted."""

    __tablename__ = "federation_pending_stubs"
    __table_args__ = (UniqueConstraint("workspace_id", "instance_id", name="uq_federation_pending_stubs_ws_peer"),)

    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    instance_id = Column(String(64), ForeignKey("instances.instance_id"), nullable=False)

    def get_id():
        return FederationPendingStub.id


class FederationWorkspaceAllowlist(BaseClass, GetDBClass):
    """Local workspaces allowed on one External Connection.

    For **new** remote workspaces joining after trust. Heal of an existing member
    ignores the allowlist.
    """

    __tablename__ = "federation_workspace_allowlist"
    __table_args__ = (
        UniqueConstraint(
            "instance_id",
            "workspace_id",
            name="uq_federation_workspace_allowlist_peer_ws",
        ),
    )

    id = Column(Integer, primary_key=True)
    instance_id = Column(String(64), ForeignKey("instances.instance_id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)

    def get_id():
        return FederationWorkspaceAllowlist.id


class FederationFilePart(BaseClass, GetDBClass):
    """In-flight federation file mailbox. Ephemeral; not in backup or migration export.

    ``payload`` is Fernet ciphertext when ``DATA_ENCRYPTION_KEY`` is set.
    ``part_total`` is the sender's part count (used by ``offer_have``).
    """

    __tablename__ = "federation_file_parts"
    __table_args__ = (UniqueConstraint("sha256", "part_index", name="uq_federation_file_parts_sha_idx"),)

    id = Column(Integer, primary_key=True)
    sha256 = Column(String(64), nullable=False)
    part_index = Column(Integer, nullable=False)
    part_total = Column(Integer, nullable=True)
    payload = Column(LargeBinary().with_variant(MEDIUMBLOB, "mysql"), nullable=False)
    created_at = Column(DateTime, nullable=False)

    def get_id():
        return FederationFilePart.id


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
    team_id = Column(String(32), nullable=False)
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
    team_id = Column(String(32), nullable=False)
    user_id = Column(String(100), nullable=False)
    kind = Column(String(64), nullable=False)
    fingerprint = Column(String(256), nullable=False)
    created_at = Column(DateTime, nullable=False)

    def get_id():
        return UserActionEcho.id
