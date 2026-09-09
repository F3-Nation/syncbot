"""Source-canonical sync envelopes (message and reaction)."""

from __future__ import annotations

from typing import Any

KIND_MESSAGE = "message"
KIND_REACTION = "reaction"

ACTION_CREATE = "create"
ACTION_EDIT = "edit"
ACTION_DELETE = "delete"
ACTION_ADD = "add"
ACTION_REMOVE = "remove"

_FOLLOW_UP_ACTIONS = frozenset({ACTION_EDIT, ACTION_DELETE, ACTION_ADD, ACTION_REMOVE})


def post_id_for_post_records(envelope: dict[str, Any]) -> str | None:
    """PostMeta ``post_id`` used to look up ``get_post_records`` for this envelope.

    Thread replies and files in a thread use ``thread_post_id``. Edits,
    deletes, and reactions use ``post_id`` of that message. A new top-level
    create has neither and fans out to every publish target.
    """
    thread = envelope.get("thread_post_id")
    if thread:
        return str(thread)
    if envelope.get("action") in _FOLLOW_UP_ACTIONS:
        pid = envelope.get("post_id")
        return str(pid) if pid else None
    return None


def build_envelope(
    *,
    kind: str,
    action: str,
    post_id: str,
    source_channel_id: str,
    source_workspace_id: int | None,
    source_team_id: str | None = None,
    source_sync_channel_id: int | None = None,
    people: list[dict[str, Any]] | None = None,
    text: str | None = None,
    blocks: list[dict] | None = None,
    file_refs: list[dict] | None = None,
    images: list[dict] | None = None,
    thread_post_id: str | None = None,
    reply_broadcast: bool = False,
    reaction: str | None = None,
    source_user_id: str | None = None,
    user_name: str | None = None,
    user_avatar_url: str | None = None,
    workspace_name: str | None = None,
    source_ts: str | None = None,
) -> dict[str, Any]:
    """Build one envelope dict. Omit unused keys rather than empty stubs.

    ``kind`` is only ``message`` or ``reaction``. ``action`` is kind-specific:
    message → create/edit/delete; reaction → add/remove.
    """
    envelope: dict[str, Any] = {
        "kind": kind,
        "action": action,
        "post_id": post_id,
        "source_channel_id": source_channel_id,
    }
    if source_workspace_id is not None:
        envelope["source_workspace_id"] = source_workspace_id
    if source_team_id:
        envelope["source_team_id"] = source_team_id
    if source_sync_channel_id is not None:
        envelope["source_sync_channel_id"] = source_sync_channel_id
    if people:
        envelope["people"] = people
    if source_user_id:
        envelope["source_user_id"] = source_user_id
    if user_name:
        envelope["user_name"] = user_name
    if user_avatar_url:
        envelope["user_avatar_url"] = user_avatar_url
    if workspace_name:
        envelope["workspace_name"] = workspace_name
    if source_ts:
        envelope["source_ts"] = source_ts

    if kind == KIND_MESSAGE:
        if action in (ACTION_CREATE, ACTION_EDIT):
            if text is not None:
                envelope["text"] = text
            if blocks:
                envelope["blocks"] = blocks
            if file_refs:
                envelope["file_refs"] = file_refs
            if images:
                envelope["images"] = images
            if thread_post_id:
                envelope["thread_post_id"] = thread_post_id
            if reply_broadcast:
                envelope["reply_broadcast"] = True
        # delete: post_id (+ shared fields) is enough
    elif kind == KIND_REACTION and reaction:
        envelope["reaction"] = reaction

    return envelope


def people_entry(
    user_id: str,
    *,
    name: str | None = None,
    email: str | None = None,
    avatar_url: str | None = None,
) -> dict[str, Any]:
    """One source-canonical person directory row."""
    entry: dict[str, Any] = {"user_id": user_id}
    if name:
        entry["name"] = name
    if email:
        entry["email"] = email
    if avatar_url:
        entry["avatar_url"] = avatar_url
    return entry
