"""Pydantic models and enums for the Feature Flag service."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RuleField(str, Enum):
    """User-context attribute a rule is evaluated against."""

    USER_ID = "user_id"
    SUBSCRIPTION_TIER = "subscription_tier"
    REGION = "region"


class Operator(str, Enum):
    """Comparison operators supported by a rule."""

    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    IN = "in"
    NOT_IN = "not_in"


_LIST_OPERATORS = {Operator.IN, Operator.NOT_IN}
_SCALAR_OPERATORS = {Operator.EQUALS, Operator.NOT_EQUALS}


class Rule(BaseModel):
    """A single condition that must hold for a flag to be enabled.

    ``value`` is a string for the scalar operators (``equals``/``not_equals``)
    and a list of strings for the membership operators (``in``/``not_in``).
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    # Enums are accepted from their string values (how JSON delivers them);
    # everything else stays strict (no int->str / "true"->bool coercion).
    field: RuleField = Field(strict=False)
    operator: Operator = Field(strict=False)
    value: str | list[str]

    @model_validator(mode="after")
    def _validate_value_shape(self) -> "Rule":
        if self.operator in _LIST_OPERATORS and not isinstance(self.value, list):
            raise ValueError(
                f"operator '{self.operator.value}' requires a list value"
            )
        if self.operator in _SCALAR_OPERATORS and not isinstance(self.value, str):
            raise ValueError(
                f"operator '{self.operator.value}' requires a string value"
            )
        return self


class FlagCreate(BaseModel):
    """Request body for creating a flag."""

    model_config = ConfigDict(strict=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    default_state: bool
    rules: list[Rule] = Field(default_factory=list)
    # Optional percentage rollout (0-100). When set, gates the flag on a
    # deterministic hash bucket in addition to the rules.
    percentage: int | None = Field(default=None, ge=0, le=100)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class Flag(FlagCreate):
    """A persisted feature flag."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class EvaluationRequest(BaseModel):
    """User context used to evaluate a flag."""

    model_config = ConfigDict(strict=True, extra="forbid")

    user_id: str = Field(min_length=1)
    subscription_tier: str
    region: str

    def as_context(self) -> dict[str, str]:
        """Return the request as a plain ``field -> value`` mapping."""

        return {
            RuleField.USER_ID.value: self.user_id,
            RuleField.SUBSCRIPTION_TIER.value: self.subscription_tier,
            RuleField.REGION.value: self.region,
        }


class EvaluationResult(BaseModel):
    """Outcome of evaluating a flag for a given user context."""

    flag_id: str
    enabled: bool
    reason: str
