"""The bounded strategy-council workflow.

    bounded context builder (Python)
      -> tycho_strategist
      -> hard proposal validation
      -> tycho_challenger
      -> hard challenge gate
      -> tycho_brief_writer
      -> citation validation + write-once persistence

There is exactly one Strategist pass and one Challenger pass.  Python owns every
gate: a Challenger ``pass`` cannot revive a card that failed a hard check, and a
rejected card stays visible in the session audit but never reaches the brief.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pipeline.strategy_context import (
    StrategyContext,
    StrategyContextStore,
    build_strategy_context,
    default_period,
)
from pipeline.strategy_evidence import (
    CardValidation,
    StrategyEvidenceStore,
    validate_card_draft,
)
from pipeline.strategy_lease import StrategyLeaseDecision
from schemas.brief import Brief, BriefPeriod, BriefStats, ClaimReference
from schemas.claim import Claim, ClaimStatus
from schemas.common import new_prefixed_id
from schemas.config import TychoConfig
from schemas.strategy import (
    BRIEF_WRITER_VERSION,
    CHALLENGER_VERSION,
    MAX_CARDS_PER_SESSION,
    STRATEGIST_VERSION,
    STRATEGY_QUESTION,
    STRATEGY_VERSION,
    AgentVersions,
    BriefDraft,
    CardStatus,
    ChallengeResult,
    ChallengeVerdict,
    ModelVersions,
    PremiseReference,
    SessionMetrics,
    SessionPeriod,
    SessionState,
    StrategyCard,
    StrategyCardDraft,
    StrategyProposal,
    StrategySession,
    manifest_hash,
)
from strategy_agent.agents import (
    BRIEF_WRITER_NAME,
    CHALLENGER_NAME,
    DEFAULT_STRATEGY_MODEL,
    STRATEGIST_NAME,
    build_brief_writer,
    build_challenger,
    build_strategist,
)
from strategy_agent.citations import CitationError, replace_citations
from strategy_agent.errors import Stage, safe_error_text
from strategy_agent.events import StrategyEvent
from strategy_agent.invoker import AgentInvoker

STRATEGY_LEASE_SECONDS = 1_800

_EMPTY_BRIEF_NOTE = (
    "No defensible cross-entity pattern survived validation for this period. "
    "Tycho publishes nothing rather than manufacture a conclusion."
)


class StrategySessionStore(StrategyContextStore, StrategyEvidenceStore, Protocol):
    """The complete store surface one strategy session uses."""

    def acquire_strategy_lease(
        self,
        period_from: datetime,
        period_to: datetime,
        strategy_version: str,
        session_id: str,
        started_at: datetime,
        lease_expires_at: datetime,
    ) -> StrategyLeaseDecision: ...

    def complete_strategy_lease(
        self,
        period_from: datetime,
        period_to: datetime,
        strategy_version: str,
        session_id: str,
        finished_at: datetime,
    ) -> None: ...

    def fail_strategy_lease(
        self,
        period_from: datetime,
        period_to: datetime,
        strategy_version: str,
        session_id: str,
        finished_at: datetime,
        error: str,
    ) -> None: ...

    def begin_strategy_session(
        self,
        session: StrategySession,
        lease_expires_at: datetime,
    ) -> StrategyLeaseDecision: ...

    def create_strategy_session(self, session: StrategySession) -> None: ...

    def finalize_strategy_session(self, session: StrategySession) -> None: ...

    def commit_strategy_session(
        self,
        session: StrategySession,
        brief: Brief | None,
        finished_at: datetime,
    ) -> None: ...

    def get_strategy_session(self, session_id: str) -> StrategySession | None: ...

    def create_brief_once(self, brief: Brief) -> bool: ...

    def get_brief(self, brief_id: str) -> Brief | None: ...


@dataclass
class StrategySessionResult:
    session: StrategySession
    brief: Brief | None = None
    events: list[StrategyEvent] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None


def stage_for(exc: BaseException) -> Stage:
    """Map an exception to the workflow stage that raised it."""
    return {
        "StrategyContextTooLarge": Stage.CONTEXT,
        "CitationError": Stage.CITATION,
        "SessionPersistenceError": Stage.PERSISTENCE,
        "StrategyModelError": Stage.STRATEGIST,
        "ValidationError": Stage.PROPOSAL_VALIDATION,
    }.get(type(exc).__name__, Stage.UNKNOWN)


def brief_id_for(period: SessionPeriod, session_id: str | None = None) -> str:
    """Weekly brief identity, made unique per session.

    The period alone is not a safe key: a retry after a failed session covers
    the same week and would collide with the abandoned attempt's brief. The
    session discriminator keeps every attempt's brief distinct and write-once.
    """
    year, week, _ = period.to.isocalendar()
    base = f"brf_{year:04d}w{week:02d}"
    if session_id is None:
        return base
    return f"{base}-{session_id.removeprefix('sts_')[-8:].lower()}"


def _card_from_validation(
    draft: StrategyCardDraft, validation: CardValidation, *, card_id: str
) -> StrategyCard:
    """Build the card from recomputed evidence, not from what the model said."""
    status = CardStatus.PROPOSED if validation.passed else CardStatus.REJECTED
    premises = [
        PremiseReference(
            claim_id=premise.claim.claim_id,
            claim_version=premise.claim.version,
            delta_ids=premise.delta_ids,
        )
        for premise in validation.premises
    ]
    resolved_claims = {premise.claim.claim_id for premise in validation.premises}
    # Keep an unresolvable premise visible, with no Delta IDs, so the session
    # audit shows exactly what the Strategist cited and why it did not stand.
    premises.extend(
        PremiseReference(claim_id=item.claim_id, claim_version=item.claim_version)
        for item in draft.premises
        if item.claim_id not in resolved_claims
    )
    return StrategyCard(
        card_id=card_id,
        statement=draft.statement,
        rationale=draft.rationale,
        confidence=draft.confidence if validation.passed else validation.confidence,
        competing_explanation=draft.competing_explanation,
        falsifier=draft.falsifier,
        entities=validation.entities or ["unresolved"],
        scopes=validation.scopes or ["identity"],
        source_families=validation.source_families or ["unresolved"],
        premises=premises,
        limitations=validation.limitations,
        status=status,
        rejection_reasons=validation.violations[:12],
    )


def _restatus(card: StrategyCard, **update: Any) -> StrategyCard:
    """Re-validate on every status change so passed-card invariants always run.

    ``model_copy`` would skip the validators, which is exactly where a card with
    too few entities or an unlabelled limitation could slip through.
    """
    return StrategyCard.model_validate({**card.model_dump(mode="json"), **update})


def _reject(card: StrategyCard, reasons: list[str]) -> StrategyCard:
    combined = [*card.rejection_reasons, *reasons][:12]
    return _restatus(card, status=CardStatus.REJECTED.value, rejection_reasons=combined)


def _pinned_claim_view(claim: Claim, delta_ids: list[str]) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "claim_version": claim.version,
        "entity": claim.entity,
        "scope": claim.scope,
        "class": claim.class_.value,
        "statement": claim.statement,
        "rationale": claim.rationale,
        "confidence": claim.confidence.value,
        "severity": claim.severity.value,
        "last_verified_at": claim.last_verified_at.isoformat(),
        "delta_ids": list(delta_ids),
    }


def build_challenge_request(
    card: StrategyCard, validation: CardValidation, context: StrategyContext
) -> str:
    """The Challenger sees the card, its pinned premises, and the hard results."""
    payload = {
        "question": STRATEGY_QUESTION,
        "card": card.model_dump(mode="json"),
        "pinned_premise_claims": [
            _pinned_claim_view(premise.claim, premise.delta_ids)
            for premise in validation.premises
        ],
        "canonical_deltas": [
            {
                "delta_id": delta.delta_id,
                "entity": delta.entity,
                "source": delta.source,
                "triage": delta.triage.value,
                "computed_at": delta.computed_at.isoformat(),
                "changes": [
                    {
                        "category": change.category.value if change.category else None,
                        "scope": change.scope.value if change.scope else None,
                        "statement": change.statement,
                    }
                    for change in delta.changes
                ],
            }
            for delta in sorted(
                {
                    delta.delta_id: delta
                    for premise in validation.premises
                    for delta in premise.deltas
                }.values(),
                key=lambda item: item.delta_id,
            )
        ],
        "deterministic_policy_checks": {
            "hard_checks_passed": validation.passed,
            "distinct_entities": len(validation.entities),
            "distinct_source_families": len(validation.source_families),
            "stale_premise_claim_ids": list(validation.stale_claim_ids),
            "confidence_ceiling": validation.confidence.value,
        },
        "period_metrics": [metric.model_dump(mode="json") for metric in context.metrics],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def build_brief_request(
    cards: list[StrategyCard],
    validations: dict[str, CardValidation],
    context: StrategyContext,
    stats: BriefStats,
) -> str:
    """The Brief Writer sees passed cards only; rejected ones never reach it."""
    payload = {
        "period": {
            "from": context.period.from_.isoformat(),
            "to": context.period.to.isoformat(),
        },
        "passed_cards": [card.model_dump(mode="json") for card in cards],
        "pinned_premise_claims": [
            _pinned_claim_view(premise.claim, premise.delta_ids)
            for card in cards
            for premise in validations[card.card_id].premises
        ],
        "period_statistics": stats.model_dump(mode="json"),
        "period_metrics": [metric.model_dump(mode="json") for metric in context.metrics],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_brief_stats(
    store: StrategySessionStore, context: StrategyContext
) -> BriefStats:
    """Deterministic period statistics, recomputed rather than model-reported."""
    period = context.period
    new = 0
    confidence_changes = 0
    for entry in context.manifest:
        claim = store.get_claim(entry.claim_id)
        if claim is None:
            continue
        if period.from_ <= claim.created_at < period.to:
            new += 1
            if claim.supersedes:
                previous = store.get_claim(claim.supersedes)
                if previous is not None and previous.confidence is not claim.confidence:
                    confidence_changes += 1
    superseded = sum(
        1
        for claim in store.list_claims()
        if claim.status is ClaimStatus.SUPERSEDED
        and period.from_ <= claim.last_verified_at < period.to
    )
    return BriefStats(
        new=new,
        superseded=superseded,
        confidence_changes=confidence_changes,
        stale_flagged=sum(1 for entry in context.manifest if entry.stale),
    )


def render_brief_markdown(draft: BriefDraft | None, period: SessionPeriod) -> str:
    """Assemble the four fixed sections; an empty session still renders one."""
    year, week, _ = period.to.isocalendar()
    header = f"# Tycho strategy brief — {year:04d}-W{week:02d}"
    if draft is None:
        return "\n\n".join(
            [
                header,
                "## What changed",
                _EMPTY_BRIEF_NOTE,
                "## What Tycho concludes",
                "No strategy card passed validation for this period.",
                "## Counter-signals",
                "Not applicable: there is no conclusion to counter.",
                "## What would change our mind",
                "New canonical evidence from at least two entities and two "
                "independent source families within one bounded period.",
            ]
        )
    return "\n\n".join(
        [
            header,
            "## What changed",
            draft.what_changed.strip(),
            "## What Tycho concludes",
            draft.what_tycho_concludes.strip(),
            "## Counter-signals",
            draft.counter_signals.strip(),
            "## What would change our mind",
            draft.what_would_change_our_mind.strip(),
        ]
    )


async def run_strategy_session(
    store: StrategySessionStore,
    config: TychoConfig,
    invoker: AgentInvoker,
    *,
    period: SessionPeriod | None = None,
    now: datetime | None = None,
    model: str = DEFAULT_STRATEGY_MODEL,
    strategy_version: str = STRATEGY_VERSION,
) -> StrategySessionResult:
    """Run one bounded strategy session and persist it write-once."""
    started_at = now or datetime.now(UTC)
    window = period or default_period(started_at)
    session_id = new_prefixed_id("sts")
    model_versions = ModelVersions(strategist=model, challenger=model, brief_writer=model)

    # The placeholder is deliberately empty: an unbuilt context has no manifest
    # and no metrics yet.  It exists so that the lease and a READABLE running
    # session appear together, before any context work or agent call.
    placeholder = StrategySession(
        session_id=session_id,
        strategy_version=strategy_version,
        question=STRATEGY_QUESTION,
        period=window,
        input_manifest=[],
        manifest_hash=manifest_hash([]),
        metrics_evidence=[],
        agent_versions=AgentVersions(
            strategist=STRATEGIST_VERSION,
            challenger=CHALLENGER_VERSION,
            brief_writer=BRIEF_WRITER_VERSION,
        ),
        model_versions=model_versions,
        state=SessionState.RUNNING,
        metrics=SessionMetrics(),
        created_at=started_at,
        updated_at=started_at,
    )
    lease = store.begin_strategy_session(
        placeholder, started_at + timedelta(seconds=STRATEGY_LEASE_SECONDS)
    )
    if lease.state != "acquired":
        # A duplicate weekly trigger and a dashboard click land on the same
        # identity.  Return the existing session; never start a second run.
        existing = (
            store.get_strategy_session(lease.session_id) if lease.session_id else None
        )
        reason = (
            "session already completed"
            if lease.state == "completed"
            else "another strategy session is active"
        )
        if existing is None:
            raise RuntimeError(f"strategy lease is {lease.state} with no readable session")
        return StrategySessionResult(
            session=existing, brief=None, events=[], skipped=True, skip_reason=reason
        )

    events: list[StrategyEvent] = []
    session: StrategySession = placeholder
    try:
        # 1. Bounded context. An oversized period fails here, before any model,
        #    and the placeholder session is already durable so the failure is
        #    visible and retryable.
        context = build_strategy_context(store, config, period=window, now=started_at)
        # Enrich the in-memory session; the durable document is rewritten by the
        # single atomic commit at the end of the run.
        session = placeholder.model_copy(
            update={
                "input_manifest": context.manifest,
                "manifest_hash": context.manifest_hash,
                "metrics_evidence": context.metrics,
                "metrics": SessionMetrics(
                    input_bytes=context.input_bytes,
                    estimated_input_tokens=context.estimated_input_tokens,
                ),
            }
        )
        events.append(
            StrategyEvent(
                session_id=session_id,
                agent="strategy_context",
                state="completed",
                card_count=0,
                claim_versions=[
                    f"{entry.claim_id}@{entry.claim_version}" for entry in context.manifest
                ],
            )
        )

        result = await _run_council(
            store, config, invoker, context, session, model=model, now=started_at
        )
        result.events = [*events, *result.events]
        return result
    except Exception as exc:
        finished_at = datetime.now(UTC)
        # Never persist str(exc): Pydantic renders input_value, which can carry
        # model output, claim text, or a grounded quote.
        message = safe_error_text(exc, stage_for(exc))
        failed = session.model_copy(
            update={
                "state": SessionState.FAILED,
                "error": message,
                "updated_at": finished_at,
            }
        )
        # One transaction: the session goes failed and the lease is released for
        # retry together, or neither does.  A context failure lands here too,
        # marking the placeholder rather than losing the attempt.
        store.commit_strategy_session(failed, None, finished_at)
        raise


async def _run_council(
    store: StrategySessionStore,
    config: TychoConfig,
    invoker: AgentInvoker,
    context: StrategyContext,
    session: StrategySession,
    *,
    model: str,
    now: datetime,
) -> StrategySessionResult:
    events: list[StrategyEvent] = []
    run_ids: list[str] = []
    tokens = {"input": 0, "output": 0, "total": 0, "latency": 0}

    def _account(invocation: Any) -> None:
        run_ids.append(invocation.run_id)
        tokens["input"] += invocation.input_tokens
        tokens["output"] += invocation.output_tokens
        tokens["total"] += invocation.total_tokens
        tokens["latency"] += invocation.latency_ms

    # 2. Strategist: one pass, at most three drafts.
    strategist_run = f"{session.session_id}:strategist"
    invocation = await invoker.invoke(
        build_strategist(model), context.document, run_id=strategist_run
    )
    _account(invocation)
    proposal = StrategyProposal.model_validate(invocation.payload)
    events.append(
        StrategyEvent(
            session_id=session.session_id,
            agent=STRATEGIST_NAME,
            state="completed",
            card_count=len(proposal.cards),
            run_id=invocation.run_id,
            input_tokens=invocation.input_tokens,
            output_tokens=invocation.output_tokens,
            total_tokens=invocation.total_tokens,
            latency_ms=invocation.latency_ms,
        )
    )

    # 3. Hard proposal validation. Invalid cards are rejected before the
    #    Challenger ever sees them.
    cards: list[StrategyCard] = []
    validations: dict[str, CardValidation] = {}
    for draft in proposal.cards:
        validation = validate_card_draft(draft, store, config, now)
        card = _card_from_validation(draft, validation, card_id=new_prefixed_id("stc"))
        cards.append(card)
        validations[card.card_id] = validation

    # 4. Challenger: one pass per surviving card.
    challenges: list[ChallengeResult] = []
    for index, card in enumerate(cards):
        if card.status is not CardStatus.PROPOSED:
            continue
        challenge_run = f"{session.session_id}:challenger:{index}"
        invocation = await invoker.invoke(
            build_challenger(model),
            build_challenge_request(card, validations[card.card_id], context),
            run_id=challenge_run,
        )
        _account(invocation)
        challenge = ChallengeResult.model_validate({**invocation.payload, "card_id": card.card_id})
        challenges.append(challenge)
        events.append(
            StrategyEvent(
                session_id=session.session_id,
                agent=CHALLENGER_NAME,
                state=challenge.verdict.value,
                card_count=1,
                run_id=invocation.run_id,
                input_tokens=invocation.input_tokens,
                output_tokens=invocation.output_tokens,
                total_tokens=invocation.total_tokens,
                latency_ms=invocation.latency_ms,
            )
        )

        # 5. Hard challenge gate. A Challenger pass is quality control, not
        #    evidence: it can only reject, never revive.
        if challenge.verdict is ChallengeVerdict.FAIL:
            reasons = [
                *(
                    f"challenger: unsupported premise {claim_id}"
                    for claim_id in challenge.unsupported_premise_claim_ids
                ),
                *(f"challenger: {violation}" for violation in challenge.policy_violations),
            ]
            cards[index] = _reject(card, reasons or ["challenger rejected the conclusion"])
        else:
            cards[index] = _restatus(card, status=CardStatus.PASSED.value)

    # 6. At most three cards survive one session; the rest are rejected, and
    #    stay visible in the audit.
    passed_indexes = [
        index for index, card in enumerate(cards) if card.status is CardStatus.PASSED
    ]
    for index in passed_indexes[MAX_CARDS_PER_SESSION:]:
        cards[index] = _reject(cards[index], ["session card limit reached"])
    passed = [card for card in cards if card.status is CardStatus.PASSED]
    rejected = [card for card in cards if card.status is CardStatus.REJECTED]

    # 7. Brief Writer: passed cards only. Zero passed cards is a valid result
    #    and renders a deterministic empty brief without a model call.
    stats = compute_brief_stats(store, context)
    draft: BriefDraft | None = None
    if passed:
        writer_run = f"{session.session_id}:brief_writer"
        invocation = await invoker.invoke(
            build_brief_writer(model),
            build_brief_request(passed, validations, context, stats),
            run_id=writer_run,
        )
        _account(invocation)
        draft = BriefDraft.model_validate(invocation.payload)
        events.append(
            StrategyEvent(
                session_id=session.session_id,
                agent=BRIEF_WRITER_NAME,
                state="completed",
                card_count=len(passed),
                passed_count=len(passed),
                run_id=invocation.run_id,
                input_tokens=invocation.input_tokens,
                output_tokens=invocation.output_tokens,
                total_tokens=invocation.total_tokens,
                latency_ms=invocation.latency_ms,
            )
        )

    # 8. Citation validation, then write-once persistence.
    pinned = {
        (premise.claim_id, premise.claim_version)
        for card in passed
        for premise in card.premises
    }
    rendered = render_brief_markdown(draft, context.period)
    citation_result = replace_citations(rendered, pinned)
    cited = {(marker.claim_id, marker.version) for marker in citation_result.citations}
    unknown = cited - pinned
    if unknown:
        raise CitationError(f"brief cites unpinned claim versions: {sorted(unknown)}")

    finished_at = datetime.now(UTC)
    brief = Brief(
        brief_id=brief_id_for(context.period, session.session_id),
        period=BriefPeriod(**{"from": context.period.from_, "to": context.period.to}),
        claims_referenced=[
            ClaimReference(claim_id=claim_id, version=version)
            for claim_id, version in sorted(pinned)
        ],
        stats=stats,
        rendered_md=citation_result.rendered,
        delivered_to=[],
        created_at=finished_at,
        strategy_session_id=session.session_id,
        strategy_card_ids=[card.card_id for card in passed],
    )
    completed = session.model_copy(
        update={
            "cards": cards,
            "challenges": challenges,
            "state": SessionState.COMPLETED,
            "brief_id": brief.brief_id,
            "run_ids": run_ids[:8],
            "metrics": SessionMetrics(
                cards_proposed=len(cards),
                cards_passed=len(passed),
                cards_rejected=len(rejected),
                input_bytes=context.input_bytes,
                estimated_input_tokens=context.estimated_input_tokens,
                input_tokens=tokens["input"],
                output_tokens=tokens["output"],
                total_tokens=tokens["total"],
                latency_ms=tokens["latency"],
            ),
            "updated_at": finished_at,
        }
    )
    # Brief, terminal session state, and lease completion land as ONE atomic
    # store operation.  A crash between them would otherwise leave a stored
    # brief whose session is still running: a state that blocks every retry.
    store.commit_strategy_session(completed, brief, finished_at)
    events.append(
        StrategyEvent(
            session_id=session.session_id,
            agent="strategy_council",
            state="completed",
            card_count=len(cards),
            passed_count=len(passed),
            rejection_count=len(rejected),
            claim_versions=[f"{claim_id}@{version}" for claim_id, version in sorted(pinned)],
        )
    )
    return StrategySessionResult(session=completed, brief=brief, events=events)
