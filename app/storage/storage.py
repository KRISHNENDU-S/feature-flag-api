"""Thread-safe in-memory flag storage guarded by an ``asyncio.Lock``."""

from __future__ import annotations

import asyncio

from app.logging_config import get_logger
from app.models import Flag

logger = get_logger("app.storage")


class StorageUnavailableError(RuntimeError):
    """Raised when the backing store cannot be reached.

    The in-memory implementation can be told to simulate an outage via
    :meth:`FlagStorage.set_available` so the evaluation service's graceful
    fallback path can be exercised in tests.
    """


class FlagStorage:
    """In-memory flag store. All mutations are serialised with an asyncio lock."""

    def __init__(self) -> None:
        self._flags: dict[str, Flag] = {}
        self._lock = asyncio.Lock()
        self._available = True

    def set_available(self, available: bool) -> None:
        """Toggle simulated availability (used to test graceful fallback)."""

        self._available = available

    def _check_available(self) -> None:
        if not self._available:
            raise StorageUnavailableError("flag storage is unavailable")

    async def create(self, flag: Flag) -> Flag:
        async with self._lock:
            self._check_available()
            if any(f.name == flag.name for f in self._flags.values()):
                raise ValueError(f"flag name already exists: {flag.name}")
            self._flags[flag.id] = flag
            logger.info(
                "flag created",
                extra={"context": {"flag_id": flag.id, "name": flag.name}},
            )
            return flag

    async def get(self, flag_id: str) -> Flag | None:
        async with self._lock:
            self._check_available()
            return self._flags.get(flag_id)

    async def list(
        self,
        *,
        name: str | None = None,
        default_state: bool | None = None,
    ) -> list[Flag]:
        """Return all flags, optionally filtered.

        ``name`` does a case-insensitive *substring* match; ``default_state``
        is an exact match. Filters combine with AND.
        """

        async with self._lock:
            self._check_available()
            flags = list(self._flags.values())

        if name is not None:
            needle = name.lower()
            flags = [f for f in flags if needle in f.name.lower()]
        if default_state is not None:
            flags = [f for f in flags if f.default_state == default_state]
        return flags

    async def update(self, flag_id: str, changes: dict[str, object]) -> Flag:
        """Apply a partial update to an existing flag and return it.

        ``changes`` holds only the fields to overwrite (already validated and
        correctly typed). Raises ``KeyError`` if the flag does not exist and
        ``ValueError`` if a new ``name`` collides with another flag.
        """

        async with self._lock:
            self._check_available()
            existing = self._flags.get(flag_id)
            if existing is None:
                raise KeyError(flag_id)

            new_name = changes.get("name")
            if (
                new_name is not None
                and new_name != existing.name
                and any(
                    f.name == new_name
                    for fid, f in self._flags.items()
                    if fid != flag_id
                )
            ):
                raise ValueError(f"flag name already exists: {new_name}")

            updated = existing.model_copy(update=changes)
            self._flags[flag_id] = updated
            logger.info(
                "flag updated",
                extra={
                    "context": {"flag_id": flag_id, "fields": sorted(changes)}
                },
            )
            return updated

    async def delete(self, flag_id: str) -> bool:
        async with self._lock:
            self._check_available()
            existed = self._flags.pop(flag_id, None) is not None
            if existed:
                logger.info("flag deleted", extra={"context": {"flag_id": flag_id}})
            return existed

    async def count(self) -> int:
        async with self._lock:
            return len(self._flags)

    def clear(self) -> None:
        """Synchronously drop all flags (used in tests)."""

        self._flags.clear()
        self._available = True
