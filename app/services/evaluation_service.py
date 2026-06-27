"""Flag evaluation logic.

Evaluation order:
  1. Check the cache (keyed by flag id + user context). Return on hit.
  2. Load the flag from storage. If storage is down, fall back to nothing we
     can return a flag for -> handled by the caller via a graceful default.
  3. No rules -> return ``default_state``.
  4. Evaluate every rule; ALL must match for the flag to be ON. A missing
     context field makes that rule fail.
  5. Optional percentage rollout gates an otherwise-ON result on a stable
     ``hash(user_id) % 100 < percentage`` bucket.
  6. Cache and return the result.
"""

from __future__ import annotations

import hashlib

from app.cache.cache import TTLCache, make_key
from app.logging_config import get_logger
from app.models import (
    EvaluationRequest,
    EvaluationResult,
    Flag,
    Operator,
)
from app.storage.storage import FlagStorage, StorageUnavailableError

logger = get_logger("app.evaluation")


def _stable_bucket(user_id: str) -> int:
    """Deterministic 0-99 bucket for a user id.

    Uses a stable hash (sha256) rather than the builtin ``hash`` so results are
    consistent across processes/runs. The spec describes the behaviour as
    ``hash(user_id) % 100 < percentage``; this is that hash, made stable.
    """

    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return int(digest, 16) % 100


def _rule_matches(operator: Operator, actual: str, expected: str | list[str]) -> bool:
    if operator is Operator.EQUALS:
        return actual == expected
    if operator is Operator.NOT_EQUALS:
        return actual != expected
    if operator is Operator.IN:
        return isinstance(expected, list) and actual in expected
    if operator is Operator.NOT_IN:
        return isinstance(expected, list) and actual not in expected
    return False


class EvaluationService:
    """Coordinates cache, storage, and rule evaluation."""

    def __init__(self, storage: FlagStorage, cache: TTLCache) -> None:
        self._storage = storage
        self._cache = cache

    def _evaluate_rules(self, flag: Flag, context: dict[str, str]) -> EvaluationResult:
        # No rules: use the default state.
        if not flag.rules:
            if flag.percentage is not None:
                return self._apply_percentage(
                    flag, context, base_enabled=flag.default_state,
                    base_reason="no rules defined, using default",
                )
            return EvaluationResult(
                flag_id=flag.id,
                enabled=flag.default_state,
                reason="no rules defined, using default",
            )

        # ALL rules must match.
        for idx, rule in enumerate(flag.rules):
            field = rule.field.value
            if field not in context:
                return EvaluationResult(
                    flag_id=flag.id,
                    enabled=False,
                    reason=(
                        f"rule {idx} failed: field '{field}' not present in context"
                    ),
                )
            if not _rule_matches(rule.operator, context[field], rule.value):
                return EvaluationResult(
                    flag_id=flag.id,
                    enabled=False,
                    reason=(
                        f"rule {idx} failed: {field} {rule.operator.value} "
                        f"{rule.value!r} (actual={context[field]!r})"
                    ),
                )

        # All rules matched.
        if flag.percentage is not None:
            return self._apply_percentage(
                flag, context, base_enabled=True,
                base_reason="all rules matched",
            )
        return EvaluationResult(
            flag_id=flag.id,
            enabled=True,
            reason="all rules matched",
        )

    def _apply_percentage(
        self,
        flag: Flag,
        context: dict[str, str],
        *,
        base_enabled: bool,
        base_reason: str,
    ) -> EvaluationResult:
        """Gate an otherwise-enabled result on the rollout percentage."""

        if not base_enabled:
            return EvaluationResult(
                flag_id=flag.id, enabled=False, reason=base_reason
            )
        bucket = _stable_bucket(context["user_id"])
        in_rollout = bucket < (flag.percentage or 0)
        reason = (
            f"{base_reason}; percentage rollout {flag.percentage}% "
            f"(bucket={bucket}, {'included' if in_rollout else 'excluded'})"
        )
        return EvaluationResult(flag_id=flag.id, enabled=in_rollout, reason=reason)

    async def evaluate(
        self, flag_id: str, request: EvaluationRequest
    ) -> EvaluationResult:
        """Evaluate ``flag_id`` for the given user context."""

        context = request.as_context()
        key = make_key(flag_id, context)

        # 1. Cache first.
        cached = self._cache.get(key)
        if cached is not None:
            logger.info(
                "evaluation served from cache",
                extra={"context": {"flag_id": flag_id, "user_id": request.user_id}},
            )
            return cached

        # 2. Load from storage with graceful fallback.
        try:
            flag = await self._storage.get(flag_id)
        except StorageUnavailableError:
            logger.warning(
                "storage unavailable during evaluation; using default",
                extra={"context": {"flag_id": flag_id}},
            )
            # We cannot know the configured default, so fail safe to disabled.
            return EvaluationResult(
                flag_id=flag_id,
                enabled=False,
                reason="storage unavailable, using default",
            )

        if flag is None:
            # Not found is surfaced as None; caller decides on 404.
            raise KeyError(flag_id)

        # 3-5. Evaluate.
        result = self._evaluate_rules(flag, context)

        # 6. Cache and return.
        self._cache.set(key, result, flag_id=flag_id)
        logger.info(
            "flag evaluated",
            extra={
                "context": {
                    "flag_id": flag_id,
                    "user_id": request.user_id,
                    "enabled": result.enabled,
                }
            },
        )
        return result
