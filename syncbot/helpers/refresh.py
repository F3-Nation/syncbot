"""Shared helpers for Refresh-button flows (Home tab and User Mapping).

Provides a single place for cooldown message text, block injection,
and the hash/cache/cooldown check so both handlers stay DRY.
"""

import time
from typing import Literal

import constants
from helpers._cache import _cache_get, _cache_set

_REFRESH_COOLDOWN_SECONDS = getattr(constants, "REFRESH_COOLDOWN_SECONDS", 60)


def cooldown_message_block(remaining_seconds: int) -> dict:
    """Return a Block Kit context block dict for the refresh cooldown message."""
    text = (
        f"No new data. Wait {remaining_seconds} second{'s' if remaining_seconds != 1 else ''} before refreshing again."
    )
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": text}],
    }


def index_of_block_with_action(block_dicts: list, action_id: str) -> int:
    """Return the index of the first block that contains an element with the given action_id.

    Used to find the Refresh button block so the cooldown message can be inserted after it.
    Returns len(block_dicts) - 1 if not found (inject at end).
    """
    for i, block in enumerate(block_dicts):
        if block.get("type") == "actions":
            for elt in block.get("elements") or []:
                if elt.get("action_id") == action_id:
                    return i
    return max(0, len(block_dicts) - 1)


def inject_cooldown_message(
    cached_blocks: list,
    after_block_index: int,
    remaining_seconds: int,
) -> list:
    """Insert the cooldown message block after the given block index. Does not mutate cached_blocks."""
    msg_block = cooldown_message_block(remaining_seconds)
    i = after_block_index + 1
    return cached_blocks[:i] + [msg_block] + cached_blocks[i:]


def refresh_cooldown_check(
    current_hash: str,
    hash_key: str,
    blocks_key: str,
    refresh_at_key: str,
    cooldown_seconds: int | None = None,
) -> tuple[Literal["cooldown", "cached", "full"], list | None, int | None]:
    """Check whether we can short-circuit based on hash and cooldown.

    Returns:
        ("cooldown", cached_blocks, remaining_seconds) when hash matches and within cooldown.
        ("cached", cached_blocks, None) when hash matches and past cooldown.
        ("full", None, None) when hash differs or no cached blocks.
    """
    cooldown_sec = cooldown_seconds if cooldown_seconds is not None else _REFRESH_COOLDOWN_SECONDS

    cached_hash = _cache_get(hash_key)
    cached_blocks = _cache_get(blocks_key)
    last_refresh_at = _cache_get(refresh_at_key)
    now = time.monotonic()

    if current_hash != cached_hash or cached_blocks is None:
        return ("full", None, None)

    if last_refresh_at is not None and (now - last_refresh_at) < cooldown_sec:
        remaining = max(0, int(cooldown_sec - (now - last_refresh_at)))
        return ("cooldown", cached_blocks, remaining)

    return ("cached", cached_blocks, None)


def refresh_after_full(
    hash_key: str,
    blocks_key: str,
    refresh_at_key: str,
    current_hash: str,
    block_dicts: list,
    cooldown_seconds: int | None = None,
) -> None:
    """Store hash, blocks, and refresh timestamp after a full refresh."""
    cooldown_sec = cooldown_seconds if cooldown_seconds is not None else _REFRESH_COOLDOWN_SECONDS

    _cache_set(hash_key, current_hash, ttl=3600)
    _cache_set(blocks_key, block_dicts, ttl=3600)
    _cache_set(refresh_at_key, time.monotonic(), ttl=cooldown_sec * 2)
