"""Federation inbound HTTP handlers.

Called from the HTTP request path. Returns ``(status_code, response_dict)``.
Requests without the ``SyncBot-Federation`` User-Agent receive a 404.
"""

import json
import re
import secrets
from datetime import UTC, datetime
from types import SimpleNamespace

from slack_sdk.web import WebClient
from sqlalchemy.exc import OperationalError, ProgrammingError

import constants
import helpers
from db import DbManager, schemas
from federation import core as federation
from helpers.envelope import (
    ACTION_ADD,
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_EDIT,
    ACTION_REMOVE,
    KIND_MESSAGE,
    KIND_REACTION,
)
from helpers.export_import import invalidate_home_tab_caches_for_team
from helpers.files import clear_request_file_pins
from helpers.group_roles import get_owner_team_ids_blocking_stub_pause
from helpers.message_blocks import rewrite_content_blocks, trim_target_blocks
from helpers.sync_apply import apply_target
from helpers.sync_participation import channel_subscribes
from helpers.user_action_echo import slack_message_ts
from helpers.workspace import (
    get_allowed_local_workspace_ids,
    invalidate_fed_ws_for_sync_cache,
    notify_sibling_sync_channels,
    replace_federation_allowlist,
    resolve_workspace_name,
    soft_delete_workspace,
)
from helpers.workspace_kind import (
    heal_workspace_to_stub,
    is_local_workspace,
    mark_peer_untrusted,
    peer_is_trusted,
)
from logger import log_debug, log_error, log_info, log_warning

_NOT_FOUND = (404, {"message": "Not Found"})
_NOT_READY = (503, {"error": "not_ready"})
_BAD_HEADER = (400, {"error": "invalid_header"})
_UNAUTHORIZED = (401, {"error": "unauthorized"})
_INSTANCE_ID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_KNOWN_POST_PATHS = frozenset(
    {
        "/pair",
        "/file",
        "/file/offer",
        "/message",
        "/message/edit",
        "/message/delete",
        "/message/react",
        "/users",
        "/teams",
        "/group-upsert",
        "/group-invite",
        "/group-leave",
        "/sync-upsert",
        "/sync-channel-upsert",
        "/sync-channel-remove",
    }
)


def _inbound_channel(
    channel_id: str,
    fed_ws: schemas.Instance,
    *,
    empty: dict,
) -> tuple[schemas.SyncChannel, schemas.Workspace] | tuple[int, dict]:
    """Resolve a live local target, or return an HTTP skip/404 response.

    First element is ``int`` when skipped.
    """
    resolved = _resolve_channel_for_federated(channel_id, fed_ws, require_active=True)
    if not resolved:
        _heal_inbound_channel_target(channel_id, fed_ws)
        resolved = _resolve_channel_for_federated(channel_id, fed_ws, require_active=True)
    if not resolved:
        log_debug(
            "inbound_skip",
            reason="channel_not_found",
            channel_id=channel_id,
            peer_instance_id=fed_ws.instance_id,
        )
        return _NOT_FOUND
    sync_channel, workspace = resolved
    if not _accept_inbound_workspace(workspace, fed_ws):
        log_debug(
            "inbound_skip",
            reason="pending_live",
            channel_id=channel_id,
            team_id=workspace.team_id,
            peer_instance_id=fed_ws.instance_id,
        )
        return 200, empty
    if not channel_subscribes(sync_channel):
        log_debug(
            "inbound_skip",
            reason="not_subscribed",
            channel_id=channel_id,
            peer_instance_id=fed_ws.instance_id,
        )
        return 200, empty
    return sync_channel, workspace


def _get_post_records(post_id: str, sync_channel_id: int) -> list[schemas.PostMeta]:
    """Look up PostMeta records for a given post_id + sync channel."""
    pid = str(post_id)[:100]
    return DbManager.find_records(
        schemas.PostMeta,
        [schemas.PostMeta.post_id == pid, schemas.PostMeta.sync_channel_id == sync_channel_id],
    )


def _envelope_target_ts(body: dict) -> str | None:
    """Target Channel Slack ts from the envelope, if the origin sent it."""
    raw = body.get("target_ts") if isinstance(body, dict) else None
    if raw is None or raw == "":
        return None
    padded = slack_message_ts(raw)
    return padded or None


def _inbound_thread_ts(
    thread_post_id: str | None,
    sync_channel_id: int,
    *,
    target_ts: str | None = None,
) -> str | None:
    """Parent Slack ts: envelope ``target_ts``, else PostMeta on this SyncChannel."""
    if not thread_post_id:
        return None
    if target_ts:
        return slack_message_ts(target_ts)
    parents = _get_post_records(str(thread_post_id), sync_channel_id)
    if not parents:
        return None
    ordered = sorted(parents, key=lambda row: (row.ts is None, row.ts))
    return slack_message_ts(ordered[0].ts)


def _parent_missing(channel_id: str, post_id: str, peer: schemas.Instance) -> tuple[int, dict]:
    log_debug(
        "inbound_skip",
        reason="parent_missing",
        channel_id=channel_id,
        post_id=post_id,
        peer_instance_id=getattr(peer, "instance_id", None),
    )
    return 409, {"error": "parent_missing"}


def _people_by_user_id(envelope: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for person in envelope.get("people") or []:
        uid = person.get("user_id") if isinstance(person, dict) else None
        if uid:
            out[str(uid)] = person
    return out


def _ingest_envelope_people(envelope: dict, stub_workspace_id: int | None) -> None:
    """Store envelope people on the source stub directory so inbound maps have names/emails."""
    if not stub_workspace_id:
        return
    now = datetime.now(UTC).replace(tzinfo=None)
    for person in envelope.get("people") or []:
        if not isinstance(person, dict):
            continue
        uid = str(person.get("user_id") or "").strip()
        if not uid:
            continue
        email = (person.get("email") or "").strip() or None
        name = (person.get("name") or "").strip() or None
        existing = DbManager.find_records(
            schemas.UserDirectory,
            [
                schemas.UserDirectory.workspace_id == stub_workspace_id,
                schemas.UserDirectory.slack_user_id == uid,
            ],
        )
        if existing:
            row = existing[0]
            updates = {}
            if email and email != row.email:
                updates[schemas.UserDirectory.email] = email
            if name and name != (row.display_name or row.real_name):
                updates[schemas.UserDirectory.display_name] = name[:100]
            if updates:
                updates[schemas.UserDirectory.updated_at] = now
                updates[schemas.UserDirectory.deleted_at] = None
                DbManager.update_records(
                    schemas.UserDirectory,
                    [schemas.UserDirectory.id == row.id],
                    updates,
                )
        else:
            DbManager.create_record(
                schemas.UserDirectory(
                    workspace_id=stub_workspace_id,
                    slack_user_id=uid,
                    email=email,
                    display_name=name[:100] if name else None,
                    updated_at=now,
                )
            )


def _inbound_image_blocks(images: list | None) -> list[dict]:
    """Normalize envelope ``{url, alt_text}`` into Slack image blocks."""
    out: list[dict] = []
    for img in images or []:
        if not isinstance(img, dict):
            continue
        url = img.get("url") or img.get("image_url")
        if url:
            out.append(
                {
                    "type": "image",
                    "image_url": url,
                    "alt_text": img.get("alt_text") or "Shared image",
                }
            )
    return out


def _prepare_inbound_envelope(envelope: dict, workspace: schemas.Workspace) -> None:
    """Map the author and rewrite mentions/blocks using this instance's directory."""
    try:
        stub_id = envelope.get("source_workspace_id")
        if isinstance(stub_id, int):
            _ingest_envelope_people(envelope, stub_id)
        images = envelope.get("images")
        if images:
            envelope["images"] = _inbound_image_blocks(images)
        people = _people_by_user_id(envelope)
        source_user_id = envelope.get("source_user_id")
        if source_user_id and not envelope.get("mapped_user_id"):
            bot_token = helpers.get_bot_token(workspace)
            ws_client = WebClient(token=bot_token) if bot_token else None
            mapped = _ensure_federated_author_mapped(str(source_user_id), workspace.id, ws_client)
            if mapped:
                envelope["mapped_user_id"] = mapped
            person = people.get(str(source_user_id)) or {}
            if not envelope.get("user_name") and person.get("name"):
                envelope["user_name"] = person.get("name")
            if not envelope.get("user_avatar_url") and person.get("avatar_url"):
                envelope["user_avatar_url"] = person.get("avatar_url")
        label = envelope.get("workspace_name") or "Remote"
        text = envelope.get("text")
        if isinstance(text, str) and text:
            envelope["text"] = _resolve_mentions_for_federated(text, workspace.id, label, people)

        def rewrite_mrkdwn(value: str) -> str:
            return _resolve_mentions_for_federated(value, workspace.id, label, people)

        def map_user_id(uid: str) -> str | None:
            tag = _resolve_mentions_for_federated(f"<@{uid}>", workspace.id, label, people)
            match = re.fullmatch(r"<@(\w+)>", tag or "")
            return match.group(1) if match else None

        def unmapped_label(uid: str) -> str:
            return _resolve_mentions_for_federated(f"<@{uid}>", workspace.id, label, people)

        blocks = envelope.get("blocks")
        if isinstance(blocks, list) and blocks:
            envelope["blocks"] = trim_target_blocks(
                rewrite_content_blocks(blocks, rewrite_mrkdwn, map_user_id, unmapped_label)
            )
    except Exception:
        log_debug(
            "federation_inbound_prepare_failed",
            workspace_id=getattr(workspace, "id", None),
        )


_PAIRING_CODE_RE = re.compile(r"^FED-[0-9A-Fa-f]{8}$")

_FIELD_MAX_LENGTHS = {
    "channel_id": 100,
    "text": 40_000,
    "post_id": 100,
    "reaction": 100,
    "instance_id": 64,
    "webhook_url": 500,
    "code": 20,
    "kind": 20,
    "action": 10,
}


# ---------------------------------------------------------------------------
# Input validation helper
# ---------------------------------------------------------------------------


def _validate_fields(body: dict, required: list[str], extras: list[str] | None = None) -> str | None:
    """Check required fields are present, non-empty, and within length limits.

    Returns an error string on failure, or *None* if valid.
    """
    for field in required:
        val = body.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            return f"missing_{field}"

    for field in required + (extras or []):
        val = body.get(field)
        max_len = _FIELD_MAX_LENGTHS.get(field)
        if max_len and isinstance(val, str) and len(val) > max_len:
            return f"{field}_too_long"

    return None


def _ensure_federated_author_mapped(
    source_user_id: str,
    target_workspace_id: int,
    target_client: WebClient | None,
) -> str | None:
    """On-the-fly email map for a federated author using local directory email only."""
    try:
        dir_rows = DbManager.find_records(
            schemas.UserDirectory,
            [
                schemas.UserDirectory.slack_user_id == source_user_id,
                schemas.UserDirectory.deleted_at.is_(None),
            ],
        )
        source_workspace_id = None
        for row in dir_rows:
            if row.email and str(row.email).strip():
                source_workspace_id = row.workspace_id
                break
        if not source_workspace_id or target_client is None:
            return None

        return helpers.ensure_mapped_target_user_id(
            source_user_id,
            source_workspace_id,
            target_workspace_id,
            source_client=None,
            target_client=target_client,
        )
    except Exception:
        log_debug(
            "federation_author_map_failed",
            source_user_id=source_user_id,
            target_workspace_id=target_workspace_id,
        )
        return None


def _resolve_mentions_for_federated(
    msg_text: str,
    target_workspace_id: int,
    remote_workspace_label: str,
    people: dict[str, dict] | None = None,
) -> str:
    """Replace ``<@U_REMOTE>`` with native local mentions using *UserMapping* / *UserDirectory* on this instance."""
    if not msg_text:
        return msg_text

    user_ids = list(dict.fromkeys(re.findall(r"<@(\w+)>", msg_text)))
    if not user_ids:
        return msg_text

    maps = DbManager.find_records(
        schemas.UserMapping,
        [
            schemas.UserMapping.target_workspace_id == target_workspace_id,
            schemas.UserMapping.source_user_id.in_(user_ids),
        ],
    )
    maps_by_uid: dict[str, schemas.UserMapping] = {}
    for mapping in maps:
        current = maps_by_uid.get(mapping.source_user_id)
        if current is None or (mapping.target_user_id and not current.target_user_id):
            maps_by_uid[mapping.source_user_id] = mapping

    missing = [uid for uid in user_ids if uid not in maps_by_uid]
    dir_by_uid: dict[str, schemas.UserDirectory] = {}
    if missing:
        for entry in DbManager.find_records(
            schemas.UserDirectory,
            [
                schemas.UserDirectory.slack_user_id.in_(missing),
                schemas.UserDirectory.deleted_at.is_(None),
            ],
        ):
            if entry.slack_user_id not in dir_by_uid:
                dir_by_uid[entry.slack_user_id] = entry

    people = people or {}
    for uid in user_ids:
        mapping = maps_by_uid.get(uid)
        method = getattr(mapping, "map_method", None) if mapping else None
        if mapping and mapping.target_user_id and method != "none":
            rep = f"<@{mapping.target_user_id}>"
        elif mapping and mapping.source_display_name:
            rep = helpers.format_unmapped_author_label(mapping.source_display_name, remote_workspace_label)
        else:
            entry = dir_by_uid.get(uid)
            display = (entry.display_name or entry.real_name) if entry else None
            if not display:
                display = (people.get(uid) or {}).get("name")
            if display:
                rep = helpers.format_unmapped_author_label(display, remote_workspace_label)
            else:
                rep = helpers.format_unmapped_author_label(uid, remote_workspace_label)
        msg_text = re.sub(rf"<@{re.escape(uid)}>", rep, msg_text)

    return msg_text


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------


def _header(headers: dict, name: str) -> str:
    """Return a header value case-insensitively (Function URL lowercases names)."""
    lowered = {str(key).lower(): value for key, value in headers.items()}
    raw = lowered.get(name.lower())
    if isinstance(raw, list | tuple):
        raw = raw[0] if raw else ""
    return str(raw or "")


def _has_federation_user_agent(headers: dict) -> bool:
    return "SyncBot-Federation" in _header(headers, "User-Agent")


def _require_signed_headers(
    headers: dict,
    *,
    file_part: bool = False,
) -> tuple[int, dict] | dict:
    """Return parsed auth headers, or a 400 response if they are missing or malformed."""
    sig = _header(headers, "X-Federation-Signature")
    ts = _header(headers, "X-Federation-Timestamp")
    instance_id = _header(headers, "X-Federation-Instance")
    if not sig or not ts or not instance_id:
        return _BAD_HEADER
    try:
        int(ts)
    except (TypeError, ValueError):
        return _BAD_HEADER
    if not _INSTANCE_ID_RE.fullmatch(instance_id):
        return _BAD_HEADER
    parsed: dict = {"sig": sig, "ts": ts, "instance_id": instance_id}
    if file_part:
        sha256 = _header(headers, "X-Federation-File-Sha256")
        try:
            index = int(_header(headers, "X-Federation-File-Index"))
            total = int(_header(headers, "X-Federation-File-Total"))
            size = int(_header(headers, "X-Federation-File-Size"))
        except (TypeError, ValueError):
            return _BAD_HEADER
        if not sha256 or index < 0 or total < 1 or size < 0:
            return _BAD_HEADER
        parsed.update({"sha256": sha256, "index": index, "total": total, "size": size})
    return parsed


def _trusted_peer_or_401(instance_id: str) -> tuple[int, dict] | schemas.Instance:
    """One Instance lookup by header id. Unknown or untrusted is 401."""
    matches = DbManager.find_records(
        schemas.Instance,
        [schemas.Instance.instance_id == instance_id],
    )
    fed_ws = matches[0] if matches else None
    if not _peer_accepted(fed_ws):
        return _UNAUTHORIZED
    return fed_ws


def _verify_federated_request(body_str: str, headers: dict) -> schemas.Instance | None:
    """Return the trusted peer or *None* (tests and leftover call sites)."""
    result = _verify_known_peer(body_str, headers)
    if isinstance(result, tuple):
        return None
    return result


def _verify_known_peer(body_str: str, headers: dict) -> tuple[int, dict] | schemas.Instance:
    """Validate headers, look up the peer, then verify the raw body."""
    parsed = _require_signed_headers(headers)
    if isinstance(parsed, tuple):
        return parsed
    peer = _trusted_peer_or_401(parsed["instance_id"])
    if isinstance(peer, tuple):
        return peer
    if not federation.federation_verify(body_str, parsed["sig"], parsed["ts"], peer.public_key):
        log_warning("federation_auth_failed", instance_id=parsed["instance_id"])
        return _UNAUTHORIZED
    return peer


def federation_preflight(method: str, path: str, headers: dict) -> tuple[int, dict] | None:
    """Header-only reject before reading a body. ``None`` means continue."""
    if not _has_federation_user_agent(headers):
        return _NOT_FOUND
    try:
        if not helpers.federation_enabled():
            return _NOT_FOUND
    except (OperationalError, ProgrammingError):
        return _NOT_READY
    base = constants.FEDERATION_API_BASE_PATH
    if not path.startswith(base):
        return _NOT_FOUND
    subpath = path[len(base) :] or "/"
    if subpath == "/ping" and method == "GET":
        parsed = _require_signed_headers(headers)
        if isinstance(parsed, tuple):
            return parsed
        peer = _trusted_peer_or_401(parsed["instance_id"])
        if isinstance(peer, tuple):
            return peer
        return None
    if method != "POST" or subpath not in _KNOWN_POST_PATHS:
        return _NOT_FOUND
    parsed = _require_signed_headers(headers, file_part=subpath == "/file")
    if isinstance(parsed, tuple):
        return parsed
    return None


# ---------------------------------------------------------------------------
# Channel access scoping
# ---------------------------------------------------------------------------


def _federated_has_channel_access(fed_ws: schemas.Instance, sync_channel: schemas.SyncChannel) -> bool:
    """Return *True* if *fed_ws* shares a group with the target channel's workspace.

    Peer identity is a stub ``Workspace`` (``instance_id`` on the
    workspace row). The target channel must belong to a local workspace in a
    shared active group with a stub for this peer.
    """
    sync = DbManager.get_record(schemas.Sync, id=sync_channel.sync_id)
    if not sync or not sync.group_id:
        return False
    target_ws = helpers.get_workspace_by_id(sync_channel.workspace_id)
    if not target_ws or not is_local_workspace(target_ws):
        return False
    stub_members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == sync.group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
            schemas.WorkspaceGroupMember.workspace_id.isnot(None),
        ],
    )
    for member in stub_members:
        ws = helpers.get_workspace_by_id(member.workspace_id)
        if ws and ws.instance_id == fed_ws.instance_id:
            return True
    return False


def _resolve_channel_for_federated(
    channel_id: str,
    fed_ws: schemas.Instance,
    *,
    require_active: bool = False,
) -> tuple[schemas.SyncChannel, schemas.Workspace] | None:
    """Look up a sync channel, verify federated access, and return the workspace.

    Returns ``(sync_channel, workspace)`` or *None* if any check fails.
    """
    filters = [
        schemas.SyncChannel.channel_id == channel_id,
        schemas.SyncChannel.deleted_at.is_(None),
    ]
    if require_active:
        filters.append(schemas.SyncChannel.status == "active")

    records = DbManager.find_records(schemas.SyncChannel, filters)
    if not records:
        return None

    for sync_channel in records:
        if not _federated_has_channel_access(fed_ws, sync_channel):
            continue
        workspace = helpers.get_workspace_by_id(sync_channel.workspace_id)
        if workspace and is_local_workspace(workspace):
            return sync_channel, workspace
    return None


def _get_local_workspace_ids(fed_ws: schemas.Instance) -> set[int]:
    """Return local workspace IDs allowed for *fed_ws* (allowlist, else shared groups)."""
    return get_allowed_local_workspace_ids(fed_ws.instance_id)


def _stub_workspace_for_peer(fed_ws: schemas.Instance, team_id: str | None = None) -> schemas.Workspace | None:
    """Return a stub Workspace for *fed_ws*, optionally matching *team_id*."""
    filters = [
        schemas.Workspace.instance_id == fed_ws.instance_id,
        schemas.Workspace.deleted_at.is_(None),
    ]
    if team_id:
        filters.append(schemas.Workspace.team_id == team_id)
    rows = DbManager.find_records(schemas.Workspace, filters)
    return rows[0] if rows else None


def _source_stub_id(fed_ws: schemas.Instance, envelope: dict) -> int | None:
    """Local stub workspace id to use as inbound ``source_workspace_id``.

    Requires ``source_team_id``. Do not fall back to an arbitrary stub for this
    peer: a connection can have several remote teams.
    """
    team_id = envelope.get("source_team_id")
    if not (isinstance(team_id, str) and team_id.strip()):
        return None
    stub = _stub_workspace_for_peer(fed_ws, team_id.strip())
    return stub.id if stub else None


def _reject_unknown_source_team(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict] | None:
    """404 when the envelope names a source team that is not on this peer's allowlist."""
    team_id = body.get("source_team_id")
    if not (isinstance(team_id, str) and team_id.strip()):
        return None
    if _source_stub_id(fed_ws, body) is not None:
        return None
    log_debug(
        "inbound_skip",
        reason="source_not_allowed",
        team_id=team_id.strip(),
        peer_instance_id=fed_ws.instance_id,
    )
    return 404, {"error": "workspace_not_found"}


def _peer_accepted(fed_ws: schemas.Instance | None) -> bool:
    return bool(fed_ws and getattr(fed_ws, "status", None) == "active" and peer_is_trusted(fed_ws))


def _pairing_allowed_workspace_ids(pairing) -> list[int]:
    raw = getattr(pairing, "allowed_workspace_ids", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    ids: list[int] = []
    for item in parsed:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return ids


def _apply_pairing_allowlist(pairing, peer: schemas.Instance) -> None:
    workspace_ids = _pairing_allowed_workspace_ids(pairing)
    if workspace_ids and peer is not None:
        replace_federation_allowlist(peer.instance_id, workspace_ids)


def _consume_pairing_code(pairing) -> None:
    if pairing is None or not getattr(pairing, "id", None):
        return
    federation.delete_pairing_code(pairing.id)


def _heal_pair_teams(body: dict, pairing, peer: schemas.Instance) -> dict[str, str]:
    """Heal every team_id in a pair request plus its subject hint."""
    team_ids: list[str] = []
    if isinstance(body.get("team_id"), str):
        team_ids.append(body["team_id"].strip())
    subject_team_id = getattr(pairing, "subject_team_id", None)
    if subject_team_id:
        team_ids.append(str(subject_team_id).strip())
    results: dict[str, str] = {}
    for team_id in dict.fromkeys(team_id for team_id in team_ids if team_id):
        results[team_id] = heal_workspace_to_stub(team_id, peer.instance_id, source="pair")
    return results


def _remote_source_ref(fed_ws: schemas.Instance, body: dict, stub_id: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        channel_id=body.get("source_channel_id") or f"fed:{fed_ws.instance_id}",
        id=None,
        workspace_id=stub_id,
    )


def _create_response_from_rows(rows: list) -> dict | None:
    if not rows:
        return None
    ordered = sorted(rows, key=lambda r: (r.ts is None, r.ts))
    return {
        "ok": True,
        "ts": slack_message_ts(ordered[0].ts),
        "posted_as_user_id": ordered[0].posted_as_user_id,
    }


def _heal_inbound_channel_target(channel_id: str, peer: schemas.Instance) -> None:
    """Heal a pending soft-deleted target before active-channel resolution."""
    channels = DbManager.find_records(
        schemas.SyncChannel,
        [schemas.SyncChannel.channel_id == channel_id],
    )
    for sync_channel in channels:
        workspace = helpers.get_workspace_by_id(sync_channel.workspace_id)
        if not workspace:
            continue
        pending = DbManager.find_records(
            schemas.FederationPendingStub,
            [
                schemas.FederationPendingStub.workspace_id == workspace.id,
                schemas.FederationPendingStub.instance_id == peer.instance_id,
            ],
        )
        if not pending:
            continue
        if workspace.deleted_at is not None:
            heal_workspace_to_stub(workspace.team_id, peer.instance_id, source="inbound")


def _accept_inbound_workspace(workspace: schemas.Workspace, peer: schemas.Instance) -> bool:
    """Reject traffic only when this peer has a pending claim on a live target."""
    if not is_local_workspace(workspace):
        return True
    pending = DbManager.find_records(
        schemas.FederationPendingStub,
        [
            schemas.FederationPendingStub.workspace_id == workspace.id,
            schemas.FederationPendingStub.instance_id == peer.instance_id,
        ],
    )
    return not pending


def _workspace_for_replicated_team(
    fed_ws: schemas.Instance,
    team_id: str,
    workspace_name: str | None = None,
    *,
    create_stub: bool = False,
) -> schemas.Workspace | None:
    """Resolve a replicated team_id: allowlisted live or this peer's existing stub."""
    from helpers.workspace_kind import ensure_stub_workspace, is_local_workspace, is_stub_workspace

    ws = DbManager.get_record(schemas.Workspace, id=team_id)
    if ws and is_local_workspace(ws):
        if ws.id not in _get_local_workspace_ids(fed_ws):
            return None
        return ws
    if ws and is_stub_workspace(ws):
        if ws.instance_id != fed_ws.instance_id:
            return None
        return ws
    if not create_stub:
        return None
    return ensure_stub_workspace(
        team_id=team_id,
        workspace_name=workspace_name if isinstance(workspace_name, str) else team_id,
        instance_id=fed_ws.instance_id,
    )


def _invalidate_local_homes() -> None:
    """Drop Home hashes for every live install on this instance (no Slack publish)."""
    rows = DbManager.find_records(schemas.Workspace, [schemas.Workspace.deleted_at.is_(None)])
    for workspace in rows:
        if is_local_workspace(workspace) and workspace.team_id:
            invalidate_home_tab_caches_for_team(workspace.team_id)


def _invalidate_replicated_group_homes(group_id: int, sync_id: int | None = None) -> None:
    """Drop Home hashes so replicated members and Channels show without a forced Refresh."""
    from helpers.sync_participation import invalidate_sync_fanout_for_syncs

    members = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group_id,
            schemas.WorkspaceGroupMember.status == "active",
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    for member in members:
        if not member.workspace_id:
            continue
        rows = DbManager.find_records(schemas.Workspace, [schemas.Workspace.id == member.workspace_id])
        ws = rows[0] if rows else None
        if ws and is_local_workspace(ws) and ws.team_id:
            invalidate_home_tab_caches_for_team(ws.team_id)
    if sync_id:
        invalidate_sync_fanout_for_syncs([sync_id])


# ---------------------------------------------------------------------------
# POST /api/federation/pair
# ---------------------------------------------------------------------------


def _pair_client_error(reason: str, status: int = 400) -> tuple[int, dict]:
    log_warning("federation_pair", direction="inbound", ok=False, reason=reason)
    return status, {"error": reason}


def handle_pair(body: dict, body_str: str, headers: dict) -> tuple[int, dict]:
    """Accept an incoming connection request from a remote instance.

    The remote instance sends its ``code``, ``webhook_url``, ``instance_id``,
    and ``public_key``.  The request must be signed with the sender's private
    key so we can verify it matches the included public key. Pairing codes are
    pairing-only (``FederationPairingCode``); they do not create a group.
    """
    parsed = _require_signed_headers(headers)
    if isinstance(parsed, tuple):
        return parsed

    err = _validate_fields(body, ["code", "webhook_url", "instance_id", "public_key"])
    if err:
        return _pair_client_error(err)

    code = body["code"]
    remote_url = body["webhook_url"]
    remote_instance_id = body["instance_id"]
    remote_public_key = body["public_key"]

    if parsed["instance_id"] != remote_instance_id:
        return _BAD_HEADER

    if not federation.instance_id_matches_public_key(remote_instance_id, remote_public_key):
        return _pair_client_error("invalid_instance_id")

    if not federation.federation_verify(body_str, parsed["sig"], parsed["ts"], remote_public_key):
        return _pair_client_error("invalid_signature", 401)

    if not _PAIRING_CODE_RE.match(code):
        return _pair_client_error("invalid_code_format")

    pairing_rows = DbManager.find_records(
        schemas.FederationPairingCode,
        [schemas.FederationPairingCode.code == code],
    )
    if not pairing_rows:
        log_warning("federation_pair", direction="inbound", ok=False, reason="unknown_code")
        return _NOT_FOUND
    pairing = pairing_rows[0]
    created_at = getattr(pairing, "created_at", None)
    if created_at is not None:
        age = datetime.now(UTC).replace(tzinfo=None) - (
            created_at.replace(tzinfo=None) if getattr(created_at, "tzinfo", None) else created_at
        )
        if age.total_seconds() > 24 * 3600:
            federation.delete_pairing_code(pairing.id)
            log_warning(
                "federation_pair",
                direction="inbound",
                ok=False,
                reason="code_expired",
            )
            return 410, {"error": "code_expired"}

    if not federation.validate_webhook_url(remote_url):
        return _pair_client_error("invalid_webhook_url")

    existing_peers = DbManager.find_records(
        schemas.Instance,
        [
            schemas.Instance.instance_id == remote_instance_id,
            schemas.Instance.status == "active",
        ],
    )
    if existing_peers:
        existing = existing_peers[0]
        if (existing.public_key or "").strip() == (remote_public_key or "").strip():
            # URL heal: same key, new URL — keep trust_status as-is.
            DbManager.update_records(
                schemas.Instance,
                [schemas.Instance.instance_id == existing.instance_id],
                {
                    schemas.Instance.webhook_url: remote_url,
                    schemas.Instance.updated_at: datetime.now(UTC).replace(tzinfo=None),
                },
            )
            results = _heal_pair_teams(body, pairing, existing)
            _apply_pairing_allowlist(pairing, existing)
            _consume_pairing_code(pairing)
            invalidate_fed_ws_for_sync_cache()
            federation.push_allowed_workspaces(existing)
            try:
                from federation.replicate import replicate_peer_snapshot

                replicate_peer_snapshot(existing)
            except Exception:
                log_error("federation_snapshot", peer_instance_id=existing.instance_id, ok=False)
            _, our_public_key = federation.get_or_create_instance_keypair()
            log_info(
                "federation_pair",
                direction="inbound",
                peer_instance_id=remote_instance_id,
                url_updated=True,
                heal=results,
            )
            _invalidate_local_homes()
            return 200, {
                "ok": True,
                "instance_id": federation.get_instance_id(),
                "public_key": our_public_key,
                "url_updated": True,
                "json_chunk_mb": constants.json_chunk_mb(),
            }
        # Same instance_id with a different key. Untrust and pause stubs
        # rather than silent overwrite.
        mark_peer_untrusted(existing)
        _consume_pairing_code(pairing)
        invalidate_fed_ws_for_sync_cache()
        log_warning(
            "federation_pair",
            direction="inbound",
            ok=False,
            reason="already_connected",
            peer_instance_id=remote_instance_id,
        )
        return 409, {"error": "already_connected"}

    fed_ws_name = pairing.label or f"Connection {remote_instance_id[:8]}"
    _team_id = body.get("team_id")
    primary_team_id = _team_id.strip() if isinstance(_team_id, str) and _team_id.strip() else None
    primary_workspace_name = body.get("workspace_name") if isinstance(body.get("workspace_name"), str) else None

    # New fingerprint = new keypair. Pause any still-trusted peer for this Slack team.
    if primary_team_id:
        predecessors = DbManager.find_records(
            schemas.Instance,
            [
                schemas.Instance.primary_team_id == primary_team_id,
                schemas.Instance.status == "active",
                schemas.Instance.instance_id != remote_instance_id,
            ],
        )
        for old in predecessors:
            mark_peer_untrusted(old)

    fed_ws = federation.get_or_create_instance(
        instance_id=remote_instance_id,
        webhook_url=remote_url,
        public_key=remote_public_key,
        name=fed_ws_name,
        primary_team_id=primary_team_id,
        primary_workspace_name=primary_workspace_name,
    )

    # Heal matching teams. A live install stays live and records a pending stub.
    # subject_team_id is only a hint; process every team_id.
    results = _heal_pair_teams(body, pairing, fed_ws)
    _apply_pairing_allowlist(pairing, fed_ws)

    _consume_pairing_code(pairing)

    invalidate_fed_ws_for_sync_cache()
    federation.push_allowed_workspaces(fed_ws)
    try:
        from federation.replicate import replicate_peer_snapshot

        replicate_peer_snapshot(fed_ws)
    except Exception:
        log_error("federation_snapshot", peer_instance_id=fed_ws.instance_id, ok=False)

    _, our_public_key = federation.get_or_create_instance_keypair()

    log_info(
        "federation_pair",
        direction="inbound",
        peer_instance_id=remote_instance_id,
        url_updated=False,
        heal=results,
    )
    _invalidate_local_homes()

    return 200, {
        "ok": True,
        "instance_id": federation.get_instance_id(),
        "public_key": our_public_key,
        "json_chunk_mb": constants.json_chunk_mb(),
    }


# ---------------------------------------------------------------------------
# POST /api/federation/message
# ---------------------------------------------------------------------------


def handle_message(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    """Apply inbound create locally."""
    from federation.files import AssembleFailed, materialize_file
    from helpers.files import hashed_file_path, pin_hashed_file

    err = _validate_fields(body, ["channel_id", "post_id", "kind", "action"])
    if err:
        return 400, {"error": err}
    if body.get("kind") != KIND_MESSAGE:
        return 400, {"error": "invalid_kind"}
    if body.get("action") != ACTION_CREATE:
        return 400, {"error": "invalid_action"}

    channel_id = body["channel_id"]
    post_id = str(body["post_id"])
    target_ts = _envelope_target_ts(body)
    target = _inbound_channel(channel_id, fed_ws, empty={"ok": True, "ts": None})
    if isinstance(target[0], int):
        return target
    rejected = _reject_unknown_source_team(body, fed_ws)
    if rejected:
        return rejected
    sync_channel, workspace = target

    existing_rows = _get_post_records(post_id, sync_channel.id)
    existing_resp = _create_response_from_rows(existing_rows)
    if existing_resp:
        log_debug(
            "inbound_idempotent",
            channel_id=channel_id,
            post_id=post_id,
            peer_instance_id=fed_ws.instance_id,
        )
        return 200, existing_resp

    try:
        envelope = dict(body)
        envelope["post_id"] = post_id
        envelope.pop("channel_id", None)
        envelope.pop("mapped_user_id", None)
        stub_id = _source_stub_id(fed_ws, envelope)
        if stub_id is not None:
            envelope["source_workspace_id"] = stub_id

        declared_refs = list(envelope.get("file_refs") or [])
        file_refs = []
        for ref in declared_refs:
            sha256 = ref.get("sha256") if isinstance(ref, dict) else None
            if not sha256:
                return 409, {"error": "incomplete_file"}
            size = int(ref.get("size") or 0)
            try:
                path = materialize_file(sha256, size)
            except AssembleFailed:
                return 409, {"error": "assemble_failed", "sha256": sha256}
            if not path:
                return 409, {"error": "incomplete_file", "sha256": sha256}
            pin_hashed_file(sha256)
            file_refs.append(
                {
                    "sha256": sha256,
                    "name": ref.get("name") or "file",
                    "mimetype": ref.get("mimetype") or "application/octet-stream",
                    "size": size,
                    "path": path or hashed_file_path(sha256),
                }
            )
        if declared_refs:
            envelope["file_refs"] = file_refs

        thread_post_id = envelope.get("thread_post_id")
        thread_ts = _inbound_thread_ts(thread_post_id, sync_channel.id, target_ts=target_ts)
        if thread_post_id and not thread_ts:
            return _parent_missing(channel_id, str(thread_post_id), fed_ws)

        _prepare_inbound_envelope(envelope, workspace)

        outcome = apply_target(
            envelope,
            sync_channel,
            workspace,
            source_sync_channel=_remote_source_ref(fed_ws, body, stub_id),
            thread_ts=thread_ts,
        )
        return 200, {
            "ok": True,
            "ts": outcome.ts,
            "posted_as_user_id": outcome.posted_as_user_id,
        }

    except Exception:
        log_error("federation_message_error", channel_id=channel_id, exc_info=True)
        return 500, {"error": "internal_error"}


# ---------------------------------------------------------------------------
# POST /api/federation/message/edit
# ---------------------------------------------------------------------------


def handle_message_edit(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    """Apply inbound edit locally."""
    err = _validate_fields(body, ["post_id", "channel_id", "kind", "action"])
    if err:
        return 400, {"error": err}
    if body.get("kind") != KIND_MESSAGE:
        return 400, {"error": "invalid_kind"}
    if body.get("action") != ACTION_EDIT:
        return 400, {"error": "invalid_action"}

    post_id = str(body["post_id"])
    channel_id = body["channel_id"]

    target = _inbound_channel(channel_id, fed_ws, empty={"ok": True, "updated": 0})
    if isinstance(target[0], int):
        return target
    rejected = _reject_unknown_source_team(body, fed_ws)
    if rejected:
        return rejected
    sync_channel, workspace = target

    post_records = _get_post_records(post_id, sync_channel.id)
    if not post_records:
        return _parent_missing(channel_id, post_id, fed_ws)
    stub_id = _source_stub_id(fed_ws, body)
    envelope = dict(body)
    envelope["post_id"] = post_id
    envelope.pop("channel_id", None)
    envelope.pop("mapped_user_id", None)
    envelope["kind"] = KIND_MESSAGE
    envelope["action"] = ACTION_EDIT
    if stub_id is not None:
        envelope["source_workspace_id"] = stub_id
    _prepare_inbound_envelope(envelope, workspace)

    source_ref = _remote_source_ref(fed_ws, body, stub_id)
    updated = 0
    for post_meta in post_records:
        try:
            apply_target(
                dict(envelope),
                sync_channel,
                workspace,
                source_sync_channel=source_ref,
                target_post_meta=post_meta,
            )
            updated += 1
        except Exception:
            log_warning("federation_edit_failed", channel_id=channel_id, ts=slack_message_ts(post_meta.ts))

    return 200, {"ok": True, "updated": updated}


# ---------------------------------------------------------------------------
# POST /api/federation/message/delete
# ---------------------------------------------------------------------------


def handle_message_delete(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    """Apply inbound delete locally."""
    err = _validate_fields(body, ["post_id", "channel_id", "kind", "action"])
    if err:
        return 400, {"error": err}
    if body.get("kind") != KIND_MESSAGE:
        return 400, {"error": "invalid_kind"}
    if body.get("action") != ACTION_DELETE:
        return 400, {"error": "invalid_action"}

    post_id = str(body["post_id"])
    channel_id = body["channel_id"]

    target = _inbound_channel(channel_id, fed_ws, empty={"ok": True, "deleted": 0})
    if isinstance(target[0], int):
        return target
    rejected = _reject_unknown_source_team(body, fed_ws)
    if rejected:
        return rejected
    sync_channel, workspace = target

    post_records = _get_post_records(post_id, sync_channel.id)
    if not post_records:
        return _parent_missing(channel_id, post_id, fed_ws)
    stub_id = _source_stub_id(fed_ws, body)
    envelope = dict(body)
    envelope["post_id"] = post_id
    envelope.pop("channel_id", None)
    envelope.pop("mapped_user_id", None)
    envelope["kind"] = KIND_MESSAGE
    envelope["action"] = ACTION_DELETE
    if stub_id is not None:
        envelope["source_workspace_id"] = stub_id

    source_ref = _remote_source_ref(fed_ws, body, stub_id)
    deleted = 0
    for post_meta in post_records:
        try:
            apply_target(
                dict(envelope),
                sync_channel,
                workspace,
                source_sync_channel=source_ref,
                target_post_meta=post_meta,
            )
            deleted += 1
        except Exception:
            log_warning("federation_delete_failed", channel_id=channel_id, ts=slack_message_ts(post_meta.ts))

    return 200, {"ok": True, "deleted": deleted}


# ---------------------------------------------------------------------------
# POST /api/federation/message/react
# ---------------------------------------------------------------------------


def handle_message_react(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    """Apply inbound reaction add/remove locally."""
    err = _validate_fields(body, ["post_id", "channel_id", "reaction", "kind", "action"])
    if err:
        return 400, {"error": err}
    if body.get("kind") != KIND_REACTION:
        return 400, {"error": "invalid_kind"}
    action = body.get("action")
    if action not in (ACTION_ADD, ACTION_REMOVE):
        return 400, {"error": "invalid_action"}

    post_id = str(body["post_id"])
    channel_id = body["channel_id"]

    resolved = _inbound_channel(channel_id, fed_ws, empty={"ok": True, "applied": 0})
    if isinstance(resolved[0], int):
        return resolved
    rejected = _reject_unknown_source_team(body, fed_ws)
    if rejected:
        return rejected
    sync_channel, workspace = resolved

    post_records = _get_post_records(post_id, sync_channel.id)
    if not post_records:
        return _parent_missing(channel_id, post_id, fed_ws)

    stub_id = _source_stub_id(fed_ws, body)
    envelope = dict(body)
    envelope["post_id"] = post_id
    envelope["action"] = action
    envelope["kind"] = KIND_REACTION
    envelope.pop("mapped_user_id", None)
    if stub_id is not None:
        envelope["source_workspace_id"] = stub_id
    _prepare_inbound_envelope(envelope, workspace)

    name_probe_cache: dict = {}
    applied = 0
    source_ref = _remote_source_ref(fed_ws, body, stub_id)
    for post_meta in post_records:
        try:
            outcome = apply_target(
                dict(envelope),
                sync_channel,
                workspace,
                source_sync_channel=source_ref,
                target_post_meta=post_meta,
                name_probe_cache=name_probe_cache,
            )
            if outcome.reaction_applied:
                applied += 1
        except Exception:
            log_warning(
                "federation_react_failed",
                channel_id=channel_id,
                ts=slack_message_ts(post_meta.ts),
            )

    return 200, {"ok": True, "applied": applied}


# ---------------------------------------------------------------------------
# POST /api/federation/users
# ---------------------------------------------------------------------------


def _page_users_for_json_cap(
    users: list[dict],
    offset: int,
    chunk_mb: int,
) -> tuple[list[dict], int | None]:
    """Return a page of *users* that serializes under *chunk_mb* (0 = remainder)."""
    if offset >= len(users):
        return [], None
    if chunk_mb == 0:
        return users[offset:], None
    cap = chunk_mb * 1024 * 1024
    page: list[dict] = []
    index = offset
    while index < len(users):
        candidate = page + [users[index]]
        probe = json.dumps({"ok": True, "users": candidate, "json_chunk_mb": chunk_mb, "next_offset": index + 1})
        if page and len(probe.encode()) > cap:
            break
        page.append(users[index])
        index += 1
    next_offset = index if index < len(users) else None
    return page, next_offset


def handle_users(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    """Exchange user directory with a federated workspace.

    Incoming users are stored under an existing stub for ``team_id``.
    Only returns users from workspaces on the allowlist (or shared groups).
    """
    remote_users = body.get("users", []) or []
    if not isinstance(remote_users, list):
        remote_users = []
    remote_team_id = body.get("team_id") if isinstance(body.get("team_id"), str) else None
    remote_workspace_name = body.get("workspace_name") if isinstance(body.get("workspace_name"), str) else None
    if remote_team_id:
        remote_team_id = remote_team_id.strip() or None

    stub = _stub_workspace_for_peer(fed_ws, remote_team_id) if remote_users and remote_team_id else None
    if stub is not None:
        if remote_workspace_name and (stub.workspace_name or "") != remote_workspace_name:
            DbManager.update_records(
                schemas.Workspace,
                [schemas.Workspace.id == stub.id],
                {schemas.Workspace.workspace_name: remote_workspace_name[:100]},
            )
        workspace_id = stub.id
        now = datetime.now(UTC)
        existing_rows = DbManager.find_records(
            schemas.UserDirectory,
            [schemas.UserDirectory.workspace_id == workspace_id],
        )
        existing_by_uid = {row.slack_user_id: row for row in existing_rows}
        to_create: list[schemas.UserDirectory] = []
        for u in remote_users:
            uid = u.get("user_id", "") or ""
            existing = existing_by_uid.get(uid)
            if existing:
                new_email = u.get("email")
                new_real = u.get("real_name")
                new_display = u.get("display_name")
                if (
                    existing.email == new_email
                    and existing.real_name == new_real
                    and existing.display_name == new_display
                ):
                    continue
                DbManager.update_records(
                    schemas.UserDirectory,
                    [schemas.UserDirectory.id == existing.id],
                    {
                        schemas.UserDirectory.email: new_email,
                        schemas.UserDirectory.real_name: new_real,
                        schemas.UserDirectory.display_name: new_display,
                        schemas.UserDirectory.updated_at: now,
                    },
                )
            else:
                to_create.append(
                    schemas.UserDirectory(
                        workspace_id=workspace_id,
                        slack_user_id=uid,
                        email=u.get("email"),
                        real_name=u.get("real_name"),
                        display_name=u.get("display_name"),
                        updated_at=now,
                    )
                )
        if to_create:
            DbManager.create_records(to_create)

        log_info(
            "federation_users_received",
            peer_instance_id=fed_ws.instance_id,
            count=len(remote_users),
        )

    allowed_ws_ids = _get_local_workspace_ids(fed_ws)

    local_users = []
    for ws_id in allowed_ws_ids:
        ws = helpers.get_workspace_by_id(ws_id)
        if not ws or ws.deleted_at:
            continue
        users = DbManager.find_records(
            schemas.UserDirectory,
            [schemas.UserDirectory.workspace_id == ws_id, schemas.UserDirectory.deleted_at.is_(None)],
        )
        for u in users:
            local_users.append(
                {
                    "user_id": u.slack_user_id,
                    "email": u.email,
                    "real_name": u.real_name,
                    "display_name": u.display_name,
                    "workspace_id": ws_id,
                    "team_id": ws.team_id,
                    "workspace_name": ws.workspace_name,
                }
            )

    try:
        recv_offset = int(body.get("offset") or 0)
    except (TypeError, ValueError):
        recv_offset = 0
    if recv_offset < 0:
        recv_offset = 0

    chunk_mb = constants.json_chunk_mb()
    page, next_offset = _page_users_for_json_cap(local_users, recv_offset, chunk_mb)
    resp: dict = {"ok": True, "users": page, "json_chunk_mb": chunk_mb}
    if next_offset is not None:
        resp["next_offset"] = next_offset
    return 200, resp


# ---------------------------------------------------------------------------
# POST /api/federation/teams
# ---------------------------------------------------------------------------


def handle_teams(body: dict, peer: schemas.Instance) -> tuple[int, dict]:
    """Convert peer Workspaces into stubs for *peer*.

    Reads ``workspaces`` (``team_id`` + display ``name``) and optional
    ``primary_team_id`` / ``primary_workspace_name``. Each listed team is healed
    to a stub for this peer. Stubs for this peer that left the payload are paused
    after the owner gate. Display names are refreshed; they are not used for
    trust. Never pauses a live install or a stub owned by a different peer.
    """
    from helpers.workspace_kind import ensure_stub_workspace, is_stub_workspace

    raw_workspaces = body.get("workspaces")
    if not isinstance(raw_workspaces, list):
        return 400, {"error": "missing_workspaces"}

    seen: list[str] = []
    names: dict[str, str] = {}
    for item in raw_workspaces:
        if not isinstance(item, dict):
            continue
        team_id = (item.get("team_id") or "").strip()
        if not team_id:
            continue
        seen.append(team_id)
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            names[team_id] = name.strip()

    results: dict[str, str] = {}
    for team_id in dict.fromkeys(seen):
        result = heal_workspace_to_stub(team_id, peer.instance_id, source="teams")
        if result == "missing":
            stub = ensure_stub_workspace(
                team_id=team_id,
                workspace_name=names.get(team_id) or team_id,
                instance_id=peer.instance_id,
            )
            result = "ensured" if stub is not None else "missing"
        elif names.get(team_id):
            workspace = DbManager.get_record(schemas.Workspace, id=team_id)
            if workspace and is_stub_workspace(workspace) and (workspace.workspace_name or "") != names[team_id]:
                DbManager.update_records(
                    schemas.Workspace,
                    [schemas.Workspace.id == workspace.id],
                    {schemas.Workspace.workspace_name: names[team_id][:100]},
                )
        results[team_id] = result

    primary_id = body.get("primary_team_id") if isinstance(body.get("primary_team_id"), str) else None
    primary_name = body.get("primary_workspace_name") if isinstance(body.get("primary_workspace_name"), str) else None
    updates: dict = {}
    if primary_id and primary_id.strip():
        updates[schemas.Instance.primary_team_id] = primary_id.strip()
    if primary_name and primary_name.strip():
        updates[schemas.Instance.primary_workspace_name] = primary_name.strip()[:100]
    if updates:
        DbManager.update_records(
            schemas.Instance,
            [schemas.Instance.instance_id == peer.instance_id],
            updates,
        )

    listed = set(dict.fromkeys(seen))
    stubs = DbManager.find_records(
        schemas.Workspace,
        [
            schemas.Workspace.instance_id == peer.instance_id,
            schemas.Workspace.deleted_at.is_(None),
        ],
    )
    to_pause = [workspace for workspace in stubs if (workspace.team_id or "") not in listed]
    blocked = get_owner_team_ids_blocking_stub_pause(
        peer.instance_id,
        [workspace.team_id for workspace in to_pause if workspace.team_id],
    )
    blocked_set = set(blocked)
    for workspace in to_pause:
        if (workspace.team_id or "") in blocked_set:
            continue
        soft_delete_workspace(workspace)

    log_debug("teams_heal", peer_instance_id=peer.instance_id, heal=results)
    invalidate_fed_ws_for_sync_cache()
    _invalidate_local_homes()
    if blocked:
        return 409, {
            "error": "owner_on_connection",
            "team_ids": sorted(blocked),
            "results": results,
        }
    return 200, {"ok": True, "results": results}


# ---------------------------------------------------------------------------
# GET /api/federation/ping
# ---------------------------------------------------------------------------


def handle_ping(headers: dict) -> tuple[int, dict]:
    """Health check for a connected peer. Requires a trusted signature over empty body."""
    parsed = _require_signed_headers(headers)
    if isinstance(parsed, tuple):
        return parsed
    peer = _trusted_peer_or_401(parsed["instance_id"])
    if isinstance(peer, tuple):
        return peer
    if not federation.federation_verify("", parsed["sig"], parsed["ts"], peer.public_key):
        return _UNAUTHORIZED
    return 200, {
        "ok": True,
        "instance_id": federation.get_instance_id(),
        "timestamp": datetime.now(UTC).isoformat(),
    }


def handle_group_upsert(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    """Upsert a WorkspaceGroup by uid."""
    err = _validate_fields(body, ["uid", "name"])
    if err:
        return 400, {"error": err}
    uid = str(body["uid"])
    name = str(body["name"])[:100]
    rows = DbManager.find_records(
        schemas.WorkspaceGroup,
        [schemas.WorkspaceGroup.uid == uid],
    )
    if rows:
        DbManager.update_records(
            schemas.WorkspaceGroup,
            [schemas.WorkspaceGroup.id == rows[0].id],
            {schemas.WorkspaceGroup.name: name, schemas.WorkspaceGroup.status: "active"},
        )
        _invalidate_replicated_group_homes(rows[0].id)
        return 200, {"ok": True, "uid": uid}
    invite = "FED-" + secrets.token_hex(4).upper()
    created = DbManager.create_record(
        schemas.WorkspaceGroup(
            name=name,
            invite_code=invite,
            status="active",
            created_at=datetime.now(UTC).replace(tzinfo=None),
            uid=uid,
        )
    )
    if created is not None:
        _invalidate_replicated_group_homes(created.id)
    return 200, {"ok": True, "uid": uid}


def handle_group_invite(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    """Add a local or stub workspace to a group by uid + team_id."""
    err = _validate_fields(body, ["uid", "team_id"])
    if err:
        return 400, {"error": err}
    uid = str(body["uid"])
    team_id = str(body["team_id"])
    groups = DbManager.find_records(
        schemas.WorkspaceGroup,
        [schemas.WorkspaceGroup.uid == uid, schemas.WorkspaceGroup.status == "active"],
    )
    if not groups:
        return 404, {"error": "group_not_found"}
    group = groups[0]
    ws = _workspace_for_replicated_team(
        fed_ws,
        team_id,
        body.get("workspace_name") if isinstance(body.get("workspace_name"), str) else None,
        create_stub=False,
    )
    if not ws:
        return 404, {"error": "workspace_not_found"}
    existing = DbManager.find_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == group.id,
            schemas.WorkspaceGroupMember.workspace_id == ws.id,
            schemas.WorkspaceGroupMember.deleted_at.is_(None),
        ],
    )
    role = str(body.get("role") or "member").strip()
    if role == "creator":
        role = "member"
    if role not in ("owner", "member"):
        role = "member"
    if not existing:
        DbManager.create_record(
            schemas.WorkspaceGroupMember(
                group_id=group.id,
                workspace_id=ws.id,
                status="active",
                role=role,
                joined_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    else:
        DbManager.update_records(
            schemas.WorkspaceGroupMember,
            [schemas.WorkspaceGroupMember.id == existing[0].id],
            {
                schemas.WorkspaceGroupMember.status: "active",
                schemas.WorkspaceGroupMember.role: role,
                schemas.WorkspaceGroupMember.deleted_at: None,
            },
        )
    _invalidate_replicated_group_homes(group.id)
    return 200, {"ok": True}


def handle_group_leave(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    err = _validate_fields(body, ["uid", "team_id"])
    if err:
        return 400, {"error": err}
    uid = str(body["uid"])
    team_id = str(body["team_id"])
    groups = DbManager.find_records(
        schemas.WorkspaceGroup,
        [schemas.WorkspaceGroup.uid == uid],
    )
    if not groups:
        return 200, {"ok": True}
    ws = _workspace_for_replicated_team(fed_ws, team_id, create_stub=False)
    if not ws:
        return 200, {"ok": True}
    DbManager.update_records(
        schemas.WorkspaceGroupMember,
        [
            schemas.WorkspaceGroupMember.group_id == groups[0].id,
            schemas.WorkspaceGroupMember.workspace_id == ws.id,
        ],
        {
            schemas.WorkspaceGroupMember.status: "left",
            schemas.WorkspaceGroupMember.deleted_at: datetime.now(UTC).replace(tzinfo=None),
        },
    )
    _invalidate_replicated_group_homes(groups[0].id)
    return 200, {"ok": True}


def handle_sync_upsert(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    err = _validate_fields(body, ["uid", "group_uid", "title"])
    if err:
        return 400, {"error": err}
    sync_uid = str(body["uid"])
    group_uid = str(body["group_uid"])
    groups = DbManager.find_records(
        schemas.WorkspaceGroup,
        [schemas.WorkspaceGroup.uid == group_uid],
    )
    if not groups:
        return 404, {"error": "group_not_found"}
    group = groups[0]
    rows = DbManager.find_records(schemas.Sync, [schemas.Sync.uid == sync_uid])
    title = str(body["title"])[:100]
    if rows:
        DbManager.update_records(
            schemas.Sync,
            [schemas.Sync.id == rows[0].id],
            {schemas.Sync.title: title, schemas.Sync.group_id: group.id},
        )
    else:
        DbManager.create_record(
            schemas.Sync(
                title=title,
                description=(str(body.get("description") or ""))[:100] or None,
                group_id=group.id,
                sync_mode=str(body.get("sync_mode") or "group")[:20],
                uid=sync_uid,
            )
        )
    _invalidate_replicated_group_homes(group.id)
    return 200, {"ok": True, "uid": sync_uid}


def handle_sync_channel_upsert(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    err = _validate_fields(body, ["sync_uid", "team_id", "channel_id"])
    if err:
        return 400, {"error": err}
    syncs = DbManager.find_records(
        schemas.Sync,
        [schemas.Sync.uid == str(body["sync_uid"])],
    )
    if not syncs:
        return 404, {"error": "sync_not_found"}
    sync = syncs[0]
    ws = _workspace_for_replicated_team(
        fed_ws,
        str(body["team_id"]),
        create_stub=False,
    )
    if not ws:
        return 404, {"error": "workspace_not_found"}
    channel_id = str(body["channel_id"])
    existing = DbManager.find_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.sync_id == sync.id,
            schemas.SyncChannel.channel_id == channel_id,
            schemas.SyncChannel.workspace_id == ws.id,
        ],
    )
    publishes = bool(body.get("publishes", True))
    subscribes = bool(body.get("subscribes", True))
    status = str(body.get("status") or "active")[:20]
    reaction_style = body.get("reaction_style")
    channel_name = str(body.get("channel_name") or "").strip().removeprefix("#")[:100] or None
    if existing:
        prior = existing[0]
        was_inactive = (prior.status or "") != "active" or prior.deleted_at is not None
        updates = {
            schemas.SyncChannel.status: status,
            schemas.SyncChannel.publishes: publishes,
            schemas.SyncChannel.subscribes: subscribes,
            schemas.SyncChannel.deleted_at: None,
        }
        if reaction_style is not None:
            updates[schemas.SyncChannel.reaction_style] = str(reaction_style)[:32]
        if channel_name:
            updates[schemas.SyncChannel.channel_name] = channel_name
        DbManager.update_records(schemas.SyncChannel, [schemas.SyncChannel.id == existing[0].id], updates)
        if was_inactive and status == "active":
            try:
                notify_sibling_sync_channels(
                    ws,
                    sync.id,
                    f":arrow_forward: Syncing with `{resolve_workspace_name(ws)}` has been resumed.",
                )
            except Exception:
                log_warning(
                    "inbound_sync_channel_resume_notice_failed",
                    team_id=getattr(ws, "team_id", None),
                    sync_id=sync.id,
                )
    else:
        DbManager.create_record(
            schemas.SyncChannel(
                sync_id=sync.id,
                workspace_id=ws.id,
                channel_id=channel_id,
                channel_name=channel_name,
                status=status,
                publishes=publishes,
                subscribes=subscribes,
                reaction_style=str(reaction_style)[:32] if reaction_style else None,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    _invalidate_replicated_group_homes(sync.group_id, sync.id)
    return 200, {"ok": True}


def handle_sync_channel_remove(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    err = _validate_fields(body, ["sync_uid", "team_id", "channel_id"])
    if err:
        return 400, {"error": err}
    syncs = DbManager.find_records(
        schemas.Sync,
        [schemas.Sync.uid == str(body["sync_uid"])],
    )
    if not syncs:
        return 200, {"ok": True}
    ws = _workspace_for_replicated_team(fed_ws, str(body["team_id"]), create_stub=False)
    if not ws:
        return 200, {"ok": True}
    DbManager.update_records(
        schemas.SyncChannel,
        [
            schemas.SyncChannel.sync_id == syncs[0].id,
            schemas.SyncChannel.workspace_id == ws.id,
            schemas.SyncChannel.channel_id == str(body["channel_id"]),
        ],
        {
            schemas.SyncChannel.status: "left",
            schemas.SyncChannel.deleted_at: datetime.now(UTC).replace(tzinfo=None),
        },
    )
    _invalidate_replicated_group_homes(syncs[0].group_id, syncs[0].id)
    return 200, {"ok": True}


# ---------------------------------------------------------------------------
# Request dispatcher
# ---------------------------------------------------------------------------


def handle_file_offer(body: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    """Peer asks whether we already have sha256; return this process's file-part cap."""
    from federation.files import file_chunk_mb_response, offer_have

    err = _validate_fields(body, ["sha256"], extras=["size"])
    if err:
        return 400, {"error": err}
    sha256 = str(body["sha256"])
    size = int(body.get("size") or 0)
    return 200, {
        "have": offer_have(sha256, size),
        "file_chunk_mb": file_chunk_mb_response(),
        "json_chunk_mb": constants.json_chunk_mb(),
    }


def handle_file_part(raw_body: bytes, headers: dict, fed_ws: schemas.Instance) -> tuple[int, dict]:
    """Accept one raw file part into the federation_file_parts mailbox."""
    import hashlib as _hashlib

    from federation.files import upsert_file_part

    parsed = _require_signed_headers(headers, file_part=True)
    if isinstance(parsed, tuple):
        return parsed

    chunk_sha = _hashlib.sha256(raw_body).hexdigest()
    sign_body_str = f"POST:/api/federation/file:{parsed['sha256']}:{parsed['index']}:{parsed['total']}:{chunk_sha}"
    if not federation.federation_verify(sign_body_str, parsed["sig"], parsed["ts"], fed_ws.public_key):
        return _UNAUTHORIZED

    return upsert_file_part(
        sha256=parsed["sha256"],
        part_index=parsed["index"],
        total=parsed["total"],
        size=parsed["size"],
        payload=raw_body,
    )


def dispatch_federation_request(
    method: str,
    path: str,
    body_str: str,
    headers: dict,
    *,
    raw_body: bytes | None = None,
) -> tuple[int, dict]:
    """Route an incoming federation HTTP request to the appropriate handler.

    Returns ``(status_code, response_dict)``.

    Requests without the ``SyncBot-Federation`` User-Agent receive a plain
    404 identical to an unknown path.
    ``POST /file`` is raw bytes (pass *raw_body*); other POSTs are JSON.
    """
    from helpers._cache import begin_request_scope

    begin_request_scope()
    try:
        return _dispatch_federation_request(method, path, body_str, headers, raw_body=raw_body)
    finally:
        clear_request_file_pins()


def _dispatch_federation_request(
    method: str,
    path: str,
    body_str: str,
    headers: dict,
    *,
    raw_body: bytes | None = None,
) -> tuple[int, dict]:
    early = federation_preflight(method, path, headers)
    if early is not None:
        return early

    try:
        if not helpers.federation_enabled():
            return _NOT_FOUND
        federation.get_or_create_instance_keypair()
    except (OperationalError, ProgrammingError):
        return _NOT_READY

    base = constants.FEDERATION_API_BASE_PATH
    subpath = path[len(base) :] or "/"

    if subpath == "/ping" and method == "GET":
        return handle_ping(headers)

    if method != "POST":
        return _NOT_FOUND

    if subpath == "/file":
        parsed = _require_signed_headers(headers, file_part=True)
        if isinstance(parsed, tuple):
            return parsed
        peer = _trusted_peer_or_401(parsed["instance_id"])
        if isinstance(peer, tuple):
            return peer
        payload = raw_body if raw_body is not None else (body_str.encode("latin-1") if body_str else b"")
        return handle_file_part(payload, headers, peer)

    body_bytes = body_str.encode() if isinstance(body_str, str) else (body_str or b"")
    json_cap = constants.federation_json_max_bytes()
    if json_cap is not None and len(body_bytes) > json_cap:
        return 413, {"error": "payload_too_large"}

    if subpath == "/pair":
        try:
            body = json.loads(body_str) if body_str else {}
        except json.JSONDecodeError:
            return 400, {"error": "invalid_json"}
        return handle_pair(body, body_str, headers)

    verified = _verify_known_peer(body_str, headers)
    if isinstance(verified, tuple):
        return verified

    try:
        body = json.loads(body_str) if body_str else {}
    except json.JSONDecodeError:
        return 400, {"error": "invalid_json"}

    if subpath == "/file/offer":
        return handle_file_offer(body, verified)
    if subpath == "/message":
        return handle_message(body, verified)
    if subpath == "/message/edit":
        return handle_message_edit(body, verified)
    if subpath == "/message/delete":
        return handle_message_delete(body, verified)
    if subpath == "/message/react":
        return handle_message_react(body, verified)
    if subpath == "/users":
        return handle_users(body, verified)
    if subpath == "/teams":
        return handle_teams(body, verified)
    if subpath == "/group-upsert":
        return handle_group_upsert(body, verified)
    if subpath == "/group-invite":
        return handle_group_invite(body, verified)
    if subpath == "/group-leave":
        return handle_group_leave(body, verified)
    if subpath == "/sync-upsert":
        return handle_sync_upsert(body, verified)
    if subpath == "/sync-channel-upsert":
        return handle_sync_channel_upsert(body, verified)
    if subpath == "/sync-channel-remove":
        return handle_sync_channel_remove(body, verified)

    return _NOT_FOUND
