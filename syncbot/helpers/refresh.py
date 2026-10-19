"""Cache of the last Home tab so Refresh can skip an unchanged publish."""

from helpers._cache import _cache_get, _cache_set


def cached_home_blocks(current_hash: str, hash_key: str, blocks_key: str) -> list | None:
    """Return the cached Home blocks when the hash still matches, otherwise ``None``."""
    cached_hash = _cache_get(hash_key)
    cached_blocks = _cache_get(blocks_key)
    if current_hash != cached_hash or cached_blocks is None:
        return None
    return cached_blocks


def refresh_after_full(
    hash_key: str,
    blocks_key: str,
    current_hash: str,
    block_dicts: list,
) -> None:
    """Store the hash and blocks after a full Home publish."""
    _cache_set(hash_key, current_hash, ttl=3600)
    _cache_set(blocks_key, block_dicts, ttl=3600)
