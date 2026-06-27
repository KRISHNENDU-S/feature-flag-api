"""Tests for rule evaluation logic and graceful fallback."""

from __future__ import annotations

import pytest

from app.models import (
    EvaluationRequest,
    Flag,
    Operator,
    Rule,
    RuleField,
)
from app.services.evaluation_service import EvaluationService
from app.storage.storage import FlagStorage


def _request(user_id="u1", tier="premium", region="us") -> EvaluationRequest:
    return EvaluationRequest(user_id=user_id, subscription_tier=tier, region=region)


async def _store(storage: FlagStorage, **kwargs) -> Flag:
    flag = Flag(**kwargs)
    await storage.create(flag)
    return flag


@pytest.mark.asyncio
async def test_no_rules_uses_default_true(service, storage):
    flag = await _store(storage, name="f-default-on", default_state=True, rules=[])
    result = await service.evaluate(flag.id, _request())
    assert result.enabled is True
    assert result.reason == "no rules defined, using default"


@pytest.mark.asyncio
async def test_no_rules_uses_default_false(service, storage):
    flag = await _store(storage, name="f-default-off", default_state=False, rules=[])
    result = await service.evaluate(flag.id, _request())
    assert result.enabled is False
    assert result.reason == "no rules defined, using default"


@pytest.mark.asyncio
async def test_equals_match(service, storage):
    rule = Rule(field=RuleField.SUBSCRIPTION_TIER, operator=Operator.EQUALS, value="premium")
    flag = await _store(storage, name="f-eq", default_state=False, rules=[rule])
    assert (await service.evaluate(flag.id, _request(tier="premium"))).enabled is True


@pytest.mark.asyncio
async def test_equals_no_match(service, storage):
    rule = Rule(field=RuleField.SUBSCRIPTION_TIER, operator=Operator.EQUALS, value="premium")
    flag = await _store(storage, name="f-eq2", default_state=True, rules=[rule])
    result = await service.evaluate(flag.id, _request(tier="free"))
    assert result.enabled is False
    assert "rule 0 failed" in result.reason


@pytest.mark.asyncio
async def test_not_equals(service, storage):
    rule = Rule(field=RuleField.REGION, operator=Operator.NOT_EQUALS, value="eu")
    flag = await _store(storage, name="f-neq", default_state=False, rules=[rule])
    assert (await service.evaluate(flag.id, _request(region="us"))).enabled is True
    # different context -> different cache key, so this is a fresh evaluation
    assert (await service.evaluate(flag.id, _request(region="eu"))).enabled is False


@pytest.mark.asyncio
async def test_in_operator(service, storage):
    rule = Rule(field=RuleField.REGION, operator=Operator.IN, value=["us", "ca"])
    flag = await _store(storage, name="f-in", default_state=False, rules=[rule])
    assert (await service.evaluate(flag.id, _request(region="ca"))).enabled is True
    assert (await service.evaluate(flag.id, _request(region="eu"))).enabled is False


@pytest.mark.asyncio
async def test_not_in_operator(service, storage):
    rule = Rule(field=RuleField.REGION, operator=Operator.NOT_IN, value=["eu", "apac"])
    flag = await _store(storage, name="f-nin", default_state=False, rules=[rule])
    assert (await service.evaluate(flag.id, _request(region="us"))).enabled is True
    assert (await service.evaluate(flag.id, _request(region="eu"))).enabled is False


@pytest.mark.asyncio
async def test_all_rules_must_match(service, storage):
    rules = [
        Rule(field=RuleField.SUBSCRIPTION_TIER, operator=Operator.EQUALS, value="premium"),
        Rule(field=RuleField.REGION, operator=Operator.IN, value=["us"]),
    ]
    flag = await _store(storage, name="f-all", default_state=False, rules=rules)
    assert (await service.evaluate(flag.id, _request(tier="premium", region="us"))).enabled is True
    # second rule fails
    assert (await service.evaluate(flag.id, _request(tier="premium", region="eu"))).enabled is False


@pytest.mark.asyncio
async def test_missing_field_fails_rule(service, storage):
    # user_id is always present, but we can craft a rule on a field and remove it
    rule = Rule(field=RuleField.SUBSCRIPTION_TIER, operator=Operator.EQUALS, value="premium")
    flag = await _store(storage, name="f-missing", default_state=True, rules=[rule])

    # Patch evaluate by passing a context missing the field via a custom request
    # subclass is overkill; instead test via the private evaluator directly.
    result = service._evaluate_rules(flag, {"user_id": "u1", "region": "us"})
    assert result.enabled is False
    assert "not present in context" in result.reason


@pytest.mark.asyncio
async def test_storage_unavailable_fallback(service, storage):
    flag = await _store(storage, name="f-fallback", default_state=True, rules=[])
    storage.set_available(False)
    result = await service.evaluate(flag.id, _request())
    assert result.enabled is False
    assert result.reason == "storage unavailable, using default"


@pytest.mark.asyncio
async def test_flag_not_found_raises(service, storage):
    with pytest.raises(KeyError):
        await service.evaluate("does-not-exist", _request())


@pytest.mark.asyncio
async def test_percentage_rollout_deterministic(service, storage):
    # 100% -> always included; 0% -> always excluded
    on = await _store(storage, name="f-100", default_state=True, rules=[], percentage=100)
    off = await _store(storage, name="f-0", default_state=True, rules=[], percentage=0)
    assert (await service.evaluate(on.id, _request(user_id="abc"))).enabled is True
    assert (await service.evaluate(off.id, _request(user_id="abc"))).enabled is False
