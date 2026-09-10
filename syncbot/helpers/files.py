"""File upload/download helpers for message sync."""

import contextlib
import logging
import os
import re
import time as _time
from logging import Logger

import requests
from slack_sdk import WebClient

_logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = 30  # seconds
_MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MB
_STREAM_CHUNK = 8192


def cleanup_temp_files(photos: list[dict] | None, direct_files: list[dict] | None) -> None:
    """Remove temporary files created during message sync."""
    for item in photos or []:
        path = item.get("path")
        if path:
            with contextlib.suppress(OSError):
                os.remove(path)
    for item in direct_files or []:
        path = item.get("path")
        if path:
            with contextlib.suppress(OSError):
                os.remove(path)


def _safe_file_parts(f: dict) -> tuple[str, str, str]:
    """Return ``(safe_id, safe_ext, default_name)`` with path-safe characters only."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", f.get("id", "file"))
    safe_ext = re.sub(r"[^a-zA-Z0-9]", "", f.get("filetype", "bin"))
    return safe_id, safe_ext, f"{safe_id}.{safe_ext}"


def _download_to_file(url: str, file_path: str, headers: dict | None = None) -> None:
    """Stream a URL to disk, aborting if the response exceeds *_MAX_FILE_BYTES*.

    Removes the partial file on any failure so /tmp doesn't fill up.
    """
    try:
        with requests.get(url, headers=headers, timeout=_DOWNLOAD_TIMEOUT, stream=True) as r:
            r.raise_for_status()
            written = 0
            with open(file_path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=_STREAM_CHUNK):
                    written += len(chunk)
                    if written > _MAX_FILE_BYTES:
                        raise ValueError(f"File exceeds {_MAX_FILE_BYTES} byte limit")
                    fh.write(chunk)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(file_path)
        raise


def download_slack_files(files: list[dict], client: WebClient, logger: Logger) -> list[dict]:
    """Download files from Slack to /tmp for direct re-upload.

    Skips stubs that have no private URL (tombstones, access-restricted, and
    external files without ``url_private``). Keeps Slack's original ``name`` so
    ``files_upload_v2`` can infer type from the extension.
    """
    downloaded: list[dict] = []
    auth_headers = {"Authorization": f"Bearer {client.token}"}
    skip_modes = frozenset({"file_access", "tombstone", "hidden_by_limit"})

    for f in files:
        try:
            url = f.get("url_private")
            mode = f.get("mode")
            if not url:
                continue
            if mode in skip_modes:
                continue

            safe_id, safe_ext, default_name = _safe_file_parts(f)
            file_name = f.get("name") or default_name
            file_path = f"/tmp/{safe_id}.{safe_ext}"

            _download_to_file(url, file_path, headers=auth_headers)

            downloaded.append(
                {
                    "path": file_path,
                    "name": file_name,
                    "mimetype": f.get("mimetype", "application/octet-stream"),
                }
            )
        except Exception as e:
            logger.error(f"download_slack_files: failed for {f.get('id')}: {e}")
    return downloaded


def upload_files_to_slack(
    bot_token: str,
    channel_id: str,
    files: list[dict],
    initial_comment: str | None = None,
    thread_ts: str | None = None,
    reply_broadcast: bool = False,
) -> tuple[dict | None, str | None]:
    """Upload one or more local files directly to a Slack channel.

    ``files.completeUploadExternal`` / ``files_upload_v2`` do not support
    ``reply_broadcast``. When broadcasting is requested for a thread upload,
    we upload first, then ``chat.update`` the share message with
    ``reply_broadcast=True`` (Slack's supported path for also-send-to-channel).
    """
    if not files:
        return None, None

    slack_client = WebClient(bot_token)
    file_uploads = []
    for f in files:
        file_uploads.append(
            {
                "file": f["path"],
                "filename": f["name"],
            }
        )

    kwargs: dict = {"channel": channel_id}
    if initial_comment:
        kwargs["initial_comment"] = initial_comment
    if thread_ts:
        kwargs["thread_ts"] = thread_ts

    try:
        if len(file_uploads) == 1:
            kwargs["file"] = file_uploads[0]["file"]
            kwargs["filename"] = file_uploads[0]["filename"]
            res = slack_client.files_upload_v2(**kwargs)
        else:
            kwargs["file_uploads"] = file_uploads
            res = slack_client.files_upload_v2(**kwargs)

        msg_ts = _extract_file_message_ts(slack_client, res, channel_id, thread_ts=thread_ts)
        if reply_broadcast and thread_ts and msg_ts:
            _broadcast_thread_file_share(slack_client, channel_id, msg_ts)
        return res, msg_ts
    except Exception as e:
        _logger.warning(f"upload_files_to_slack: failed for channel {channel_id}: {e}")
        return None, None


def _broadcast_thread_file_share(client: WebClient, channel_id: str, message_ts: str) -> None:
    """Also-send an existing thread file share to the channel via chat.update."""
    try:
        # Omit text/blocks so Slack only flips reply_broadcast (no content rewrite).
        client.chat_update(channel=channel_id, ts=message_ts, reply_broadcast=True)
    except Exception as e:
        _logger.warning(
            "upload_files_to_slack: reply_broadcast via chat.update failed",
            extra={"channel_id": channel_id, "ts": message_ts, "error": str(e)},
        )


def _extract_file_message_ts(
    client: WebClient,
    upload_response,
    channel_id: str,
    thread_ts: str | None = None,
) -> str | None:
    """Extract the message ts created by a file upload."""
    if not upload_response:
        return None

    file_id = None
    with contextlib.suppress(KeyError, TypeError, IndexError):
        file_id = upload_response["file"]["id"]

    if not file_id:
        try:
            files_list = upload_response["files"]
            if files_list and len(files_list) > 0:
                file_id = files_list[0]["id"] if isinstance(files_list[0], dict) else files_list[0].get("id")
        except (KeyError, TypeError, IndexError):
            pass

    if not file_id:
        _logger.warning("_extract_file_message_ts: could not find file_id in upload response")
        return None

    for attempt in range(4):
        try:
            info_resp = client.files_info(file=file_id)
            shares = info_resp["file"]["shares"]
            for share_type in ("public", "private"):
                channel_shares = shares.get(share_type, {}).get(channel_id, [])
                if not channel_shares:
                    continue
                if thread_ts:
                    for share in channel_shares:
                        if str(share.get("ts") or "") == str(thread_ts) or str(share.get("thread_ts") or "") == str(
                            thread_ts
                        ):
                            ts = share.get("ts")
                            if ts:
                                _logger.info(
                                    "_extract_file_message_ts: success",
                                    extra={"file_id": file_id, "ts": ts, "attempt": attempt},
                                )
                                return ts
                ts = channel_shares[0].get("ts")
                if ts:
                    _logger.info(
                        "_extract_file_message_ts: success",
                        extra={"file_id": file_id, "ts": ts, "attempt": attempt},
                    )
                    return ts
        except (KeyError, TypeError, IndexError):
            pass
        except Exception as e:
            _logger.warning(f"_extract_file_message_ts: files.info error (attempt {attempt}): {e}")

        if attempt < 3:
            _time.sleep(1.5)

    _logger.warning(f"_extract_file_message_ts: could not resolve ts for file {file_id} after retries")
    return None
