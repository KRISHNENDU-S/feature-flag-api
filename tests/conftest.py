"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from app.cache.cache import TTLCache
from app.services.evaluation_service import EvaluationService
from app.storage.storage import FlagStorage


@pytest.fixture
def cache() -> TTLCache:
    return TTLCache(ttl_seconds=300)


@pytest.fixture
def storage() -> FlagStorage:
    return FlagStorage()


@pytest.fixture
def service(storage: FlagStorage, cache: TTLCache) -> EvaluationService:
    return EvaluationService(storage=storage, cache=cache)
