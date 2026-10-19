"""Workspace record management and name resolution."""

import os
from datetime import UTC, datetime

from slack_sdk import WebClient

import constants
from db import DbManager, schemas
from helpers._cache import _cache_get, _cache_set
from helpers.core import safe_get
from helpers.encryption import decrypt_bot_token
from logger import log_debug, log_error, log_info, log_warning


def get_bot_token(workspace: schemas.Workspace | None) -> str | None:
    """Return the bot token for a live workspace on this instance, or None.

    Live vs stub is ``instance_id``, not token presence. Looks up Bolt
    ``slack_bots`` first, then local ``SLACK_BOT_TOKEN`` when OAuth is off.
    """
    if workspace is None or getattr(workspace, "deleted_at", None) is not None:
        return None
    try:
        from federation.core import get_instance_id

        if getattr(workspace, "instance_id", None) != get_instance_id():
            return None
    except Exception:
        return None

    team_id = getattr(workspace, "team_id", None)
    client_id = os.environ.get(constants.SLACK_CLIENT_ID, "").strip()
    if team_id and client_id:
        try:
            from db import get_engine
            from helpers.encryption_installation_store import EncryptedSQLAlchemyInstallationStore

            store = EncryptedSQLAlchemyInstallationStore(client_id=client_id, engine=get_engine())
            bot = store.find_bot(enterprise_id=None, team_id=team_id)
            token = getattr(bot, "bot_token", None) if bot else None
            if token:
                return token
        except Exception:
            log_debug("get_bot_token", reason="installation store lookup failed", exc_info=True)

    if constants.LOCAL_DEVELOPMENT and constants.HAS_REAL_BOT_TOKEN:
        env_token = os.environ.get(constants.SLACK_BOT_TOKEN, "").strip()
        if env_token:
            return env_token

    raw = getattr(workspace, "bot_token", None)
    if raw:
        try:
            return decrypt_bot_token(raw)
        except Exception:
            return None
    miss_key = f"bot_token_missing:{team_id}"
    if team_id and not _cache_get(miss_key):
        _cache_set(miss_key, True)
        log_debug(
            "bot_token_missing",
            team_id=team_id,
            workspace_id=getattr(workspace, "id", None),
        )
    return None


def invalidate_fed_ws_for_sync_cache() -> None:
    """Drop leftover ``fed_ws_for_sync:`` cache keys after pair, unpair, or restore.

    Fan-out no longer uses ``get_federated_workspace_for_sync``; routing goes
    through stub ``Workspace`` rows (``helpers.workspace_kind``). This remains a
    prefix delete for safety on warm containers that still hold old keys.
    """
    from helpers._cache import _cache_delete_prefix

    _cache_delete_prefix("fed_ws_for_sync:")


def replace_federation_allowlist(instance_id: str, workspace_ids: list[int]) -> None:
    """Replace the local workspaces allowed for *instance_id*."""
    instance_id = (instance_id or "").strip()
    if not instance_id:
        return
    ids = sorted({int(wid) for wid in workspace_ids if wid})
    DbManager.delete_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == instance_id],
    )
    for workspace_id in ids:
        DbManager.create_record(
            schemas.FederationWorkspaceAllowlist(
                instance_id=instance_id,
                workspace_id=workspace_id,
            )
        )


def get_allowed_local_workspace_ids(instance_id: str) -> set[int]:
    """Local workspace IDs allowed on *instance_id* (allowlist, else shared groups)."""
    from helpers.workspace_kind import is_local_workspace

    instance_id = (instance_id or "").strip()
    if not instance_id:
        return set()
    allowlist = DbManager.find_records(
        schemas.FederationWorkspaceAllowlist,
        [schemas.FederationWorkspaceAllowlist.instance_id == instance_id],
    )
    if allowlist:
        return {row.workspace_id for row in allowlist if row.workspace_id}

    stubs = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.instance_id == instance_id,
            schemas.Workspace.deleted_at.is_(None),
        ],
    )
    stub_ids = {ws.id for ws in stubs}
    if not stub_ids:
        return set()
    stub_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id.in_(list(stub_ids)),
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    group_ids = {m.group_id for m in stub_members}
    ws_ids: set[int] = set()
    for group_id in group_ids:
        group_members = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.group_id == group_id,
                schemas.WorkspaceGroupMember.workspace_id.isnot(None),
                schemas.WorkspaceGroupMember.status == "active",
                schemas.WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )
        for member in group_members:
            ws = get_workspace_by_id(member.workspace_id)
            if ws and is_local_workspace(ws):
                ws_ids.add(member.workspace_id)
    return ws_ids


# Routing uses stub workspaces (``Workspace.instance_id``) via
# ``helpers.workspace_kind.peer_for_workspace`` — there is no
# ``get_federated_workspace_for_sync`` anymore.


def get_workspace_record(team_id: str, body: dict, context: dict, client: WebClient) -> schemas.Workspace | None:
    """Fetch or create the Workspace record for a Slack workspace."""
    from federation.core import get_instance_id
    from helpers.settings import team_id_is_blocked

    workspace_record: schemas.Workspace | None = DbManager.get_record(schemas.Workspace, id=team_id)
    if team_id_is_blocked(team_id):
        log_info("workspace_blocked", team_id=team_id)
        if workspace_record and workspace_record.deleted_at is None and not _is_stub_workspace(workspace_record):
            slack_apps_uninstall(get_bot_token(workspace_record))
            uninstall_workspace(team_id)
            return DbManager.get_record(schemas.Workspace, id=team_id)
        return workspace_record

    team_domain = safe_get(body, "team", "domain")
    announce_live = False

    if not workspace_record:
        try:
            team_info = client.team_info()
            ws_name = team_info["team"]["name"]
        except Exception as exc:
            log_debug("get_workspace", error=str(exc))
            ws_name = team_domain
        workspace_record: schemas.Workspace = DbManager.create_record(
            schemas.Workspace(
                team_id=team_id,
                workspace_name=ws_name,
                instance_id=get_instance_id(),
            )
        )
        announce_live = True
    elif workspace_record.deleted_at is not None or _is_stub_workspace(workspace_record):
        workspace_record = _restore_workspace(workspace_record, context, client)
        announce_live = True
    else:
        _maybe_refresh_bot_token(workspace_record, context)
        _maybe_refresh_workspace_name(workspace_record, client)

    if announce_live:
        _push_live_team_to_trusted_peers(team_id)

    return workspace_record


def _push_live_team_to_trusted_peers(team_id: str) -> None:
    """Best-effort push of the current allowlist to trusted peers."""
    from federation.core import push_allowed_workspaces

    peers = DbManager.find_records(
        schemas.Instance,
        [
            schemas.Instance.private_key_encrypted.is_(None),
            schemas.Instance.status == "active",
            schemas.Instance.trust_status == "trusted",
        ],
    )
    for peer in peers:
        try:
            result = push_allowed_workspaces(peer)
            ok = bool(result and result.get("ok"))
            emit = log_warning if not ok else log_debug
            emit(
                "push_teams",
                team_id=team_id,
                peer_instance_id=peer.instance_id,
                ok=ok,
                heal=result.get("results") if isinstance(result, dict) else None,
            )
        except Exception:
            log_error(
                "push_teams",
                team_id=team_id,
                peer_instance_id=peer.instance_id,
                ok=False,
            )
            log_error(
                "workspace_team_announcement_failed", team_id=team_id, peer_instance_id=peer.instance_id, exc_info=True
            )


def _maybe_refresh_bot_token(workspace_record: schemas.Workspace, context: dict) -> None:
    """Update Bolt ``slack_bots`` when the request context has a newer bot token."""
    new_token = safe_get(context, "bot_token")
    if not new_token or not workspace_record.team_id:
        return

    stored = get_bot_token(workspace_record)
    if stored == new_token:
        return

    client_id = os.environ.get(constants.SLACK_CLIENT_ID, "").strip()
    if not client_id:
        return
    try:
        from datetime import UTC, datetime

        from slack_sdk.oauth.installation_store import Bot

        from db import get_engine
        from helpers.encryption_installation_store import EncryptedSQLAlchemyInstallationStore

        store = EncryptedSQLAlchemyInstallationStore(client_id=client_id, engine=get_engine())
        existing = store.find_bot(enterprise_id=None, team_id=workspace_record.team_id)
        installed_at = getattr(existing, "installed_at", None) if existing else None
        if installed_at is None:
            installed_at = datetime.now(UTC)
        bot = Bot(
            app_id=getattr(existing, "app_id", None) if existing else None,
            enterprise_id=getattr(existing, "enterprise_id", None) if existing else None,
            team_id=workspace_record.team_id,
            bot_token=new_token,
            bot_id=getattr(existing, "bot_id", None) if existing else None,
            bot_user_id=getattr(existing, "bot_user_id", None) if existing else None,
            bot_scopes=getattr(existing, "bot_scopes", None) if existing else None,
            bot_refresh_token=getattr(existing, "bot_refresh_token", None) if existing else None,
            bot_token_expires_at=getattr(existing, "bot_token_expires_at", None) if existing else None,
            is_enterprise_install=getattr(existing, "is_enterprise_install", False) if existing else False,
            installed_at=installed_at,
        )
        store.save_bot(bot)
        log_info("bot_token_refreshed", workspace_id=workspace_record.id, team_id=workspace_record.team_id)
    except Exception:
        log_debug("bot_token_refresh_skipped", exc_info=True)


def _maybe_refresh_workspace_name(workspace_record: schemas.Workspace, client: WebClient) -> None:
    """Refresh the stored workspace name from the Slack API (at most once per day)."""
    cache_key = f"ws_name_refresh:{workspace_record.id}"
    if _cache_get(cache_key):
        return

    _cache_set(cache_key, True, ttl=86400)

    try:
        team_info = client.team_info()
        current_name = team_info["team"]["name"]
    except Exception as exc:
        log_debug("maybe_refresh_workspace_name_team_info_call_failed", error=str(exc))
        return

    if current_name and current_name != workspace_record.workspace_name:
        DbManager.update_records(
            schemas.Workspace,
            [schemas.Workspace.id == workspace_record.id],
            {schemas.Workspace.workspace_name: current_name},
        )
        workspace_record.workspace_name = current_name
        log_info("workspace_name_refreshed", workspace_id=workspace_record.id, new_name=current_name)


def _is_stub_workspace(workspace_record: schemas.Workspace) -> bool:
    from helpers.workspace_kind import is_stub_workspace

    return is_stub_workspace(workspace_record)


def soft_delete_workspace(workspace_record: schemas.Workspace) -> list:
    """Pause a workspace like uninstall: keep PostMeta until retention purge.

    Does not change ``instance_id`` (live vs stub). No-op if already deleted.
    """
    if workspace_record is None or getattr(workspace_record, "id", None) is None:
        return []
    if getattr(workspace_record, "deleted_at", None) is not None:
        return []

    now = datetime.now(UTC)
    DbManager.update_records(
        schemas.Workspace,
        [schemas.Workspace.id == workspace_record.id],
        {schemas.Workspace.deleted_at: now},
    )

    active_memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id == workspace_record.id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    for membership in active_memberships:
        DbManager.update_records(
            schemas.WorkspaceGroupMember,
            [schemas.WorkspaceGroupMember.id == membership.id],
            {schemas.WorkspaceGroupMember.deleted_at: now},
        )

    my_channels = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.workspace_id == workspace_record.id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    for sync_channel in my_channels:
        DbManager.update_records(
            schemas.SyncChannel,
            [schemas.SyncChannel.id == sync_channel.id],
            {schemas.SyncChannel.deleted_at: now, schemas.SyncChannel.status: "paused"},
        )

    from helpers.sync_participation import invalidate_sync_fanout_for_syncs

    invalidate_sync_fanout_for_syncs(c.sync_id for c in my_channels)
    log_info(
        "workspace_soft_deleted",
        workspace_id=workspace_record.id,
        team_id=getattr(workspace_record, "team_id", None),
        memberships_paused=len(active_memberships),
        channels_paused=len(my_channels),
    )
    return my_channels


_PAUSE_RESTORE_PUBLIC = ":double_vertical_bar: This Sync is paused while SyncBot restores membership."
_PAUSE_RESTORE_PRIVATE = (
    ":double_vertical_bar: This Sync is paused. This Channel is private, so Authorize SyncBot "
    "and Resume Sync to add it."
)
_RESUME_RESTORE = ":arrow_forward: This Sync has been resumed."
_JOIN_RESTORE = ":arrows_counterclockwise: SyncBot joined this Channel to restore the Sync."


def _set_restored_channel_status(sync_channel: schemas.SyncChannel, status: str, workspace: schemas.Workspace) -> None:
    """Write SyncChannel status, drop fan-out cache, and replicate when possible."""
    DbManager.update_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.id == sync_channel.id],
        {schemas.SyncChannel.status: status, schemas.SyncChannel.deleted_at: None},
    )
    sync_channel.status = status
    sync_channel.deleted_at = None
    from helpers.sync_participation import invalidate_sync_fanout_for_syncs

    invalidate_sync_fanout_for_syncs([sync_channel.sync_id])
    try:
        from federation.replicate import replicate_sync_channel_upsert

        sync = DbManager.get_record(schemas.Sync, id=sync_channel.sync_id)
        group = DbManager.get_record(schemas.WorkspaceGroup, id=sync.group_id) if sync and sync.group_id else None
        if sync and group:
            replicate_sync_channel_upsert(sync, group, sync_channel, workspace)
    except Exception:
        log_warning(
            "heal_sync_channels_replicate_failed",
            channel_id=sync_channel.channel_id,
            workspace_id=workspace.id,
        )


def notify_sibling_sync_channels(workspace: schemas.Workspace, sync_id: int, message: str) -> None:
    """Post *message* on other Workspaces' Channels in this Sync that still have a bot token."""
    from helpers.notifications import notify_synced_channels

    siblings = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.sync_id == sync_id,
            schemas.SyncChannel.workspace_id != workspace.id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    posted: set[tuple[int, str]] = set()
    for sibling in siblings:
        member_ws = get_workspace_by_id(sibling.workspace_id)
        token = get_bot_token(member_ws) if member_ws else None
        if not token or not sibling.channel_id:
            continue
        key = (sibling.workspace_id, sibling.channel_id)
        if key in posted:
            continue
        posted.add(key)
        try:
            notify_synced_channels(WebClient(token=token), [sibling.channel_id], message)
        except Exception as exc:
            log_warning(
                "heal_sync_channels_sibling_failed",
                channel_id=sibling.channel_id,
                workspace_id=sibling.workspace_id,
                error=str(exc),
            )


def heal_restored_sync_channels(
    workspace: schemas.Workspace,
    *,
    client: WebClient,
    acting_user_id: str | None = None,
    context: dict | None = None,
    source: str | None = None,
) -> dict[str, int]:
    """Rejoin public Sync Channels after reinstall or import; leave private paused.

    Public Channels are paused with a notice, the bot joins, then the Sync
    resumes with a notice (and a join notice when the bot was not already a
    member). Private Channels stay paused, with a pause notice in that Channel
    when the bot can post, and on the twin Channels otherwise.
    """
    from helpers.conversations import ConversationAccessError, ensure_bot_in_conversation, inspect_bot_channel_access
    from helpers.notifications import notify_admins_dm, notify_synced_channels
    from helpers.workspace_kind import is_local_workspace

    counts = {"resumed": 0, "joined": 0, "paused_private": 0, "skipped": 0}
    if workspace is None or not is_local_workspace(workspace) or not getattr(workspace, "id", None):
        return counts
    if client is None:
        log_debug("heal_sync_channels", reason="missing_client", workspace_id=workspace.id, source=source)
        return counts

    ws_name = resolve_workspace_name(workspace)
    channels = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.workspace_id == workspace.id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    private_need_admin = False
    changed = False
    for sync_channel in channels:
        channel_id = sync_channel.channel_id
        if not channel_id:
            continue
        is_private, is_member = inspect_bot_channel_access(client, channel_id)
        if not is_private and is_member:
            counts["skipped"] += 1
            continue

        if is_private:
            if sync_channel.status == "paused":
                counts["skipped"] += 1
                continue
            _set_restored_channel_status(sync_channel, "paused", workspace)
            posted = notify_synced_channels(client, [channel_id], _PAUSE_RESTORE_PRIVATE)
            notify_sibling_sync_channels(
                workspace,
                sync_channel.sync_id,
                f":double_vertical_bar: Syncing with `{ws_name}` is paused. That Channel is "
                "private, so someone there needs to Resume Sync after Authorize.",
            )
            if not posted:
                private_need_admin = True
            counts["paused_private"] += 1
            changed = True
            continue

        _set_restored_channel_status(sync_channel, "paused", workspace)
        joined = False
        if not is_member:
            try:
                ensure_bot_in_conversation(
                    client,
                    channel_id,
                    team_id=workspace.team_id,
                    acting_user_id=acting_user_id,
                    context=context,
                )
                joined = True
            except ConversationAccessError as exc:
                log_warning(
                    "heal_sync_channels_join_failed",
                    channel_id=channel_id,
                    workspace_id=workspace.id,
                    error=str(exc),
                )
                notify_synced_channels(
                    client,
                    [channel_id],
                    ":double_vertical_bar: This Sync is paused. SyncBot could not rejoin this Channel.",
                )
                notify_sibling_sync_channels(
                    workspace,
                    sync_channel.sync_id,
                    f":double_vertical_bar: Syncing with `{ws_name}` is paused. SyncBot could not rejoin that Channel.",
                )
                counts["paused_private"] += 1
                changed = True
                continue

        notify_synced_channels(client, [channel_id], _PAUSE_RESTORE_PUBLIC)
        _set_restored_channel_status(sync_channel, "active", workspace)
        notify_synced_channels(client, [channel_id], _RESUME_RESTORE)
        if joined:
            notify_synced_channels(client, [channel_id], _JOIN_RESTORE)
            counts["joined"] += 1
        notify_sibling_sync_channels(
            workspace,
            sync_channel.sync_id,
            f":arrow_forward: Syncing with `{ws_name}` has been resumed.",
        )
        counts["resumed"] += 1
        changed = True

    if private_need_admin:
        notify_admins_dm(
            client,
            ":double_vertical_bar: A private Channel Sync stayed paused. Authorize SyncBot, "
            "then Resume Sync to add it.",
            team_id=workspace.team_id,
        )
    if changed:
        notified_ws: set[int] = set()
        memberships = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.workspace_id == workspace.id,
                schemas.WorkspaceGroupMember.status == "active",
                schemas.WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )
        admin_msg = (
            f":double_vertical_bar: `{ws_name}` is back. Public Channel Syncs will resume. "
            "Private Channels stay paused until someone Resumes Sync."
            if counts["paused_private"]
            else f":arrow_forward: `{ws_name}` has been restored. Group syncing will resume."
        )
        for membership in memberships:
            peers = DbManager.find_records(
                schemas.WorkspaceGroupMember,
                [
                    schemas.WorkspaceGroupMember.group_id == membership.group_id,
                    schemas.WorkspaceGroupMember.workspace_id != workspace.id,
                    schemas.WorkspaceGroupMember.status == "active",
                    schemas.WorkspaceGroupMember.deleted_at.is_(None),
                ],
            )
            for peer in peers:
                if not peer.workspace_id or peer.workspace_id in notified_ws:
                    continue
                member_ws = get_workspace_by_id(peer.workspace_id)
                token = get_bot_token(member_ws) if member_ws else None
                if not token:
                    continue
                notified_ws.add(peer.workspace_id)
                try:
                    notify_admins_dm(WebClient(token=token), admin_msg, team_id=member_ws.team_id)
                except Exception as exc:
                    log_warning(
                        "heal_sync_channels_admin_dm_failed",
                        workspace_id=peer.workspace_id,
                        error=str(exc),
                    )

    log_info(
        "heal_sync_channels",
        source=source,
        workspace_id=workspace.id,
        team_id=workspace.team_id,
        resumed=counts["resumed"],
        joined=counts["joined"],
        paused_private=counts["paused_private"],
        skipped=counts["skipped"],
    )
    return counts


def _restore_workspace(
    workspace_record: schemas.Workspace,
    context: dict,
    client: WebClient,
) -> schemas.Workspace:
    """Reclaim a paused or stub workspace as a live install on this instance."""
    from helpers.settings import team_id_is_blocked
    from helpers.workspace_kind import heal_workspace_to_local

    if team_id_is_blocked(workspace_record.team_id):
        log_info("workspace_restore_blocked", team_id=workspace_record.team_id)
        return workspace_record

    new_token = safe_get(context, "bot_token")
    heal_workspace_to_local(workspace_record.team_id, source="reinstall")
    if new_token:
        _maybe_refresh_bot_token(workspace_record, context)

    workspace_record = DbManager.get_record(schemas.Workspace, id=workspace_record.team_id)
    heal_restored_sync_channels(
        workspace_record,
        client=client,
        context=context,
        source="reinstall",
    )
    log_info("workspace_restored", workspace_id=workspace_record.id)
    return workspace_record


def get_workspace_by_id(workspace_id: int, context: dict | None = None) -> schemas.Workspace | None:
    """Look up a workspace by its integer primary-key ``id`` column.

    If *context* is provided, uses request-scoped cache to avoid repeated DB
    lookups for the same workspace_id within one request.
    """
    if context is not None:
        cache = context.setdefault("_workspace_by_id", {})
        if workspace_id in cache:
            return cache[workspace_id]
    rows = DbManager.find_records(schemas.Workspace, [schemas.Workspace.id == workspace_id])
    result = rows[0] if rows else None
    if context is not None:
        context.setdefault("_workspace_by_id", {})[workspace_id] = result
    return result


def get_groups_for_workspace(workspace_id: int) -> list[schemas.WorkspaceGroup]:
    """Return all active groups the workspace belongs to."""
    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id == workspace_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    if not members:
        return []
    group_ids = [m.group_id for m in members]
    return DbManager.find_records(
        schemas.WorkspaceGroup,
        [schemas.WorkspaceGroup.id.in_(group_ids), schemas.WorkspaceGroup.status == "active"],
    )


def get_group_members(group_id: int) -> list[schemas.WorkspaceGroupMember]:
    """Return all active members of a group."""
    return DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )


def resolve_workspace_name(workspace: schemas.Workspace) -> str:
    """Return a human-readable name for a workspace."""
    if workspace.workspace_name:
        return workspace.workspace_name

    if get_bot_token(workspace):
        try:
            ws_client = WebClient(token=get_bot_token(workspace))
            team_info = ws_client.team_info()
            name = safe_get(team_info, "team", "name")
            if name:
                DbManager.update_records(
                    schemas.Workspace,
                    [schemas.Workspace.id == workspace.id],
                    {schemas.Workspace.workspace_name: name},
                )
                workspace.workspace_name = name
                return name
        except Exception as exc:
            # Name lookup is best-effort; falling back to team_id keeps UI usable
            # even when Slack API calls fail intermittently.
            log_debug(
                "resolve_workspace_name_failed", workspace_id=workspace.id, team_id=workspace.team_id, error=str(exc)
            )

    return workspace.team_id or f"Workspace {workspace.id}"


def resolve_channel_name(channel_id: str, workspace=None) -> str:
    """Resolve a channel ID to a human-readable name."""
    if not channel_id:
        return channel_id

    cache_key = f"chan_name:{channel_id}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    ch_name, _is_private = lookup_channel_meta(channel_id, workspace)
    ws_name = getattr(workspace, "workspace_name", None) if workspace else None

    if ws_name:
        result = f"#{ch_name} ({ws_name})"
    else:
        result = f"#{ch_name}"

    if ch_name != channel_id:
        _cache_set(cache_key, result, ttl=3600)
    return result


def stored_channel_name(channel_id: str, workspace_id: int | None) -> str | None:
    """Display name saved on a SyncChannel for *channel_id* in *workspace_id*."""
    if not channel_id or not workspace_id:
        return None
    rows = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.channel_id == channel_id,
            schemas.SyncChannel.workspace_id == workspace_id,
            schemas.SyncChannel.deleted_at.is_(None),
        ],
    )
    for row in rows:
        name = (getattr(row, "channel_name", None) or "").strip()
        if name and name != channel_id:
            return name[:100]
    return None


def remember_channel_name(channel_id: str, workspace_id: int | None, name: str | None) -> None:
    """Persist a Slack Channel display name on matching SyncChannel rows."""
    if not channel_id or not workspace_id:
        return
    cleaned = (name or "").strip().removeprefix("#")[:100]
    if not cleaned or cleaned == channel_id:
        return
    DbManager.update_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.channel_id == channel_id,
            schemas.SyncChannel.workspace_id == workspace_id,
        ],
        {schemas.SyncChannel.channel_name: cleaned},
    )


def lookup_channel_meta(
    channel_id: str,
    workspace=None,
    *,
    user_token: str | None = None,
    client: WebClient | None = None,
) -> tuple[str, bool]:
    """Return ``(name, is_private)`` for a Slack channel.

    Tries *client* (the request bot), then the workspace bot token, then
    *user_token*. The bot cannot see a private Channel it has not joined yet,
    which is why publish used to store the Channel ID as ``sync.title``.
    Federated stubs have no token: fall back to ``sync_channels.channel_name``.
    Never log *user_token*.
    """
    if not channel_id:
        return channel_id, False

    from helpers._cache import request_scope_get, request_scope_set

    req_key = f"chan_meta:{channel_id}"
    cached_req = request_scope_get(req_key)
    if isinstance(cached_req, tuple) and len(cached_req) == 2:
        return str(cached_req[0]), bool(cached_req[1])

    cache_key = f"chan_meta:{channel_id}"
    cached = _cache_get(cache_key)
    if isinstance(cached, tuple) and len(cached) == 2:
        request_scope_set(req_key, cached)
        return str(cached[0]), bool(cached[1])

    clients: list[WebClient] = []
    if client is not None:
        clients.append(client)
    bot_token = get_bot_token(workspace) if workspace is not None else None
    if bot_token:
        try:
            clients.append(WebClient(token=bot_token))
        except Exception as exc:
            log_debug("lookup_channel_meta", channel_id=channel_id, error=str(exc))
    if user_token:
        clients.append(WebClient(token=user_token))

    name, is_private = channel_id, False
    for slack_client in clients:
        try:
            info = slack_client.conversations_info(channel=channel_id)
            channel = safe_get(info, "channel") or {}
            found = channel.get("name")
            if isinstance(found, str) and found:
                name = found
                is_private = bool(channel.get("is_private"))
                break
        except Exception as exc:
            log_debug("lookup_channel_meta", channel_id=channel_id, error=str(exc))

    workspace_id = getattr(workspace, "id", None)
    if name == channel_id:
        stored = stored_channel_name(channel_id, workspace_id)
        if stored:
            name = stored

    # Always memoize in request scope (including misses) so publish/Home does not
    # re-hit Slack for the same unknown private channel. Process cache stays
    # success-only so a later join can resolve the name.
    request_scope_set(req_key, (name, is_private))
    if name != channel_id:
        _cache_set(cache_key, (name, is_private), ttl=3600)
    return name, is_private


def slack_apps_uninstall(token: str | None) -> None:
    """Remove this app from a Workspace using Slack ``apps.uninstall``."""
    if not token:
        return
    client_id = os.environ.get(constants.SLACK_CLIENT_ID, "").strip()
    client_secret = os.environ.get(constants.SLACK_CLIENT_SECRET, "").strip()
    if not client_id or not client_secret:
        return
    try:
        WebClient(token=token).apps_uninstall(client_id=client_id, client_secret=client_secret)
    except Exception as exc:
        log_warning("apps_uninstall_failed", error=str(exc))


def uninstall_workspace(team_id: str) -> None:
    """Wipe Bolt install rows for this team, then pause SyncBot workspace data."""
    from helpers.conversations import clear_workspace_installations

    clear_workspace_installations(team_id)
    _soft_delete_uninstalled_workspace(team_id)


def _soft_delete_uninstalled_workspace(team_id: str) -> None:
    """Soft-delete the workspace after Slack uninstalled the app."""
    from helpers.notifications import notify_admins_dm, notify_synced_channels
    from helpers.settings import soft_delete_retention_days

    workspace_record = DbManager.get_record(schemas.Workspace, team_id)
    if not workspace_record:
        log_warning("uninstall_workspace_unknown", team_id=team_id)
        return
    if workspace_record.deleted_at is not None:
        return

    ws_name = resolve_workspace_name(workspace_record)
    retention_days = soft_delete_retention_days()
    my_channels = soft_delete_workspace(workspace_record)

    pending_stubs = DbManager.find_records(
        schemas.FederationPendingStub,
        [schemas.FederationPendingStub.workspace_id == workspace_record.id],
    )
    for pending in pending_stubs:
        peer = DbManager.get_record(schemas.Instance, id=pending.instance_id)
        if not peer or peer.status != "active" or peer.trust_status != "trusted":
            continue
        from helpers.workspace_kind import heal_workspace_to_stub

        if heal_workspace_to_stub(team_id, peer.instance_id, source="uninstall") == "converted":
            from helpers.sync_participation import invalidate_sync_fanout_for_syncs

            invalidate_sync_fanout_for_syncs(c.sync_id for c in my_channels)
            return

    notified_ws: set[int] = set()
    paused_memberships = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.workspace_id == workspace_record.id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.isnot(None),
        ],
    )
    for membership in paused_memberships:
        group_members = DbManager.find_records(
            schemas.WorkspaceGroupMember,
            [
                schemas.WorkspaceGroupMember.group_id == membership.group_id,
                schemas.WorkspaceGroupMember.workspace_id != workspace_record.id,
                schemas.WorkspaceGroupMember.status == "active",
                schemas.WorkspaceGroupMember.deleted_at.is_(None),
            ],
        )
        for member in group_members:
            if not member.workspace_id or member.workspace_id in notified_ws:
                continue
            member_ws = get_workspace_by_id(member.workspace_id)
            if not member_ws or member_ws.deleted_at is not None or not get_bot_token(member_ws):
                continue
            notified_ws.add(member.workspace_id)

            try:
                member_client = WebClient(token=get_bot_token(member_ws))

                notify_admins_dm(
                    member_client,
                    f":double_vertical_bar: `{ws_name}` has uninstalled SyncBot. "
                    f"Syncing has been paused. If they reinstall within {retention_days} days, "
                    "Syncing will resume automatically.",
                    team_id=member_ws.team_id,
                )

                member_channel_ids = []
                for sync_channel in my_channels:
                    sibling_channels = DbManager.find_records(
                        schemas.SyncChannel,
                        [
                            schemas.SyncChannel.sync_id == sync_channel.sync_id,
                            schemas.SyncChannel.workspace_id == member.workspace_id,
                            schemas.SyncChannel.deleted_at.is_(None),
                        ],
                    )
                    for sibling in sibling_channels:
                        member_channel_ids.append(sibling.channel_id)

                if member_channel_ids:
                    notify_synced_channels(
                        member_client,
                        member_channel_ids,
                        f":double_vertical_bar: Syncing with `{ws_name}` has been paused because they uninstalled the app.",
                    )
            except Exception as exc:
                log_warning(
                    "uninstall_notify_failed",
                    workspace_id=member.workspace_id,
                    error=str(exc),
                )
