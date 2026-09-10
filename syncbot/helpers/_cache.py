"""Lightweight in-process TTL cache.

Lambda containers are reused across invocations, so a short TTL cache
avoids redundant DB queries for the same sync list within a warm container.
"""

import contextvars
import time as _time

_CACHE: dict = {}
_CACHE_TTL_SECONDS = 60
_USER_INFO_CACHE_TTL = 300  # 5 min for user info lookups
_REQUEST_CACHE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("syncbot_request_cache", default=None)


def _cache_get(key: str):
    """Return a cached value if it exists and has not expired, else *None*."""
    entry = _CACHE.get(key)
    if entry and (_time.monotonic() - entry["t"]) < entry.get("ttl", _CACHE_TTL_SECONDS):
        return entry["v"]
    _CACHE.pop(key, None)
    return None


def _cache_set(key: str, value, ttl: int = _CACHE_TTL_SECONDS):
    """Store *value* in the cache under *key* with an optional TTL (seconds)."""
    _CACHE[key] = {"v": value, "t": _time.monotonic(), "ttl": ttl}


def _cache_delete(key: str) -> None:
    """Remove a single cache entry."""
    _CACHE.pop(key, None)


def _cache_delete_prefix(prefix: str) -> int:
    """Remove all cache entries whose key starts with *prefix*. Returns count removed."""
    to_remove = [k for k in _CACHE if k.startswith(prefix)]
    for k in to_remove:
        _CACHE.pop(k, None)
    return len(to_remove)


def clear_all_caches() -> int:
    """Remove every entry from the in-process cache. Returns count removed."""
    count = len(_CACHE)
    _CACHE.clear()
    return count


def begin_request_scope() -> None:
    """Start a per-request dict (mapping rows, user tokens, channel lookups)."""
    _REQUEST_CACHE.set({})


def clear_request_scope() -> None:
    """Drop the current request-scope dict (tests and end-of-request)."""
    _REQUEST_CACHE.set(None)


def request_scope_get(key: str):
    store = _REQUEST_CACHE.get()
    if not store:
        return None
    return store.get(key)


def request_scope_set(key: str, value) -> None:
    store = _REQUEST_CACHE.get()
    if store is None:
        return
    store[key] = value


def request_scope_delete(key: str) -> None:
    store = _REQUEST_CACHE.get()
    if store:
        store.pop(key, None)
