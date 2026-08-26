"""The strategy dispatcher contract.

Both triggers - the weekly Cloud Scheduler session and the authenticated
dashboard "Run Strategy Session" action - use this one request shape.  It
accepts a bounded period and a request ID and nothing else: there is no field a
caller could use to smuggle prompt text into an agent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated

from schemas.strategy import SessionPeriod, StrategyTrigger

MAX_PERIOD_DAYS = 31
MIN_PERIOD_DAYS = 1

RequestId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]


class StrategyRequestError(ValueError):
    """The trigger payload is not a valid bounded strategy request."""


class StrategyRequest(BaseModel):
    """The only payload a strategy dispatcher accepts."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    request_id: RequestId
    trigger: StrategyTrigger
    period_from: datetime
    period_to: datetime
    strategy_version: Annotated[str, StringConstraints(min_length=1, max_length=64)] = Field(
        default="strategy-council@1"
    )

    @model_validator(mode="after")
    def bounded_period(self) -> "StrategyRequest":
        for name, value in (("period_from", self.period_from), ("period_to", self.period_to)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must include a timezone offset")
        span = self.period_to - self.period_from
        if span.days < MIN_PERIOD_DAYS:
            raise ValueError(f"a strategy period must span at least {MIN_PERIOD_DAYS} day")
        if span.days > MAX_PERIOD_DAYS:
            raise ValueError(f"a strategy period may not exceed {MAX_PERIOD_DAYS} days")
        return self

    def period(self) -> SessionPeriod:
        return SessionPeriod(**{"from": self.period_from, "to": self.period_to})


@dataclass(frozen=True)
class ParsedStrategyRequest:
    request: StrategyRequest
    period: SessionPeriod


def parse_strategy_request(body: bytes | str | dict[str, Any]) -> ParsedStrategyRequest:
    """Parse and validate one trigger payload; reject anything else."""
    if isinstance(body, (bytes, str)):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise StrategyRequestError("strategy request is not valid JSON") from exc
    else:
        payload = body
    if not isinstance(payload, dict):
        raise StrategyRequestError("strategy request must be a JSON object")
    allowed = set(StrategyRequest.model_fields)
    unknown = set(payload) - allowed
    if unknown:
        # Named explicitly so a caller cannot pass "prompt", "question", or
        # "instructions" and have it silently ignored.
        raise StrategyRequestError(f"strategy request rejects fields: {sorted(unknown)}")
    try:
        request = StrategyRequest.model_validate(payload)
    except Exception as exc:
        raise StrategyRequestError(f"invalid strategy request: {exc}") from exc
    return ParsedStrategyRequest(request=request, period=request.period())


def supported_triggers() -> tuple[str, ...]:
    """The two triggers that share this code path."""
    return tuple(get_args(StrategyTrigger))
