"""In-memory TTL cache for flag evaluation results.

The cache key is derived from ``flag_id`` plus a hash of the user context, so
different user contexts for the same flag are cached independently. Entries
expire after ``ttl_seconds`` (default 300s). Hit/miss counters are exposed for
the ``/health`` endpoint, and ``invalidate(flag_id)`` drops every entry that
belongs to a flag (used when a flag is updated or deleted).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from app.logging_config import get_logger

logger = get_logger("app.cache")


@dataclass
class _Entry:
    value: Any
    expires_at: float
    flag_id: str


def make_key(flag_id: str, user_context: dict[str, str]) -> str:
    """Build a stable cache key from a flag id and a user context.

    The context is serialised with sorted keys so logically identical
    contexts always produce the same key regardless of dict ordering.
    """

    serialized = json.dumps(user_context, sort_keys=True)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"{flag_id}:{digest}"


class TTLCache:
    """A simple thread/async-safe-enough in-memory cache with TTL.

    Access is synchronous and fast; the FastAPI layer serialises writes to
    storage via an ``asyncio.Lock``, and cache operations here are individually
    atomic dict operations, which is sufficient for a single-process service.
    """

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, _Entry] = {}
        self._hits = 0
        self._misses = 0

    # -- core operations ---------------------------------------------------
    def get(self, key: str) -> Any | None:
        """Return the cached value for ``key`` or ``None`` on miss/expiry."""

        entry = self._store.get(key)
        now = time.monotonic()
        if entry is None:
            self._misses += 1
            logger.debug("cache miss", extra={"context": {"key": key}})
            return None
        if entry.expires_at <= now:
            # Expired: evict and count as a miss.
            self._store.pop(key, None)
            self._misses += 1
            logger.debug("cache expired", extra={"context": {"key": key}})
            return None
        self._hits += 1
        logger.debug("cache hit", extra={"context": {"key": key}})
        return entry.value

    def set(self, key: str, value: Any, flag_id: str) -> None:
        """Store ``value`` under ``key``, tagged with ``flag_id`` for invalidation."""

        self._store[key] = _Entry(
            value=value,
            expires_at=time.monotonic() + self._ttl,
            flag_id=flag_id,
        )
        logger.debug("cache set", extra={"context": {"key": key, "flag_id": flag_id}})

    def invalidate(self, flag_id: str) -> int:
        """Remove every entry belonging to ``flag_id``. Returns count removed."""

        to_remove = [k for k, e in self._store.items() if e.flag_id == flag_id]
        for k in to_remove:
            self._store.pop(k, None)
        logger.info(
            "cache invalidate",
            extra={"context": {"flag_id": flag_id, "removed": len(to_remove)}},
        )
        return len(to_remove)

    def clear(self) -> None:
        """Drop all entries and reset counters (used in tests)."""

        self._store.clear()
        self._hits = 0
        self._misses = 0

    # -- stats -------------------------------------------------------------
    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    @property
    def hit_rate(self) -> float:
        """Fraction of lookups that were hits (0.0 when there were none)."""

        total = self._hits + self._misses
        if total == 0:
            return 0.0
        return self._hits / total

    def stats(self) -> dict[str, float | int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self.hit_rate, 4),
            "size": len(self._store),
        }
