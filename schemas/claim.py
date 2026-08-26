"""Versioned, evidenced claim schemas."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.common import AwareDatetime, ClaimId, DeltaId, NonEmptyStr
from schemas.delta import ONTOLOGY_BRANCHES


class ClaimClass(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    OPERATIONAL = "operational"


class InferenceKind(StrEnum):
    PRESENT_STATE = "present_state"
    INTENT_OR_FUTURE = "intent_or_future"


class Confidence(StrEnum):
    CONFIRMED = "confirmed"
    LIKELY = "likely"
    SPECULATIVE = "speculative"


class Severity(StrEnum):
    CRITICAL = "critical"
    NOTABLE = "notable"
    CONTEXT = "context"


class ClaimStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delta_id: DeltaId
    source: NonEmptyStr
    note: NonEmptyStr


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    claim_id: ClaimId
    entity: NonEmptyStr
    scope: NonEmptyStr
    class_: ClaimClass = Field(alias="class")
    inference_kind: InferenceKind | None = None
    statement: NonEmptyStr
    rationale: NonEmptyStr
    confidence: Confidence
    severity: Severity
    evidence: list[Evidence] = Field(min_length=1)
    status: ClaimStatus
    superseded_by: ClaimId | None = None
    supersedes: ClaimId | None = None
    disputes: ClaimId | None = None
    version: int = Field(ge=1)
    created_at: AwareDatetime
    last_verified_at: AwareDatetime
    created_by: NonEmptyStr
    history: list[dict[str, Any]] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def enforce_claim_rules(self) -> Claim:
        if self.scope not in ONTOLOGY_BRANCHES and not self.scope.startswith("sources/"):
            raise ValueError(f"unknown ontology scope: {self.scope}")
        if self.scope.startswith("sources/") and len(self.scope.removeprefix("sources/")) == 0:
            raise ValueError("operational source scope must name a source")

        if self.class_ is ClaimClass.FACT and self.confidence is not Confidence.CONFIRMED:
            raise ValueError("fact claims must have confirmed confidence")
        if self.class_ is ClaimClass.INFERENCE:
            if self.inference_kind is None:
                raise ValueError("inference claims require inference_kind")
            if self.confidence not in {Confidence.LIKELY, Confidence.SPECULATIVE}:
                raise ValueError("inference confidence must be likely or speculative")
            if self.disputes is None and len({item.source for item in self.evidence}) < 2:
                raise ValueError("inference claims require evidence from distinct sources")
            if self.disputes is not None:
                if len(self.evidence) != 1:
                    raise ValueError("dispute inference must cite exactly one conflicting signal")
                if self.confidence is not Confidence.SPECULATIVE:
                    raise ValueError("dispute inference confidence must be speculative")
            if (
                self.inference_kind is InferenceKind.INTENT_OR_FUTURE
                and self.confidence is not Confidence.SPECULATIVE
            ):
                raise ValueError("intent_or_future inference confidence must be speculative")
        elif self.inference_kind is not None or self.disputes is not None:
            raise ValueError("only inference claims may set inference_kind or disputes")
        if self.class_ is ClaimClass.OPERATIONAL and not self.scope.startswith("sources/"):
            raise ValueError("operational claims must use a sources/<source> scope")

        if self.status is ClaimStatus.SUPERSEDED and self.superseded_by is None:
            raise ValueError("a superseded claim must link to its replacement")
        if self.status is ClaimStatus.ACTIVE and self.superseded_by is not None:
            raise ValueError("an active claim cannot have superseded_by")
        if (
            self.supersedes == self.claim_id
            or self.superseded_by == self.claim_id
            or self.disputes == self.claim_id
        ):
            raise ValueError("a claim cannot link to itself")
        return self


class SupersessionPair(BaseModel):
    """Fixture/helper model proving both links of a supersession chain."""

    model_config = ConfigDict(extra="forbid")
    old: Claim
    new: Claim

    @model_validator(mode="after")
    def links_match(self) -> SupersessionPair:
        if self.old.status is not ClaimStatus.SUPERSEDED:
            raise ValueError("old claim must be superseded")
        if self.old.superseded_by != self.new.claim_id:
            raise ValueError("old.superseded_by must point to new claim")
        if self.new.supersedes != self.old.claim_id:
            raise ValueError("new.supersedes must point to old claim")
        return self
