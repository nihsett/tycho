"""Explicit read/query methods for the dashboard.

Route handlers call named methods here; they never write a database query
themselves.  Every rule the handoff asks for lives in this module:

- canonical ``delta@2`` only, through :mod:`dashboard.api.source`;
- exact claim version / history reconstruction;
- deterministic active / stale / disputed / superseded counts;
- strategy and brief data loaded by immutable ID;
- stable ordering and bounded pagination.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pipeline.strategy_evidence import (
    claim_is_stale,
    evidence_defect,
    source_family,
    staleness_threshold,
)
from schemas.claim import Claim, ClaimClass, ClaimStatus
from schemas.config import TychoConfig
from schemas.delta import Delta, Triage
from schemas.strategy import CardStatus, SessionState, StrategySession

from dashboard.api.activity import derive_activity
from dashboard.api.models import (
    ActivityResponse,
    BriefView,
    CardView,
    ClaimPin,
    ComponentState,
    DeltaEvidence,
    EntityCard,
    EvidenceChip,
    GroundedChange,
    HealthComponent,
    HealthResponse,
    HistoryEntry,
    LatestChange,
    LifecycleKind,
    LifecycleLinks,
    NotableClaim,
    ObservationRef,
    OverviewResponse,
    OverviewTotals,
    PremiseChip,
    ProvenanceResponse,
    SessionMetricsView,
    SessionView,
    SourceRef,
    StrategySessionResponse,
    TimelineEvent,
    TimelineResponse,
    WatcherStatus,
)
from dashboard.api.source import ReadSource

MAX_TIMELINE_LIMIT = 200
DEFAULT_TIMELINE_LIMIT = 50
#: Acquisition is scheduled daily; two missed days is a real signal, not noise.
ACQUISITION_STALE_HOURS = 48
ANALYST_IDLE_DAYS = 14
STRATEGY_IDLE_DAYS = 14


class UnknownResource(LookupError):
    """A requested entity, scope, claim, version, or session does not exist."""


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def source_ref_for(config: TychoConfig, entity: str, source: str) -> SourceRef | None:
    """Resolve a watcher's public target from tycho.yaml, never from a payload."""
    entity_config = config.entities.get(entity)
    if entity_config is None:
        return None
    configured = getattr(entity_config.sources, source, None)
    if configured is None:
        return None
    repo = getattr(configured, "repo", None)
    if repo:
        return SourceRef(source=source, kind="repository", target=f"https://github.com/{repo}")
    url = getattr(configured, "url", None)
    if url:
        return SourceRef(source=source, kind="url", target=str(url))
    handle = getattr(configured, "handle", None)
    if handle:
        return SourceRef(source=source, kind="handle", target=str(handle))
    return None


@dataclass(frozen=True)
class FleetSnapshot:
    """One consistent read of governed memory, assembled once per request."""

    generated_at: datetime
    claims: list[Claim]
    deltas: list[Delta]
    watchers: list[dict[str, Any]]

    def claims_by_id(self) -> dict[str, Claim]:
        return {claim.claim_id: claim for claim in self.claims}

    def deltas_by_id(self) -> dict[str, Delta]:
        return {delta.delta_id: delta for delta in self.deltas}

    def disputed_targets(self) -> set[str]:
        """A claim's disputed badge is derived from active inbound links."""
        return {
            claim.disputes
            for claim in self.claims
            if claim.disputes and claim.status is ClaimStatus.ACTIVE
        }

    def disputers(self) -> dict[str, list[str]]:
        inbound: dict[str, list[str]] = defaultdict(list)
        for claim in self.claims:
            if claim.disputes and claim.status is ClaimStatus.ACTIVE:
                inbound[claim.disputes].append(claim.claim_id)
        return {key: sorted(value) for key, value in inbound.items()}


class ReadModel:
    """Every dashboard query, named after the product concept it serves."""

    def __init__(self, source: ReadSource, config: TychoConfig) -> None:
        self._source = source
        self._config = config

    # --- Shared loading ----------------------------------------------------

    def snapshot(self, *, now: datetime | None = None) -> FleetSnapshot:
        return FleetSnapshot(
            generated_at=now or datetime.now(UTC),
            claims=self._source.list_claims(),
            deltas=self._source.list_canonical_deltas(),
            watchers=self._source.watcher_activity(),
        )

    def entities(self) -> list[str]:
        return list(self._config.entities)

    def scopes(self) -> list[str]:
        return list(self._config.ontology)

    def _stale(self, claim: Claim, now: datetime) -> bool:
        return claim_is_stale(claim, self._config, now)

    def _pin(self, claim: Claim, now: datetime) -> ClaimPin:
        return ClaimPin(
            claim_id=claim.claim_id,
            version=claim.version,
            entity=claim.entity,
            scope=claim.scope,
            claim_class=claim.class_.value,
            statement=_clip(claim.statement, 1_200) or claim.claim_id,
            confidence=claim.confidence.value,
            severity=claim.severity.value,
            status=claim.status.value,
            stale=self._stale(claim, now),
        )

    def _chips(self, claim: Claim, deltas: dict[str, Delta]) -> list[EvidenceChip]:
        chips: list[EvidenceChip] = []
        for item in claim.evidence[:16]:
            delta = deltas.get(item.delta_id)
            chips.append(
                EvidenceChip(
                    delta_id=item.delta_id,
                    source=item.source,
                    source_family=source_family(claim.entity, item.source),
                    canonical=delta is not None
                    and evidence_defect(claim, item, delta) is None,
                )
            )
        return chips

    # --- Health ------------------------------------------------------------

    def health(self, snapshot: FleetSnapshot) -> HealthResponse:
        now = snapshot.generated_at
        components: list[HealthComponent] = []

        last_fetch = max(
            (dt for row in snapshot.watchers if (dt := _aware(row["last_fetched_at"]))),
            default=None,
        )
        observations = sum(int(row["observation_count"]) for row in snapshot.watchers)
        watcher_count = len(snapshot.watchers)
        acquisition_state = ComponentState.UNKNOWN
        if last_fetch is not None:
            hours = (now - last_fetch).total_seconds() / 3600
            acquisition_state = (
                ComponentState.OK if hours <= ACQUISITION_STALE_HOURS else ComponentState.STALE
            )
        components.append(
            HealthComponent(
                key="acquisition",
                name="Acquisition watchers",
                state=acquisition_state,
                detail=f"{watcher_count} watchers, {observations} observations recorded",
                last_success_at=last_fetch,
                count=watcher_count,
            )
        )

        meaningful = [delta for delta in snapshot.deltas if delta.triage is Triage.MEANINGFUL]
        last_delta = max((delta.computed_at for delta in snapshot.deltas), default=None)
        components.append(
            HealthComponent(
                key="differ",
                name="Gemini semantic differ",
                state=ComponentState.OK if snapshot.deltas else ComponentState.UNKNOWN,
                detail=(
                    f"{len(meaningful)} meaningful and "
                    f"{len(snapshot.deltas) - len(meaningful)} noise canonical Deltas"
                ),
                last_success_at=last_delta,
                count=len(snapshot.deltas),
            )
        )

        active = [claim for claim in snapshot.claims if claim.status is ClaimStatus.ACTIVE]
        last_claim = max((claim.created_at for claim in snapshot.claims), default=None)
        analyst_state = ComponentState.UNKNOWN
        if last_claim is not None:
            days = (now - last_claim).days
            analyst_state = (
                ComponentState.OK if days <= ANALYST_IDLE_DAYS else ComponentState.IDLE
            )
        components.append(
            HealthComponent(
                key="analyst",
                name="Analyst",
                state=analyst_state,
                detail=f"{len(active)} active claims of {len(snapshot.claims)} recorded",
                last_success_at=last_claim,
                count=len(active),
            )
        )

        sessions = sorted(
            self._source.strategy_sessions(), key=lambda item: item.created_at
        )
        completed = [item for item in sessions if item.state is SessionState.COMPLETED]
        latest_completed = completed[-1] if completed else None
        strategy_state = ComponentState.UNKNOWN
        detail = "no strategy session recorded yet"
        if sessions:
            newest = sessions[-1]
            if newest.state is SessionState.RUNNING:
                strategy_state = ComponentState.OK
                detail = "a strategy session is running"
            elif latest_completed is None:
                strategy_state = ComponentState.FAILED
                detail = f"{len(sessions)} sessions recorded, none completed"
            else:
                days = (now - latest_completed.updated_at).days
                strategy_state = (
                    ComponentState.OK if days <= STRATEGY_IDLE_DAYS else ComponentState.IDLE
                )
                detail = (
                    f"{len(completed)} completed of {len(sessions)} sessions; "
                    f"latest passed {latest_completed.metrics.cards_passed} of "
                    f"{latest_completed.metrics.cards_proposed} cards"
                )
        components.append(
            HealthComponent(
                key="strategy",
                name="Strategy Council",
                state=strategy_state,
                detail=detail,
                last_success_at=latest_completed.updated_at if latest_completed else None,
                count=len(completed),
            )
        )

        rank = {
            ComponentState.OK: 0,
            ComponentState.IDLE: 1,
            ComponentState.STALE: 2,
            ComponentState.UNKNOWN: 3,
            ComponentState.FAILED: 4,
        }
        overall = max((item.state for item in components), key=lambda item: rank[item])
        return HealthResponse(generated_at=now, state=overall, components=components)

    # --- Overview ----------------------------------------------------------

    def overview(self, snapshot: FleetSnapshot) -> OverviewResponse:
        now = snapshot.generated_at
        deltas_by_entity: dict[str, list[Delta]] = defaultdict(list)
        for delta in snapshot.deltas:
            deltas_by_entity[delta.entity].append(delta)
        claims_by_entity: dict[str, list[Claim]] = defaultdict(list)
        for claim in snapshot.claims:
            claims_by_entity[claim.entity].append(claim)
        watchers_by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in snapshot.watchers:
            watchers_by_entity[row["entity"]].append(row)
        disputed = snapshot.disputed_targets()

        cards: list[EntityCard] = []
        for entity, entity_config in self._config.entities.items():
            claims = claims_by_entity.get(entity, [])
            active = [claim for claim in claims if claim.status is ClaimStatus.ACTIVE]
            facts = [claim for claim in active if claim.class_ is ClaimClass.FACT]
            entity_deltas = sorted(
                deltas_by_entity.get(entity, []), key=lambda item: item.computed_at
            )
            meaningful = [
                delta for delta in entity_deltas if delta.triage is Triage.MEANINGFUL
            ]
            latest_change = None
            if meaningful:
                newest = meaningful[-1]
                change = newest.changes[0]
                latest_change = LatestChange(
                    delta_id=newest.delta_id,
                    statement=_clip(change.statement, 1_200) or newest.summary,
                    category=change.category.value if change.category else None,
                    scope=change.scope.value if change.scope else None,
                    source=newest.source,
                    source_family=source_family(newest.entity, newest.source),
                    observed_at=newest.computed_at,
                    change_count=len(newest.changes),
                )
            notable = None
            if active:
                ranked = sorted(
                    active,
                    key=lambda claim: (
                        {"critical": 0, "notable": 1, "context": 2}[claim.severity.value],
                        -claim.last_verified_at.timestamp(),
                        claim.claim_id,
                    ),
                )[0]
                notable = NotableClaim(
                    claim_id=ranked.claim_id,
                    version=ranked.version,
                    statement=_clip(ranked.statement, 1_200) or ranked.claim_id,
                    scope=ranked.scope,
                    confidence=ranked.confidence.value,
                    severity=ranked.severity.value,
                    stale=self._stale(ranked, now),
                    last_verified_at=ranked.last_verified_at,
                )
            watcher_rows = watchers_by_entity.get(entity, [])
            watchers = []
            for row in sorted(watcher_rows, key=lambda item: item["source"]):
                ref = source_ref_for(self._config, entity, row["source"])
                watchers.append(
                    WatcherStatus(
                        source=row["source"],
                        kind=ref.kind if ref else "unknown",
                        target=ref.target if ref else "not configured",
                        last_observed_at=_aware(row["last_fetched_at"]),
                        observation_count=int(row["observation_count"]),
                    )
                )
            last_observed = max(
                (item.last_observed_at for item in watchers if item.last_observed_at),
                default=None,
            )
            last_verified = max(
                (claim.last_verified_at for claim in active), default=None
            )
            waiting = None
            if not active and not meaningful:
                waiting = "Watching, but nothing has cleared the evidence bar yet."
            elif not active:
                waiting = (
                    "Meaningful change recorded; no claim from this entity is "
                    "currently active."
                )
            elif not meaningful:
                waiting = "No meaningful canonical change recorded for this entity yet."
            cards.append(
                EntityCard(
                    entity=entity,
                    name=entity_config.name,
                    description=_clip(entity_config.description, 1_200) or entity_config.name,
                    latest_change=latest_change,
                    active_fact_count=len(facts),
                    active_claim_count=len(active),
                    last_observed_at=last_observed,
                    last_verified_at=last_verified,
                    notable_claim=notable,
                    stale=any(self._stale(claim, now) for claim in active),
                    disputed=any(claim.claim_id in disputed for claim in active),
                    watchers=watchers,
                    waiting_for=waiting,
                )
            )

        meaningful_total = sum(
            1 for delta in snapshot.deltas if delta.triage is Triage.MEANINGFUL
        )
        totals = OverviewTotals(
            active_claims=sum(
                1 for claim in snapshot.claims if claim.status is ClaimStatus.ACTIVE
            ),
            retired_claims=sum(
                1 for claim in snapshot.claims if claim.status is ClaimStatus.RETIRED
            ),
            superseded_claims=sum(
                1 for claim in snapshot.claims if claim.status is ClaimStatus.SUPERSEDED
            ),
            canonical_deltas=len(snapshot.deltas),
            meaningful_deltas=meaningful_total,
            noise_deltas=len(snapshot.deltas) - meaningful_total,
            observations=sum(int(row["observation_count"]) for row in snapshot.watchers),
        )
        return OverviewResponse(generated_at=now, entities=cards, totals=totals)

    # --- Belief timeline ---------------------------------------------------

    def timeline(
        self,
        entity: str,
        *,
        scope: str | None = None,
        limit: int = DEFAULT_TIMELINE_LIMIT,
        offset: int = 0,
        now: datetime | None = None,
    ) -> TimelineResponse:
        if entity not in self._config.entities:
            raise UnknownResource(f"unknown entity: {entity}")
        if scope is not None and scope not in set(self._config.ontology):
            raise UnknownResource(f"unknown scope: {scope}")
        limit = max(1, min(int(limit), MAX_TIMELINE_LIMIT))
        offset = max(0, int(offset))

        snapshot = self.snapshot(now=now)
        moment = snapshot.generated_at
        deltas = snapshot.deltas_by_id()
        by_id = snapshot.claims_by_id()
        disputers = snapshot.disputers()

        events: list[TimelineEvent] = []
        for claim in snapshot.claims:
            if claim.entity != entity:
                continue
            if scope is not None and claim.scope != scope:
                continue
            pin = self._pin(claim, moment)
            chips = self._chips(claim, deltas)
            events.append(
                TimelineEvent(
                    event_id=f"{claim.claim_id}:{claim.version}:created",
                    kind=LifecycleKind.CREATED,
                    at=claim.created_at,
                    claim=pin,
                    evidence=chips,
                    note=f"created by {_clip(claim.created_by, 120)}",
                )
            )
            if claim.last_verified_at > claim.created_at and claim.status is ClaimStatus.ACTIVE:
                events.append(
                    TimelineEvent(
                        event_id=f"{claim.claim_id}:{claim.version}:verified",
                        kind=LifecycleKind.VERIFIED,
                        at=claim.last_verified_at,
                        claim=pin,
                        evidence=chips,
                        note="re-observed unchanged; verification clock bumped",
                    )
                )
            for disputer_id in disputers.get(claim.claim_id, []):
                disputer = by_id.get(disputer_id)
                if disputer is None:
                    continue
                events.append(
                    TimelineEvent(
                        event_id=f"{claim.claim_id}:{claim.version}:disputed:{disputer_id}",
                        kind=LifecycleKind.DISPUTED,
                        at=disputer.created_at,
                        claim=pin,
                        replacement=self._pin(disputer, moment),
                        evidence=self._chips(disputer, deltas),
                        note="a conflicting signal disputes this claim; it stays active",
                    )
                )
            if claim.status is ClaimStatus.SUPERSEDED:
                replacement = by_id.get(claim.superseded_by or "")
                at = replacement.created_at if replacement else claim.last_verified_at
                events.append(
                    TimelineEvent(
                        event_id=f"{claim.claim_id}:{claim.version}:superseded",
                        kind=LifecycleKind.SUPERSEDED,
                        at=at,
                        claim=pin,
                        replacement=self._pin(replacement, moment) if replacement else None,
                        evidence=self._chips(replacement, deltas) if replacement else chips,
                        note="publish-before-retire: the replacement claim is linked",
                    )
                )
            if claim.status is ClaimStatus.RETIRED:
                retired_at = None
                reason = None
                for record in claim.history:
                    if record.get("action") == "retired":
                        retired_at = _aware(record.get("at")) or retired_at
                        reason = _clip(str(record.get("reason") or ""), 300) or reason
                events.append(
                    TimelineEvent(
                        event_id=f"{claim.claim_id}:{claim.version}:retired",
                        kind=LifecycleKind.RETIRED,
                        at=retired_at or claim.last_verified_at,
                        claim=pin,
                        evidence=chips,
                        note=reason or "retired from the active belief set",
                    )
                )

        events.sort(key=lambda item: (item.at, item.event_id), reverse=True)
        window = events[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(events) else None
        return TimelineResponse(
            entity=entity,
            scope=scope,
            total=len(events),
            limit=limit,
            offset=offset,
            next_offset=next_offset,
            events=window,
        )

    # --- Provenance --------------------------------------------------------

    def provenance(
        self, claim_id: str, version: int, *, now: datetime | None = None
    ) -> ProvenanceResponse:
        claim = self._source.get_claim(claim_id)
        if claim is None:
            raise UnknownResource(f"unknown claim: {claim_id}")
        moment = now or datetime.now(UTC)

        history = self._history(claim)
        exact = version == claim.version
        note = None
        if not exact:
            recorded = {entry.version for entry in history if entry.version}
            if version not in recorded and version > claim.version:
                raise UnknownResource(f"unknown claim version: {claim_id}@{version}")
            note = (
                f"version {version} is reconstructed from the claim's embedded "
                f"history; the current version is {claim.version}"
            )

        evidence: list[DeltaEvidence] = []
        for item in claim.evidence[:16]:
            delta = self._source.get_delta(item.delta_id)
            defect = evidence_defect(claim, item, delta)
            if delta is None:
                evidence.append(
                    DeltaEvidence(
                        delta_id=item.delta_id,
                        entity=claim.entity,
                        source=item.source,
                        source_family=source_family(claim.entity, item.source),
                        computed_at=claim.created_at,
                        triage="unresolved",
                        summary="This evidence is not present in the canonical Delta table.",
                        generated_by="unresolved",
                        prompt_version="unresolved",
                        observations=[],
                        source_ref=source_ref_for(self._config, claim.entity, item.source),
                        admissible=False,
                        defect=_clip(defect, 300),
                    )
                )
                continue
            evidence.append(
                DeltaEvidence(
                    delta_id=delta.delta_id,
                    entity=delta.entity,
                    source=delta.source,
                    source_family=source_family(delta.entity, delta.source),
                    computed_at=delta.computed_at,
                    triage=delta.triage.value,
                    summary=_clip(delta.summary, 1_200) or delta.delta_id,
                    generated_by=delta.generated_by or "unknown",
                    prompt_version=delta.prompt_version or "unknown",
                    changes=[
                        GroundedChange(
                            category=change.category.value if change.category else None,
                            scope=change.scope.value if change.scope else None,
                            statement=_clip(change.statement, 1_200) or delta.delta_id,
                            before=_clip(change.before, 4_000)
                            if isinstance(change.before, str)
                            else None,
                            after=_clip(change.after, 4_000)
                            if isinstance(change.after, str)
                            else None,
                            quote_before=_clip(
                                change.evidence_before.quote if change.evidence_before else None,
                                4_000,
                            ),
                            quote_after=_clip(
                                change.evidence_after.quote if change.evidence_after else None,
                                4_000,
                            ),
                        )
                        for change in delta.changes[:8]
                    ],
                    observations=self._observations(delta),
                    source_ref=source_ref_for(self._config, delta.entity, delta.source),
                    admissible=defect is None,
                    defect=_clip(defect, 300),
                )
            )

        snapshot_claims = self._source.list_claims()
        disputed_by = sorted(
            other.claim_id
            for other in snapshot_claims
            if other.disputes == claim.claim_id and other.status is ClaimStatus.ACTIVE
        )
        return ProvenanceResponse(
            claim=self._pin(claim, moment),
            requested_version=version,
            current_version=claim.version,
            exact_version=exact,
            reconstruction_note=_clip(note, 300),
            rationale=_clip(claim.rationale, 1_200) or claim.claim_id,
            created_at=claim.created_at,
            last_verified_at=claim.last_verified_at,
            created_by=claim.created_by,
            staleness_days=staleness_threshold(self._config, claim.scope),
            lifecycle=LifecycleLinks(
                supersedes=claim.supersedes,
                superseded_by=claim.superseded_by,
                disputes=claim.disputes,
                disputed_by=disputed_by[:8],
            ),
            history=history,
            evidence=evidence,
        )

    def _observations(self, delta: Delta) -> list[ObservationRef]:
        """Observation metadata only.  The raw GCS payload is never fetched."""
        refs: list[ObservationRef] = []
        for role, obs_id in (("before", delta.obs_before), ("after", delta.obs_after)):
            observation = self._source.get_observation(obs_id)
            refs.append(
                ObservationRef(
                    obs_id=obs_id,
                    role=role,
                    fetched_at=observation.fetched_at if observation else None,
                    kind=observation.kind.value if observation else None,
                    status=observation.status.value if observation else None,
                    resolved=observation is not None,
                )
            )
        return refs

    def _history(self, claim: Claim) -> list[HistoryEntry]:
        """Project the embedded history through a strict field allowlist."""
        entries: list[HistoryEntry] = []
        for record in claim.history[:20]:
            if not isinstance(record, dict):
                continue
            previous = record.get("previous_state")
            previous = previous if isinstance(previous, dict) else {}
            raw_ids = record.get("mapped_v2_delta_ids") or previous.get("evidence_delta_ids") or []
            delta_ids = [
                value
                for value in raw_ids
                if isinstance(value, str) and value.startswith("dlt_")
            ][:16]
            version = previous.get("version")
            entries.append(
                HistoryEntry(
                    at=_aware(record.get("at")),
                    event=_clip(str(record.get("event") or "") or None, 120),
                    action=_clip(str(record.get("action") or "") or None, 120),
                    actor=_clip(str(record.get("actor") or "") or None, 120),
                    reason=_clip(str(record.get("reason") or "") or None, 300),
                    version=int(version) if isinstance(version, int) and version >= 1 else None,
                    status=_clip(str(previous.get("status") or "") or None, 120),
                    delta_ids=delta_ids,
                )
            )
        return entries

    # --- Strategy ----------------------------------------------------------

    def latest_session(self, *, now: datetime | None = None) -> StrategySessionResponse:
        sessions = self._source.strategy_sessions()
        completed = [item for item in sessions if item.state is SessionState.COMPLETED]
        if not completed:
            running = [item for item in sessions if item.state is SessionState.RUNNING]
            waiting = (
                "A strategy session is running; the brief appears when it commits."
                if running
                else "No strategy session has completed yet."
            )
            return StrategySessionResponse(waiting_for=waiting)
        session = max(completed, key=lambda item: (item.updated_at, item.session_id))
        return self.session_view(session, now=now)

    def session(self, session_id: str, *, now: datetime | None = None) -> StrategySessionResponse:
        session = self._source.get_strategy_session(session_id)
        if session is None:
            raise UnknownResource(f"unknown strategy session: {session_id}")
        return self.session_view(session, now=now)

    def session_for_period(
        self, period_from: datetime, period_to: datetime
    ) -> str | None:
        """The session that already covers this bounded period, if any.

        The lease identity is ``(period_from, period_to, strategy_version)``, so
        a trigger inside the same week resolves to whatever this returns.  A
        running session wins over a completed one; a failed session is not
        returned, because the period stays retryable.
        """
        candidates = [
            item
            for item in self._source.strategy_sessions()
            if item.period.from_ == period_from and item.period.to == period_to
        ]
        running = [item for item in candidates if item.state is SessionState.RUNNING]
        if running:
            return max(running, key=lambda item: item.created_at).session_id
        completed = [item for item in candidates if item.state is SessionState.COMPLETED]
        if completed:
            return max(completed, key=lambda item: item.updated_at).session_id
        return None

    def session_view(
        self, session: StrategySession, *, now: datetime | None = None
    ) -> StrategySessionResponse:
        moment = now or datetime.now(UTC)
        brief = None
        if session.brief_id:
            record = self._source.get_brief(session.brief_id)
            if record is not None:
                brief = BriefView(
                    brief_id=record.brief_id,
                    period_from=record.period.from_,
                    period_to=record.period.to,
                    created_at=record.created_at,
                    rendered_md=record.rendered_md[:40_000],
                    claims_referenced=[
                        pin
                        for reference in record.claims_referenced[:200]
                        if (pin := self._pin_reference(reference.claim_id, reference.version, moment))
                    ],
                    stats_new=record.stats.new,
                    stats_superseded=record.stats.superseded,
                    stats_confidence_changes=record.stats.confidence_changes,
                    stats_stale_flagged=record.stats.stale_flagged,
                    empty=not record.strategy_card_ids,
                )
        challenges = {result.card_id: result for result in session.challenges}
        passed = [
            self._card_view(card, challenges, moment) for card in session.passed_cards()
        ]
        rejected = [
            self._card_view(card, challenges, moment) for card in session.rejected_cards()
        ]
        return StrategySessionResponse(
            session=self._session_summary(session),
            brief=brief,
            passed_cards=passed[:3],
            rejected_cards=rejected[:12],
            waiting_for=(
                "No conclusion survived validation for this period."
                if not passed
                else None
            ),
        )

    def _pin_reference(self, claim_id: str, version: int, moment: datetime) -> ClaimPin | None:
        """The claim version this brief pinned, which is what keeps it reproducible.

        The statement carried here is the claim's *current* text. Whether that
        text still belongs to the pinned version is the provenance endpoint's
        question to answer: it reports ``exact_version`` and, when it is false,
        a reconstruction note. This pin exists so a brief can be resolved, not
        so it can be re-read as of the day it was written.
        """
        claim = self._source.get_claim(claim_id)
        if claim is None:
            return None
        pin = self._pin(claim, moment)
        return pin.model_copy(update={"version": version})

    def _session_summary(self, session: StrategySession) -> SessionView:
        return SessionView(
            session_id=session.session_id,
            state=session.state.value,
            question=_clip(session.question, 1_200) or session.session_id,
            period_from=session.period.from_,
            period_to=session.period.to,
            created_at=session.created_at,
            updated_at=session.updated_at,
            strategy_version=session.strategy_version,
            manifest_hash=session.manifest_hash,
            agent_versions=session.agent_versions.model_dump(),
            model_versions=session.model_versions.model_dump(),
            metrics=SessionMetricsView(
                cards_proposed=session.metrics.cards_proposed,
                cards_passed=session.metrics.cards_passed,
                cards_rejected=session.metrics.cards_rejected,
                challenges=len(session.challenges),
                manifest_entries=len(session.input_manifest),
                input_bytes=session.metrics.input_bytes,
                estimated_input_tokens=session.metrics.estimated_input_tokens,
                input_tokens=session.metrics.input_tokens,
                output_tokens=session.metrics.output_tokens,
                total_tokens=session.metrics.total_tokens,
                latency_ms=session.metrics.latency_ms,
            ),
            error=_clip(session.error, 300),
            brief_id=session.brief_id,
        )

    def _card_view(
        self, card: Any, challenges: dict[str, Any], moment: datetime
    ) -> CardView:
        challenge = challenges.get(card.card_id)
        premises: list[PremiseChip] = []
        for premise in card.premises[:5]:
            claim = self._source.get_claim(premise.claim_id)
            premises.append(
                PremiseChip(
                    claim_id=premise.claim_id,
                    claim_version=premise.claim_version,
                    delta_ids=list(premise.delta_ids)[:16],
                    entity=claim.entity if claim else None,
                    scope=claim.scope if claim else None,
                    statement=_clip(claim.statement, 1_200) if claim else None,
                    confidence=claim.confidence.value if claim else None,
                    resolved=claim is not None
                    and claim.version == premise.claim_version
                    and claim.status is ClaimStatus.ACTIVE,
                )
            )
        challenger_reasons: list[str] = []
        if challenge is not None:
            challenger_reasons = [
                *(
                    f"unsupported premise {claim_id}"
                    for claim_id in challenge.unsupported_premise_claim_ids
                ),
                *(str(value) for value in challenge.policy_violations),
            ]
        return CardView(
            card_id=card.card_id,
            statement=_clip(card.statement, 1_200) or card.card_id,
            rationale=_clip(card.rationale, 1_200) or card.card_id,
            confidence=card.confidence.value,
            competing_explanation=_clip(card.competing_explanation, 1_200) or "not recorded",
            falsifier=_clip(card.falsifier, 1_200) or "not recorded",
            entities=list(card.entities)[:8],
            scopes=list(card.scopes)[:8],
            source_families=list(card.source_families)[:16],
            premises=premises,
            limitations=[
                _clip(f"{item.kind.value}: {item.detail}", 300) or item.kind.value
                for item in card.limitations[:5]
            ],
            status=card.status.value,
            rejection_reasons=[
                _clip(reason, 300) or "rejected" for reason in card.rejection_reasons[:12]
            ],
            challenger_verdict=challenge.verdict.value if challenge is not None else None,
            challenger_reasons=[
                _clip(reason, 300) or "rejected" for reason in challenger_reasons[:12]
            ],
        )

    def activity(self, session_id: str) -> ActivityResponse:
        session = self._source.get_strategy_session(session_id)
        if session is None:
            raise UnknownResource(f"unknown strategy session: {session_id}")
        return ActivityResponse(
            session_id=session.session_id,
            events=derive_activity(session)[:64],
            derived_from="persisted strategy session record",
        )

    def card_status_counts(self, session: StrategySession) -> dict[str, int]:
        counts = {status.value: 0 for status in CardStatus}
        for card in session.cards:
            counts[card.status.value] += 1
        return counts
