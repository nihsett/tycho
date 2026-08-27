"""Strict, bounded response schemas for the dashboard API.

Every response is a closed Pydantic model.  Nothing free-form crosses the wire:
enums are enums, IDs keep their ULID shape, prose carries an explicit maximum
length, and unknown fields are rejected in both directions.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from schemas.common import ClaimId, DeltaId, ObservationId

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_200)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Quote = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)]
Markdown = Annotated[str, StringConstraints(min_length=0, max_length=40_000)]
CardId = Annotated[str, StringConstraints(pattern=r"^stc_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
SessionId = Annotated[str, StringConstraints(pattern=r"^sts_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
RunId = Annotated[str, StringConstraints(pattern=r"^run_[0-9a-f]{16}$")]
BriefId = Annotated[str, StringConstraints(pattern=r"^brf_[A-Za-z0-9._:-]{1,64}$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Health -----------------------------------------------------------------


class ComponentState(StrEnum):
    OK = "ok"
    STALE = "stale"
    IDLE = "idle"
    FAILED = "failed"
    UNKNOWN = "unknown"


class HealthComponent(Model):
    key: Label
    name: Label
    state: ComponentState
    detail: ShortText
    last_success_at: datetime | None = None
    count: int = Field(default=0, ge=0)


class HealthResponse(Model):
    generated_at: datetime
    state: ComponentState
    components: list[HealthComponent] = Field(max_length=8)


# --- Overview ---------------------------------------------------------------


class SourceRef(Model):
    """Where a watcher looks.  Taken from tycho.yaml, never from raw payloads."""

    source: Label
    kind: Label
    target: Annotated[str, StringConstraints(min_length=1, max_length=400)]


class WatcherStatus(Model):
    source: Label
    kind: Label
    target: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    last_observed_at: datetime | None = None
    observation_count: int = Field(default=0, ge=0)


class LatestChange(Model):
    delta_id: DeltaId
    statement: Text
    category: Label | None = None
    scope: Label | None = None
    source: Label
    source_family: Label
    observed_at: datetime
    change_count: int = Field(ge=1)


class NotableClaim(Model):
    claim_id: ClaimId
    version: int = Field(ge=1)
    statement: Text
    scope: Label
    confidence: Label
    severity: Label
    stale: bool
    last_verified_at: datetime


class EntityCard(Model):
    entity: Label
    name: Label
    description: Text
    latest_change: LatestChange | None = None
    active_fact_count: int = Field(ge=0)
    active_claim_count: int = Field(ge=0)
    last_observed_at: datetime | None = None
    last_verified_at: datetime | None = None
    notable_claim: NotableClaim | None = None
    stale: bool = False
    disputed: bool = False
    watchers: list[WatcherStatus] = Field(default_factory=list, max_length=8)
    waiting_for: ShortText | None = None


class OverviewTotals(Model):
    active_claims: int = Field(ge=0)
    retired_claims: int = Field(ge=0)
    superseded_claims: int = Field(ge=0)
    canonical_deltas: int = Field(ge=0)
    meaningful_deltas: int = Field(ge=0)
    noise_deltas: int = Field(ge=0)
    observations: int = Field(ge=0)


class OverviewResponse(Model):
    generated_at: datetime
    entities: list[EntityCard] = Field(max_length=4)
    totals: OverviewTotals


# --- Belief timeline --------------------------------------------------------


class LifecycleKind(StrEnum):
    CREATED = "created"
    VERIFIED = "verified"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class ClaimPin(Model):
    """Every timeline event pins an exact (claim_id, version)."""

    claim_id: ClaimId
    version: int = Field(ge=1)
    entity: Label
    scope: Label
    claim_class: Label
    statement: Text
    confidence: Label
    severity: Label
    status: Label
    stale: bool


class EvidenceChip(Model):
    delta_id: DeltaId
    source: Label
    source_family: Label
    canonical: bool


class TimelineEvent(Model):
    event_id: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    kind: LifecycleKind
    at: datetime
    claim: ClaimPin
    replacement: ClaimPin | None = None
    evidence: list[EvidenceChip] = Field(default_factory=list, max_length=16)
    note: ShortText | None = None


class TimelineResponse(Model):
    entity: Label
    scope: Label | None = None
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    next_offset: int | None = None
    events: list[TimelineEvent] = Field(max_length=200)


# --- Provenance -------------------------------------------------------------


class ObservationRef(Model):
    obs_id: ObservationId
    role: Label
    fetched_at: datetime | None = None
    kind: Label | None = None
    status: Label | None = None
    resolved: bool


class GroundedChange(Model):
    category: Label | None = None
    scope: Label | None = None
    statement: Text
    before: Quote | None = None
    after: Quote | None = None
    quote_before: Quote | None = None
    quote_after: Quote | None = None


class DeltaEvidence(Model):
    delta_id: DeltaId
    entity: Label
    source: Label
    source_family: Label
    computed_at: datetime
    triage: Label
    summary: Text
    generated_by: Label
    prompt_version: Label
    changes: list[GroundedChange] = Field(default_factory=list, max_length=8)
    observations: list[ObservationRef] = Field(default_factory=list, max_length=2)
    source_ref: SourceRef | None = None
    admissible: bool
    defect: ShortText | None = None


class LifecycleLinks(Model):
    supersedes: ClaimId | None = None
    superseded_by: ClaimId | None = None
    disputes: ClaimId | None = None
    disputed_by: list[ClaimId] = Field(default_factory=list, max_length=8)


class HistoryEntry(Model):
    """One bounded, allowlisted row of a claim's embedded history."""

    at: datetime | None = None
    event: Label | None = None
    action: Label | None = None
    actor: Label | None = None
    reason: ShortText | None = None
    version: int | None = Field(default=None, ge=1)
    status: Label | None = None
    delta_ids: list[DeltaId] = Field(default_factory=list, max_length=16)


class ProvenanceResponse(Model):
    claim: ClaimPin
    requested_version: int = Field(ge=1)
    current_version: int = Field(ge=1)
    exact_version: bool
    reconstruction_note: ShortText | None = None
    rationale: Text
    created_at: datetime
    last_verified_at: datetime
    created_by: Label
    staleness_days: int = Field(ge=0)
    lifecycle: LifecycleLinks
    history: list[HistoryEntry] = Field(default_factory=list, max_length=20)
    evidence: list[DeltaEvidence] = Field(default_factory=list, max_length=16)


# --- Strategy ---------------------------------------------------------------


class PremiseChip(Model):
    claim_id: ClaimId
    claim_version: int = Field(ge=1)
    delta_ids: list[DeltaId] = Field(default_factory=list, max_length=16)
    entity: Label | None = None
    scope: Label | None = None
    statement: Text | None = None
    confidence: Label | None = None
    resolved: bool


class CardView(Model):
    card_id: CardId
    statement: Text
    rationale: Text
    confidence: Label
    competing_explanation: Text
    falsifier: Text
    entities: list[Label] = Field(default_factory=list, max_length=8)
    scopes: list[Label] = Field(default_factory=list, max_length=8)
    source_families: list[Label] = Field(default_factory=list, max_length=16)
    premises: list[PremiseChip] = Field(default_factory=list, max_length=5)
    limitations: list[ShortText] = Field(default_factory=list, max_length=5)
    status: Label
    rejection_reasons: list[ShortText] = Field(default_factory=list, max_length=12)
    challenger_verdict: Label | None = None
    challenger_reasons: list[ShortText] = Field(default_factory=list, max_length=12)


class BriefView(Model):
    brief_id: BriefId
    period_from: datetime
    period_to: datetime
    created_at: datetime
    rendered_md: Markdown
    claims_referenced: list[ClaimPin] = Field(default_factory=list, max_length=200)
    stats_new: int = Field(ge=0)
    stats_superseded: int = Field(ge=0)
    stats_confidence_changes: int = Field(ge=0)
    stats_stale_flagged: int = Field(ge=0)
    empty: bool


class SessionMetricsView(Model):
    cards_proposed: int = Field(ge=0)
    cards_passed: int = Field(ge=0)
    cards_rejected: int = Field(ge=0)
    challenges: int = Field(ge=0)
    manifest_entries: int = Field(ge=0)
    input_bytes: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)


class SessionView(Model):
    session_id: SessionId
    state: Label
    question: Text
    period_from: datetime
    period_to: datetime
    created_at: datetime
    updated_at: datetime
    strategy_version: Label
    manifest_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    agent_versions: dict[Label, Label]
    model_versions: dict[Label, Label]
    metrics: SessionMetricsView
    error: ShortText | None = None
    brief_id: BriefId | None = None


class StrategySessionResponse(Model):
    session: SessionView | None = None
    brief: BriefView | None = None
    passed_cards: list[CardView] = Field(default_factory=list, max_length=3)
    rejected_cards: list[CardView] = Field(default_factory=list, max_length=12)
    waiting_for: ShortText | None = None


# --- Agent activity and SSE -------------------------------------------------


class ActivityKind(StrEnum):
    """The closed set of safe activity/SSE events.

    Structure only: an agent name, a state, a count, or an ID.  There is no
    member here that could carry a prompt, a model response, a grounded quote,
    a tool argument, or raw source content.
    """

    RUN_STARTED = "run_started"
    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    CARD_REJECTED = "card_rejected"
    BRIEF_COMPLETED = "brief_completed"
    RUN_FAILED = "run_failed"
    HEARTBEAT = "heartbeat"


class ActivityEvent(Model):
    """One structural step.  Every field is an ID, an enum, or a count."""

    seq: int = Field(ge=0)
    event: ActivityKind
    at: datetime
    session_id: SessionId | None = None
    run_id: RunId | None = None
    agent: Label | None = None
    state: Label | None = None
    card_id: CardId | None = None
    brief_id: BriefId | None = None
    card_count: int = Field(default=0, ge=0)
    passed_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    reason_count: int = Field(default=0, ge=0)
    reason_classes: list[Label] = Field(default_factory=list, max_length=8)
    claim_versions: list[Label] = Field(default_factory=list, max_length=200)
    failure_class: Label | None = None
    derived: bool = True


class ActivityResponse(Model):
    session_id: SessionId
    events: list[ActivityEvent] = Field(max_length=64)
    derived_from: Label


# --- Strategy trigger -------------------------------------------------------


class RunState(StrEnum):
    DISPATCHING = "dispatching"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TriggerResponse(Model):
    run_id: RunId
    state: RunState
    duplicate: bool
    session_id: SessionId | None = None
    brief_id: BriefId | None = None
    period_from: datetime
    period_to: datetime
    stream_path: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    detail: ShortText


class ErrorResponse(Model):
    error: Label
    detail: ShortText
