"""Strict strategy-council contracts: premises, cards, challenges, sessions.

Every field a model may propose is bounded and closed here.  Nothing in this
module trusts a model-supplied identifier: ``PremiseReference.delta_ids`` is
recomputed from the claim store before a card is ever constructed, which is why
the draft schemas the agents actually emit carry no Delta IDs at all.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, StringConstraints, model_validator

from schemas.common import AwareDatetime, ClaimId, DeltaId, NonEmptyStr
from schemas.delta import ONTOLOGY_BRANCHES

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"

STRATEGY_VERSION = "strategy-council@1"
STRATEGIST_VERSION = "tycho_strategist@1"
CHALLENGER_VERSION = "tycho_challenger@1"
BRIEF_WRITER_VERSION = "tycho_brief_writer@1"

#: The single question a V1 strategy session may answer.  The dispatcher never
#: accepts prompt text, so this constant is the only strategy question there is.
STRATEGY_QUESTION = (
    "What materially changed across the monitored coding-agent market, why does "
    "it matter, and what evidence could change this conclusion?"
)

MAX_CARDS_PER_SESSION = 3
MIN_PREMISES_PER_CARD = 2
MAX_PREMISES_PER_CARD = 5
MIN_ENTITIES_PER_CARD = 2
MIN_SOURCE_FAMILIES_PER_CARD = 2

MAX_STATEMENT_LENGTH = 400
MAX_RATIONALE_LENGTH = 800
MAX_COMPETING_LENGTH = 600
MAX_FALSIFIER_LENGTH = 600
MAX_REASON_LENGTH = 300
MAX_DETAIL_LENGTH = 300
MAX_RENDERED_LENGTH = 20_000

StrategyCardId = Annotated[str, StringConstraints(pattern=rf"^stc_{_ULID}$")]
StrategySessionId = Annotated[str, StringConstraints(pattern=rf"^sts_{_ULID}$")]
RunId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

BoundedStatement = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_STATEMENT_LENGTH)
]
BoundedRationale = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_RATIONALE_LENGTH)
]
BoundedCompeting = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_COMPETING_LENGTH)
]
BoundedFalsifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_FALSIFIER_LENGTH)
]
BoundedReason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REASON_LENGTH)
]
BoundedDetail = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_DETAIL_LENGTH)
]


class StrategyConfidence(StrEnum):
    """Strategy cards are never ``confirmed``; synthesis is not observation."""

    LIKELY = "likely"
    SPECULATIVE = "speculative"


class StrategyInferenceKind(StrEnum):
    PRESENT_STATE = "present_state"


class CardStatus(StrEnum):
    PROPOSED = "proposed"
    PASSED = "passed"
    REJECTED = "rejected"


class SessionState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class LimitationKind(StrEnum):
    STALE_PREMISE = "stale_premise"


def _unique(values: list[str], label: str) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")
    return values


def _known_scopes(values: list[str], label: str) -> list[str]:
    unknown = set(values) - ONTOLOGY_BRANCHES
    if unknown:
        raise ValueError(f"unknown ontology scopes in {label}: {sorted(unknown)}")
    return values


class CardLimitation(BaseModel):
    """An explicitly labelled weakness attached to one named premise claim."""

    model_config = ConfigDict(extra="forbid")

    kind: LimitationKind
    claim_id: ClaimId
    detail: BoundedDetail


class DraftPremise(BaseModel):
    """What the Strategist is allowed to cite: a claim at an exact version."""

    model_config = ConfigDict(extra="forbid")

    claim_id: ClaimId
    claim_version: int = Field(ge=1)


class StrategyCardDraft(BaseModel):
    """The Strategist's structured output.

    It deliberately carries no Delta IDs, entities, scopes, metrics, or counts:
    every one of those is recomputed from the claim store in Python.
    """

    model_config = ConfigDict(extra="forbid")

    statement: BoundedStatement
    rationale: BoundedRationale
    confidence: StrategyConfidence
    competing_explanation: BoundedCompeting
    falsifier: BoundedFalsifier
    premises: list[DraftPremise] = Field(min_length=1, max_length=MAX_PREMISES_PER_CARD)
    limitations: list[CardLimitation] = Field(default_factory=list, max_length=MAX_PREMISES_PER_CARD)

    @model_validator(mode="after")
    def premises_are_distinct(self) -> "StrategyCardDraft":
        _unique([premise.claim_id for premise in self.premises], "draft premise claim IDs")
        limitation_claims = {item.claim_id for item in self.limitations}
        premise_claims = {premise.claim_id for premise in self.premises}
        if not limitation_claims <= premise_claims:
            raise ValueError("a limitation must name one of the card's own premises")
        return self


class StrategyProposal(BaseModel):
    """One Strategist pass over one bounded context manifest."""

    model_config = ConfigDict(extra="forbid")

    cards: list[StrategyCardDraft] = Field(default_factory=list, max_length=MAX_CARDS_PER_SESSION)
    no_pattern_reason: BoundedReason | None = None

    @model_validator(mode="after")
    def zero_cards_needs_a_reason(self) -> "StrategyProposal":
        if not self.cards and not self.no_pattern_reason:
            raise ValueError("a zero-card proposal must explain why no pattern is defensible")
        return self


class PremiseReference(BaseModel):
    """A premise pinned to an exact claim version and its canonical Deltas.

    ``delta_ids`` is empty only on a rejected card whose premise could not be
    resolved in the store.  A passed card is required to carry them.
    """

    model_config = ConfigDict(extra="forbid")

    claim_id: ClaimId
    claim_version: int = Field(ge=1)
    delta_ids: list[DeltaId] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def delta_ids_are_distinct(self) -> "PremiseReference":
        _unique(list(self.delta_ids), "premise delta IDs")
        return self


class StrategyCard(BaseModel):
    """A validated strategy conclusion; ``passed`` only after every hard check."""

    model_config = ConfigDict(extra="forbid")

    card_id: StrategyCardId
    statement: BoundedStatement
    rationale: BoundedRationale
    inference_kind: StrategyInferenceKind = StrategyInferenceKind.PRESENT_STATE
    confidence: StrategyConfidence
    competing_explanation: BoundedCompeting
    falsifier: BoundedFalsifier
    entities: list[NonEmptyStr] = Field(min_length=1, max_length=8)
    scopes: list[NonEmptyStr] = Field(min_length=1, max_length=8)
    source_families: list[NonEmptyStr] = Field(min_length=1, max_length=16)
    premises: list[PremiseReference] = Field(min_length=1, max_length=MAX_PREMISES_PER_CARD)
    limitations: list[CardLimitation] = Field(default_factory=list, max_length=MAX_PREMISES_PER_CARD)
    status: CardStatus
    rejection_reasons: list[BoundedReason] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def enforce_card_shape(self) -> "StrategyCard":
        _unique([premise.claim_id for premise in self.premises], "premise claim IDs")
        _unique(list(self.entities), "card entities")
        _unique(_known_scopes(list(self.scopes), "card scopes"), "card scopes")
        _unique(list(self.source_families), "card source families")
        limitation_claims = {item.claim_id for item in self.limitations}
        if not limitation_claims <= {premise.claim_id for premise in self.premises}:
            raise ValueError("a limitation must name one of the card's own premises")
        if self.status is CardStatus.REJECTED and not self.rejection_reasons:
            raise ValueError("a rejected card must record why it was rejected")
        if self.status is not CardStatus.REJECTED and self.rejection_reasons:
            raise ValueError("only a rejected card may carry rejection reasons")
        if self.status is CardStatus.PASSED:
            if len(self.premises) < MIN_PREMISES_PER_CARD:
                raise ValueError("a passed card needs at least two premises")
            if any(not premise.delta_ids for premise in self.premises):
                raise ValueError("every premise of a passed card needs canonical Delta IDs")
            if len(self.entities) < MIN_ENTITIES_PER_CARD:
                raise ValueError("a passed card needs at least two distinct entities")
            if len(self.source_families) < MIN_SOURCE_FAMILIES_PER_CARD:
                raise ValueError("a passed card needs at least two distinct source families")
            if self.limitations and self.confidence is not StrategyConfidence.SPECULATIVE:
                raise ValueError("a labelled limitation forces speculative confidence")
        return self


class ChallengeVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class ChallengeResult(BaseModel):
    """The Challenger's structured output for exactly one card."""

    model_config = ConfigDict(extra="forbid")

    card_id: StrategyCardId
    verdict: ChallengeVerdict
    unsupported_premise_claim_ids: list[ClaimId] = Field(
        default_factory=list, max_length=MAX_PREMISES_PER_CARD
    )
    policy_violations: list[BoundedReason] = Field(default_factory=list, max_length=12)
    correction_request: BoundedReason | None = None

    @model_validator(mode="after")
    def failure_must_be_explained(self) -> "ChallengeResult":
        _unique(list(self.unsupported_premise_claim_ids), "unsupported premise claim IDs")
        if self.verdict is ChallengeVerdict.FAIL and not (
            self.unsupported_premise_claim_ids or self.policy_violations
        ):
            raise ValueError("a failing challenge must name a premise or a policy violation")
        if self.verdict is ChallengeVerdict.PASS and (
            self.unsupported_premise_claim_ids or self.policy_violations
        ):
            raise ValueError("a passing challenge cannot also report defects")
        return self


class ManifestEntry(BaseModel):
    """One claim admitted to a session, pinned at the version the session read."""

    model_config = ConfigDict(extra="forbid")

    claim_id: ClaimId
    claim_version: int = Field(ge=1)
    entity: NonEmptyStr
    scope: NonEmptyStr
    confidence: NonEmptyStr
    severity: NonEmptyStr
    delta_ids: list[DeltaId] = Field(min_length=1, max_length=16)
    source_families: list[NonEmptyStr] = Field(min_length=1, max_length=16)
    last_verified_at: AwareDatetime
    stale: bool
    staleness_days: int = Field(ge=0)


class MetricEvidence(BaseModel):
    """A deterministic metric that carries the Deltas it was computed from."""

    model_config = ConfigDict(extra="forbid")

    name: NonEmptyStr
    value: int = Field(ge=0)
    delta_ids: list[DeltaId] = Field(default_factory=list, max_length=64)


class SessionPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime

    @model_validator(mode="after")
    def chronological(self) -> "SessionPeriod":
        if self.from_ >= self.to:
            raise ValueError("strategy period 'from' must precede 'to'")
        return self

    def lease_key(self, strategy_version: str) -> str:
        return f"{self.from_.isoformat()}\0{self.to.isoformat()}\0{strategy_version}"


class AgentVersions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategist: NonEmptyStr = STRATEGIST_VERSION
    challenger: NonEmptyStr = CHALLENGER_VERSION
    brief_writer: NonEmptyStr = BRIEF_WRITER_VERSION


class ModelVersions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategist: NonEmptyStr
    challenger: NonEmptyStr
    brief_writer: NonEmptyStr


class SessionMetrics(BaseModel):
    """Safe counters only: no claim text, quotes, prompts, or model output."""

    model_config = ConfigDict(extra="forbid")

    cards_proposed: int = Field(default=0, ge=0)
    cards_passed: int = Field(default=0, ge=0)
    cards_rejected: int = Field(default=0, ge=0)
    input_bytes: int = Field(default=0, ge=0)
    estimated_input_tokens: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def passed_cards_are_capped(self) -> "SessionMetrics":
        if self.cards_passed > MAX_CARDS_PER_SESSION:
            raise ValueError(f"at most {MAX_CARDS_PER_SESSION} cards may survive a session")
        return self


class StrategySession(BaseModel):
    """A write-once record of one bounded strategy run."""

    model_config = ConfigDict(extra="forbid")

    session_id: StrategySessionId
    strategy_version: NonEmptyStr = STRATEGY_VERSION
    question: StrictStr
    period: SessionPeriod
    input_manifest: list[ManifestEntry] = Field(default_factory=list, max_length=200)
    manifest_hash: Sha256Hex
    metrics_evidence: list[MetricEvidence] = Field(default_factory=list, max_length=16)
    cards: list[StrategyCard] = Field(default_factory=list, max_length=12)
    challenges: list[ChallengeResult] = Field(default_factory=list, max_length=12)
    agent_versions: AgentVersions = Field(default_factory=AgentVersions)
    model_versions: ModelVersions
    state: SessionState
    metrics: SessionMetrics = Field(default_factory=SessionMetrics)
    run_ids: list[RunId] = Field(default_factory=list, max_length=8)
    brief_id: NonEmptyStr | None = None
    error: BoundedReason | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def enforce_session_shape(self) -> "StrategySession":
        if self.question != STRATEGY_QUESTION:
            raise ValueError("v1 sessions answer only the fixed market question")
        _unique([card.card_id for card in self.cards], "session card IDs")
        _unique(
            [entry.claim_id for entry in self.input_manifest], "manifest claim IDs"
        )
        card_ids = {card.card_id for card in self.cards}
        challenged = [result.card_id for result in self.challenges]
        _unique(challenged, "challenge card IDs")
        if not set(challenged) <= card_ids:
            raise ValueError("a challenge must reference a card in this session")
        passed = [card for card in self.cards if card.status is CardStatus.PASSED]
        if len(passed) > MAX_CARDS_PER_SESSION:
            raise ValueError(f"at most {MAX_CARDS_PER_SESSION} cards may survive a session")
        if self.state is SessionState.FAILED and not self.error:
            raise ValueError("a failed session must record a bounded error")
        if self.state is not SessionState.FAILED and self.error:
            raise ValueError("only a failed session may record an error")
        if self.state is not SessionState.COMPLETED and self.brief_id:
            raise ValueError("only a completed session may reference a brief")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self

    def passed_cards(self) -> list[StrategyCard]:
        return [card for card in self.cards if card.status is CardStatus.PASSED]

    def rejected_cards(self) -> list[StrategyCard]:
        return [card for card in self.cards if card.status is CardStatus.REJECTED]


def manifest_hash(entries: list[ManifestEntry]) -> str:
    """Hash the exact claim versions a session read, in a stable order."""
    identity = "\n".join(
        f"{entry.claim_id}@{entry.claim_version}"
        for entry in sorted(entries, key=lambda item: item.claim_id)
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class StrategyBriefSection(StrEnum):
    WHAT_CHANGED = "What changed"
    WHAT_TYCHO_CONCLUDES = "What Tycho concludes"
    COUNTER_SIGNALS = "Counter-signals"
    WHAT_WOULD_CHANGE_OUR_MIND = "What would change our mind"


class BriefDraft(BaseModel):
    """The Brief Writer's structured output, before citation validation."""

    model_config = ConfigDict(extra="forbid")

    what_changed: StrictStr = Field(min_length=1, max_length=MAX_RENDERED_LENGTH)
    what_tycho_concludes: StrictStr = Field(min_length=1, max_length=MAX_RENDERED_LENGTH)
    counter_signals: StrictStr = Field(min_length=1, max_length=MAX_RENDERED_LENGTH)
    what_would_change_our_mind: StrictStr = Field(min_length=1, max_length=MAX_RENDERED_LENGTH)


class CitationMarker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: ClaimId
    version: int = Field(ge=1)


StrategyTrigger = Literal["scheduler", "dashboard"]
