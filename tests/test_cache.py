"""Tests for the TTL cache: hit/miss tracking, expiry, and invalidation."""

from __future__ import annotations

import pytest

from app.cache.cache import TTLCache, make_key
from app.models import (
    EvaluationRequest,
    Flag,
    Operator,
    Rule,
    RuleField,
)


def test_make_key_is_stable_and_context_sensitive():
    k1 = make_key("flag-1", {"user_id": "u", "region": "us"})
    k2 = make_key("flag-1", {"region": "us", "user_id": "u"})  # order swapped
    k3 = make_key("flag-1", {"user_id": "u", "region": "eu"})
    assert k1 == k2
    assert k1 != k3
    assert k1.startswith("flag-1:")


def test_hit_and_miss_counters():
    cache = TTLCache(ttl_seconds=300)
    assert cache.get("missing") is None  # miss
    cache.set("k", "v", flag_id="flag-1")
    assert cache.get("k") == "v"  # hit
    assert cache.hits == 1
    assert cache.misses == 1
    assert cache.hit_rate == 0.5


def test_hit_rate_zero_when_empty():
    cache = TTLCache(ttl_seconds=300)
    assert cache.hit_rate == 0.0
    assert cache.stats()["hit_rate"] == 0.0


def test_ttl_expiry(monkeypatch):
    cache = TTLCache(ttl_seconds=300)

    fake_now = {"t": 1000.0}
    monkeypatch.setattr("app.cache.cache.time.monotonic", lambda: fake_now["t"])

    cache.set("k", "v", flag_id="flag-1")
    assert cache.get("k") == "v"  # not expired yet

    fake_now["t"] += 301  # advance beyond TTL
    assert cache.get("k") is None  # expired -> miss


def test_invalidate_removes_only_matching_flag():
    cache = TTLCache(ttl_seconds=300)
    cache.set("a", 1, flag_id="flag-1")
    cache.set("b", 2, flag_id="flag-1")
    cache.set("c", 3, flag_id="flag-2")

    removed = cache.invalidate("flag-1")
    assert removed == 2
    assert cache.get("a") is None
    assert cache.get("c") == 3


@pytest.mark.asyncio
async def test_evaluation_uses_cache_on_second_call(service, storage, cache):
    rule = Rule(field=RuleField.REGION, operator=Operator.EQUALS, value="us")
    flag = Flag(name="cached-flag", default_state=False, rules=[rule])
    await storage.create(flag)

    req = EvaluationRequest(user_id="u1", subscription_tier="free", region="us")

    first = await service.evaluate(flag.id, req)
    assert cache.misses == 1 and cache.hits == 0

    second = await service.evaluate(flag.id, req)
    assert cache.hits == 1
    assert first == second


@pytest.mark.asyncio
async def test_invalidation_forces_recompute(service, storage, cache):
    flag = Flag(name="inv-flag", default_state=True, rules=[])
    await storage.create(flag)
    req = EvaluationRequest(user_id="u1", subscription_tier="free", region="us")

    await service.evaluate(flag.id, req)  # populates cache
    cache.invalidate(flag.id)
    await service.evaluate(flag.id, req)  # must miss again

    assert cache.misses == 2
