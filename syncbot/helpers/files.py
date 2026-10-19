"""File upload/download helpers for message sync.

Hashed copies live at ``/tmp/sb-file-{sha256}``. Slack ``file_id`` only skips a
second download of the same Slack object on this warm container. Federation
integrity and peer reuse use content SHA-256 + size.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import time as _time
from collections import OrderedDict
from collections.abc import Callable
from logging import Logger

import requests
from slack_sdk import WebClient

from helpers.user_action_echo import slack_message_ts
from logger import log_debug, log_error, log_warning

# Slack documents 1 GB per file on every plan, including Free.
_MAX_FILE_BYTES = 1024 * 1024 * 1024
# Stay inside the 120s function timeout (download then upload v2).
_TRANSFER_TIMEOUT = 90
_STREAM_CHUNK = 8192
# conversations.history and conversations.replies are Tier 3: 50+ per minute.
# The lookup keeps going until Slack returns the share ts. The platform
# request timeout is what ends it.
_TIER3_PER_MINUTE = 50
_SHARE_LOOKUP_INTERVAL_S = 60 / _TIER3_PER_MINUTE

_HASHED_PREFIX = "/tmp/sb-file-"
_LAMBDA_LRU_BYTES = 200 * 1024 * 1024
_DEFAULT_LRU_BYTES = 1024 * 1024 * 1024

# Warm-container sidecar: Slack file_id → (sha256, size). Not durable.
_slack_file_index: dict[str, tuple[str, int]] = {}
# LRU of sha256 → size (bytes on disk). Never evict paths in _pinned.
_lru: OrderedDict[str, int] = OrderedDict()
_lru_bytes = 0
_pinned: set[str] = set()
_cache_lock = threading.Lock()


def max_file_bytes() -> int:
    return _MAX_FILE_BYTES


def hashed_file_path(sha256: str) -> str:
    return f"{_HASHED_PREFIX}{sha256}"


def _lru_budget() -> int:
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return _LAMBDA_LRU_BYTES
    return _DEFAULT_LRU_BYTES


def pin_hashed_file(sha256: str) -> None:
    """Keep ``sb-file-{sha256}`` from LRU eviction for the rest of this request."""
    if not sha256:
        return
    with _cache_lock:
        _pinned.add(sha256)


def unpin_hashed_file(sha256: str) -> None:
    if not sha256:
        return
    with _cache_lock:
        _pinned.discard(sha256)


def clear_request_file_pins() -> None:
    with _cache_lock:
        _pinned.clear()


def remember_slack_file_id(slack_file_id: str | None, sha256: str, size: int) -> None:
    if not slack_file_id or not sha256:
        return
    with _cache_lock:
        _slack_file_index[str(slack_file_id)] = (sha256, int(size))


def lookup_slack_file_id(slack_file_id: str | None) -> tuple[str, int] | None:
    if not slack_file_id:
        return None
    with _cache_lock:
        return _slack_file_index.get(str(slack_file_id))


def _touch_lru(sha256: str, size: int) -> None:
    global _lru_bytes
    if sha256 in _lru:
        _lru_bytes -= _lru[sha256]
        del _lru[sha256]
    _lru[sha256] = size
    _lru.move_to_end(sha256)
    _lru_bytes += size
    budget = _lru_budget()
    while _lru_bytes > budget and _lru:
        victim, victim_size = next(iter(_lru.items()))
        if victim in _pinned:
            _lru.move_to_end(victim)
            # All remaining entries pinned (or only pinned left) — stop.
            if all(k in _pinned for k in _lru):
                break
            continue
        del _lru[victim]
        _lru_bytes -= victim_size
        path = hashed_file_path(victim)
        with contextlib.suppress(OSError):
            os.remove(path)


def register_hashed_file(sha256: str, size: int) -> None:
    """Record a hashed file in the LRU (after a successful write or assemble)."""
    if not sha256:
        return
    with _cache_lock:
        _touch_lru(sha256, int(size))


def hashed_file_ready(sha256: str, size: int | None = None) -> bool:
    """True when ``/tmp/sb-file-{sha256}`` exists and optionally matches *size*."""
    if not sha256:
        return False
    path = hashed_file_path(sha256)
    try:
        st = os.stat(path)
    except OSError:
        return False
    if size is not None and st.st_size != int(size):
        return False
    with _cache_lock:
        _touch_lru(sha256, st.st_size)
    return True


def is_hashed_cache_path(path: str | None) -> bool:
    return bool(path) and str(path).startswith(_HASHED_PREFIX)


def cleanup_temp_files(photos: list[dict] | None, direct_files: list[dict] | None) -> None:
    """Remove non-hashed temporary files created during message sync.

    Hashed ``sb-file-{sha256}`` copies are kept for LRU reuse.
    """
    for item in photos or []:
        path = item.get("path")
        if path and not is_hashed_cache_path(path):
            with contextlib.suppress(OSError):
                os.remove(path)
    for item in direct_files or []:
        path = item.get("path")
        if path and not is_hashed_cache_path(path):
            with contextlib.suppress(OSError):
                os.remove(path)
    clear_request_file_pins()


def _safe_file_parts(f: dict) -> tuple[str, str, str]:
    """Return ``(safe_id, safe_ext, default_name)`` with path-safe characters only."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", f.get("id", "file"))
    safe_ext = re.sub(r"[^a-zA-Z0-9]", "", f.get("filetype", "bin"))
    return safe_id, safe_ext, f"{safe_id}.{safe_ext}"


def _download_and_hash(url: str, headers: dict | None = None) -> tuple[str, int, str]:
    """Stream *url* to a temp file while hashing; rename into ``sb-file-{sha256}``.

    Returns ``(sha256, size, path)``. Removes partials on failure.
    """
    digest = hashlib.sha256()
    written = 0
    fd, temp_path = tempfile.mkstemp(prefix="sb-dl-", dir="/tmp")
    os.close(fd)
    try:
        with requests.get(url, headers=headers, timeout=_TRANSFER_TIMEOUT, stream=True) as r:
            r.raise_for_status()
            with open(temp_path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=_STREAM_CHUNK):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > _MAX_FILE_BYTES:
                        raise ValueError(f"File exceeds {_MAX_FILE_BYTES} byte limit")
                    digest.update(chunk)
                    fh.write(chunk)
        sha256 = digest.hexdigest()
        final_path = hashed_file_path(sha256)
        if os.path.isfile(final_path):
            with contextlib.suppress(OSError):
                os.remove(temp_path)
            if os.path.getsize(final_path) != written:
                raise ValueError("cached file size mismatch")
        else:
            os.replace(temp_path, final_path)
        register_hashed_file(sha256, written)
        pin_hashed_file(sha256)
        return sha256, written, final_path
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(temp_path)
        raise


def write_bytes_hashed(data: bytes, *, expected_sha256: str | None = None) -> tuple[str, int, str]:
    """Write *data* into the hashed cache. Returns ``(sha256, size, path)``."""
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError(f"File exceeds {_MAX_FILE_BYTES} byte limit")
    sha256 = hashlib.sha256(data).hexdigest()
    if expected_sha256 and sha256 != expected_sha256:
        raise ValueError("sha256 mismatch")
    path = hashed_file_path(sha256)
    if not (os.path.isfile(path) and os.path.getsize(path) == len(data)):
        fd, temp_path = tempfile.mkstemp(prefix="sb-asm-", dir="/tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(temp_path, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(temp_path)
            raise
    register_hashed_file(sha256, len(data))
    pin_hashed_file(sha256)
    return sha256, len(data), path


def assemble_parts_to_hashed(
    parts,
    *,
    expected_sha256: str,
    expected_size: int,
) -> str:
    """Stream *parts* to disk, verify sha256+size, write ``sb-file-{sha256}``. Return path.

    *parts* is an iterable of byte chunks. Callers should yield one part at a
    time so the whole file is not held in RAM.
    """
    digest = hashlib.sha256()
    written = 0
    fd, temp_path = tempfile.mkstemp(prefix="sb-asm-", dir="/tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            for chunk in parts:
                if not chunk:
                    continue
                written += len(chunk)
                if written > _MAX_FILE_BYTES:
                    raise ValueError(f"File exceeds {_MAX_FILE_BYTES} byte limit")
                digest.update(chunk)
                fh.write(chunk)
        sha256 = digest.hexdigest()
        if sha256 != expected_sha256:
            raise ValueError("sha256 mismatch after assemble")
        if written != int(expected_size):
            raise ValueError("size mismatch after assemble")
        final_path = hashed_file_path(sha256)
        os.replace(temp_path, final_path)
        register_hashed_file(sha256, written)
        pin_hashed_file(sha256)
        return final_path
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(temp_path)
        raise


_SKIP_FILE_MODES = frozenset({"file_access", "tombstone", "hidden_by_limit"})


def hosted_file_fetch_url(file: dict | None) -> str | None:
    """Private download URL for a hosted Slack file, or None for stubs."""
    if not isinstance(file, dict) or file.get("mode") in _SKIP_FILE_MODES:
        return None
    url = file.get("url_private_download") or file.get("url_private") or ""
    url = str(url).strip()
    return url or None


def event_has_downloadable_hosted_file(event: dict | None) -> bool:
    """True when any event file has a private download URL."""
    if not isinstance(event, dict):
        return False
    return any(hosted_file_fetch_url(item if isinstance(item, dict) else None) for item in event.get("files") or [])


def download_slack_files(files: list[dict], client: WebClient, logger: Logger) -> list[dict]:
    """Download files from Slack into the hashed ``/tmp`` cache.

    Skips stubs that have no private URL (tombstones, access-restricted, and
    external files without ``url_private``). Prefers ``url_private_download``.
    Keeps Slack's original ``name`` so ``files_upload_v2`` can infer type from
    the extension. Reuses a warm-cache hit for the same Slack ``file_id`` when
    the hashed file is still present.
    """
    downloaded: list[dict] = []
    auth_headers = {"Authorization": f"Bearer {client.token}"}

    for f in files:
        try:
            url = hosted_file_fetch_url(f)
            if not url:
                continue

            safe_id, _safe_ext, default_name = _safe_file_parts(f)
            file_name = f.get("name") or default_name
            slack_file_id = str(f.get("id") or safe_id)
            cached = lookup_slack_file_id(slack_file_id)
            if cached:
                sha256, size = cached
                if hashed_file_ready(sha256, size):
                    pin_hashed_file(sha256)
                    downloaded.append(
                        {
                            "sha256": sha256,
                            "name": file_name,
                            "mimetype": f.get("mimetype", "application/octet-stream"),
                            "size": size,
                            "path": hashed_file_path(sha256),
                            "slack_file_id": slack_file_id,
                        }
                    )
                    continue

            sha256, size, path = _download_and_hash(url, headers=auth_headers)
            remember_slack_file_id(slack_file_id, sha256, size)
            downloaded.append(
                {
                    "sha256": sha256,
                    "name": file_name,
                    "mimetype": f.get("mimetype", "application/octet-stream"),
                    "size": size,
                    "path": path,
                    "slack_file_id": slack_file_id,
                }
            )
        except Exception as e:
            log_error("file_share_failed", reason="download_failed", file_id=f.get("id"), error=str(e))
            log_error("download_slack_files", value=f.get("id"), error=str(e))
    return downloaded


def upload_files_to_slack(
    bot_token: str,
    channel_id: str,
    files: list[dict],
    initial_comment: str | None = None,
    blocks: list[dict] | None = None,
    username: str | None = None,
    icon_url: str | None = None,
    thread_ts: str | None = None,
    reply_broadcast: bool = False,
    after_upload: Callable | None = None,
    after_share_ts: Callable | None = None,
) -> tuple[dict | None, str | None]:
    """Upload one or more local files directly to a Slack channel.

    Streams each file from disk (does not load the whole file into RAM).
    *after_upload* receives file ids before ``files.completeUploadExternal``
    shares them into the Channel. *after_share_ts* runs once the share
    message ts is known, before ``chat.update``.

    ``files.completeUploadExternal`` does not support ``reply_broadcast``.
    When broadcasting is requested for a thread upload, complete first, then
    ``chat.update`` the share with ``reply_broadcast=True``.

    ``blocks`` and ``initial_comment`` cannot both be sent: Slack ignores
    blocks when the comment is set. ``username`` and ``icon_url`` need
    ``chat:write.customize``.
    """
    if not files:
        return None, None

    slack_client = WebClient(bot_token)
    try:
        prepared: list[dict] = []
        for item in files:
            path = item["path"]
            filename = item["name"]
            length = int(item.get("size") or os.path.getsize(path))
            if length > _MAX_FILE_BYTES:
                raise ValueError(f"File exceeds {_MAX_FILE_BYTES} byte limit")
            sha256 = item.get("sha256")
            if sha256:
                pin_hashed_file(str(sha256))
            url_response = slack_client.files_getUploadURLExternal(filename=filename, length=length)
            file_id = url_response.get("file_id")
            upload_url = url_response.get("upload_url")
            if not file_id or not upload_url:
                raise RuntimeError("files.getUploadURLExternal did not return file_id and upload_url")
            prepared.append(
                {
                    "file_id": str(file_id),
                    "upload_url": upload_url,
                    "path": path,
                    "title": filename,
                    "length": length,
                }
            )

        if after_upload:
            after_upload([item["file_id"] for item in prepared])

        for item in prepared:
            with open(item["path"], "rb") as handle:
                put = requests.post(item["upload_url"], data=handle, timeout=_TRANSFER_TIMEOUT)
            if put.status_code != 200:
                raise RuntimeError(f"file upload POST returned {put.status_code}")

        complete_kwargs: dict = {
            "files": [{"id": item["file_id"], "title": item["title"]} for item in prepared],
            "channel_id": channel_id,
        }
        if blocks:
            complete_kwargs["blocks"] = json.dumps(blocks)
        elif initial_comment:
            complete_kwargs["initial_comment"] = initial_comment
        if username:
            complete_kwargs["username"] = username
        if icon_url:
            complete_kwargs["icon_url"] = icon_url
        if thread_ts:
            complete_kwargs["thread_ts"] = thread_ts
        res = slack_client.files_completeUploadExternal(**complete_kwargs)

        msg_ts = _extract_file_message_ts(slack_client, res, channel_id, thread_ts=thread_ts)
        if after_share_ts and msg_ts:
            after_share_ts(msg_ts)
        if reply_broadcast and thread_ts and msg_ts:
            _broadcast_thread_file_share(slack_client, channel_id, msg_ts)
        return res, msg_ts
    except Exception as e:
        log_error("file_share_failed", reason="upload_failed", channel_id=channel_id, error=str(e))
        raise


def _broadcast_thread_file_share(client: WebClient, channel_id: str, message_ts: str) -> None:
    """Also-send an existing thread file share to the channel via chat.update."""
    try:
        # Omit text/blocks so Slack only flips reply_broadcast (no content rewrite).
        client.chat_update(channel=channel_id, ts=message_ts, reply_broadcast=True)
    except Exception as e:
        log_warning("file_share_broadcast_failed", channel_id=channel_id, ts=message_ts, error=str(e))


def _share_ts_from_file_payload(
    file_obj: dict | None,
    channel_id: str,
    thread_ts: str | None = None,
) -> str | None:
    """Return the share *message* ts from a file object's ``shares`` map.

    A file uploaded into a thread is a new message: ``thread_ts`` is the
    parent and ``ts`` is the share. Slack may also list the parent
    (``ts == thread_ts``). That parent ts must not be used for PostMeta or
    user-token echo — the inbound ``file_share`` has the new ts, would miss
    both guards, and re-sync in a loop. Bot-token uploads are skipped via
    ``bot_id``; user-token uploads are not.
    """
    if not isinstance(file_obj, dict):
        return None
    shares = file_obj.get("shares") or {}
    if not isinstance(shares, dict):
        return None
    parent = str(thread_ts) if thread_ts else None
    for share_type in ("public", "private"):
        channel_shares = (shares.get(share_type) or {}).get(channel_id, [])
        if not channel_shares:
            continue
        if parent:
            for share in channel_shares:
                ts = share.get("ts")
                if not ts:
                    continue
                ts_s = str(ts)
                if ts_s == parent:
                    continue
                if str(share.get("thread_ts") or "") == parent:
                    return ts_s
            continue
        ts = channel_shares[0].get("ts")
        if ts:
            return str(ts)
    return None


def _file_id_from_upload_response(upload_response) -> str | None:
    """Best-effort file id from a complete-upload response."""
    if not upload_response:
        return None
    with contextlib.suppress(KeyError, TypeError, IndexError, AttributeError):
        file_id = upload_response["file"]["id"]
        if file_id:
            return str(file_id)
    with contextlib.suppress(KeyError, TypeError, IndexError, AttributeError):
        data = getattr(upload_response, "data", None) or upload_response
        file_id = data["file"]["id"]
        if file_id:
            return str(file_id)
    try:
        data = getattr(upload_response, "data", None) or upload_response
        files_list = data["files"]
        if files_list and len(files_list) > 0:
            first = files_list[0]
            file_id = first["id"] if isinstance(first, dict) else first.get("id")
            if file_id:
                return str(file_id)
    except (KeyError, TypeError, IndexError, AttributeError):
        pass
    return None


def file_ids_from_message_event(body: dict) -> list[str]:
    """File ids on a Slack message event, including ``message.files`` on edits."""
    event = body.get("event") or {}
    files = event.get("files") or event.get("message", {}).get("files") or []
    ids: list[str] = []
    for item in files:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
    return ids


def event_is_new_file_share(event: dict | None) -> bool:
    """True when this event is a Slack ``file_share`` (a file was shared).

    ``upload`` is share-at-upload-time vs later, not new vs parent copy.
    Upload v2 always shares later, so real shares arrive ``upload: false``.
    """
    return isinstance(event, dict) and event.get("subtype") == "file_share"


def event_keeps_hosted_files(event: dict | None, *, is_reply: bool, text: str) -> bool:
    """Whether this event's hosted files should be downloaded and synced.

    Keep ``file_share``, top-level messages, also-send-to-channel replies
    (``thread_broadcast`` / ``reply_broadcast``), and file-only thread
    messages (blank text; Slack sometimes omits the subtype). Strip only a
    text thread reply that is not ``file_share`` (parent file listing).
    """
    if not isinstance(event, dict) or not event.get("files"):
        return False
    if event_is_new_file_share(event) or not is_reply:
        return True
    subtype = event.get("subtype")
    if subtype in {"thread_broadcast", "reply_broadcast"} or event.get("reply_broadcast") is True:
        return True
    return not (text or "").strip()


def _extract_file_message_ts(
    client: WebClient,
    upload_response,
    channel_id: str,
    thread_ts: str | None = None,
) -> str | None:
    """Extract the message ts created by a file upload.

    Prefer ``shares`` on the upload response when that ts is the new share,
    not the thread parent. Otherwise keep reading history and ``files.info``
    at Slack's Tier 3 pace until the share ts is present. There is no
    app-side deadline; the platform request timeout ends the lookup.
    """
    if not upload_response:
        return None

    data = getattr(upload_response, "data", None) or upload_response
    with contextlib.suppress(KeyError, TypeError, IndexError, AttributeError):
        ts = _share_ts_from_file_payload(data.get("file"), channel_id, thread_ts=thread_ts)
        if ts:
            log_debug("file_share_ts", channel_id=channel_id, ts=ts, source="upload_shares", thread_ts=thread_ts)
            return ts
    with contextlib.suppress(KeyError, TypeError, IndexError, AttributeError):
        files_list = data.get("files") or []
        if files_list:
            first = files_list[0] if isinstance(files_list[0], dict) else None
            ts = _share_ts_from_file_payload(first, channel_id, thread_ts=thread_ts)
            if ts:
                log_debug("file_share_ts", channel_id=channel_id, ts=ts, source="upload_shares", thread_ts=thread_ts)
                return ts

    file_id = _file_id_from_upload_response(upload_response)
    if not file_id:
        log_warning("file_share_ts_missing_file_id")
        return None

    # completeUploadExternal often returns before Slack posts the share.
    attempt = 0
    while True:
        if attempt:
            _time.sleep(_SHARE_LOOKUP_INTERVAL_S)
        ts = _share_ts_from_channel_history(client, channel_id, file_id, thread_ts=thread_ts)
        if ts:
            log_debug(
                "file_share_ts",
                channel_id=channel_id,
                ts=ts,
                source="history",
                thread_ts=thread_ts,
                attempt=attempt,
            )
            return ts
        try:
            info_resp = client.files_info(file=file_id)
            payload = getattr(info_resp, "data", None) or info_resp
            file_obj = payload.get("file") if isinstance(payload, dict) else None
            if file_obj is None:
                file_obj = info_resp.get("file")
            ts = _share_ts_from_file_payload(file_obj, channel_id, thread_ts=thread_ts)
            if ts:
                log_debug(
                    "file_share_ts",
                    channel_id=channel_id,
                    ts=ts,
                    source="files_info",
                    thread_ts=thread_ts,
                    attempt=attempt,
                )
                return ts
        except Exception as e:
            log_warning("file_share_ts_files_info_failed", attempt=attempt, error=str(e))
        attempt += 1


def _share_ts_from_channel_history(
    client: WebClient,
    channel_id: str,
    file_id: str,
    thread_ts: str | None = None,
) -> str | None:
    """Find the share message ts by looking up *file_id* in recent channel messages."""
    parent = str(thread_ts) if thread_ts else None
    cursor = None
    seen_cursors: set[str] = set()
    # Replies are oldest-first. Follow next_cursor until Slack ends the thread.
    # Channel history is newest-first; one page is the recent messages.
    # Slack recommends no more than 200 objects per page.
    while True:
        try:
            if parent:
                kwargs: dict = {"channel": channel_id, "ts": parent, "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                res = client.conversations_replies(**kwargs)
            else:
                res = client.conversations_history(channel=channel_id, limit=200)
        except Exception as exc:
            log_debug("file_share_ts", channel_id=channel_id, ts=None, source="history_miss", error=str(exc))
            return None
        messages = res.get("messages") if hasattr(res, "get") else None
        if messages is None:
            data = getattr(res, "data", None)
            messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return None
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            files = msg.get("files") or []
            ids = {str(item["id"]) for item in files if isinstance(item, dict) and item.get("id")}
            if file_id not in ids:
                continue
            ts = msg.get("ts")
            if not ts:
                continue
            ts_s = str(ts)
            if parent and slack_message_ts(ts_s) == slack_message_ts(parent):
                continue
            return ts_s
        if not parent:
            return None
        meta = res.get("response_metadata") if hasattr(res, "get") else None
        cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
        if not cursor or cursor in seen_cursors:
            return None
        seen_cursors.add(cursor)
