"""Safe agent-activity events derived from a persisted strategy session.

The Strategy Council's in-flight events are not persisted, so the dashboard does
not pretend to replay them.  It reconstructs a structural timeline from the
session's own immutable record: which agents ran, in the fixed workflow order,
with counts and IDs.  Every event carries ``derived=True`` and the UI says so.

Nothing in here can carry content.  A rejection reason is reduced to a
deterministic *class* name before it becomes an event, so Challenger prose never
reaches the activity timeline even though the Strategy Brief panel shows the
stored reasons in full.
"""

from __future__ import annotations

from datetime import datetime

from schemas.strategy import CardStatus, ChallengeVerdict, SessionState, StrategySession

from dashboard.api.models import ActivityEvent, ActivityKind

CONTEXT_AGENT = "strategy_context"
STRATEGIST_AGENT = "tycho_strategist"
CHALLENGER_AGENT = "tycho_challenger"
BRIEF_WRITER_AGENT = "tycho_brief_writer"
COUNCIL_AGENT = "tycho_strategy_council"

#: Deterministic rejection classes.  The mapping reads the Python-generated
#: reason text and emits only the class label, never the text itself.
_REASON_CLASSES: tuple[tuple[str, str], ...] = (
    ("challenger:", "challenger"),
    ("cross-entity conclusion needs", "entity_diversity"),
    ("independent source families", "source_diversity"),
    ("conclusion asserts unsupported", "conclusion_language"),
    ("is stale and is not labelled", "stale_premise"),
    ("limitation names", "limitation_mismatch"),
    ("exceeds the evidence ceiling", "confidence_ceiling"),
    ("session card limit reached", "card_limit"),
    ("unknown premise claim", "premise_resolution"),
    ("not active", "premise_resolution"),
    ("is at version", "premise_version"),
    ("is operational", "premise_resolution"),
    ("has no resolvable evidence", "evidence_admissibility"),
    ("cites", "evidence_admissibility"),
    ("premises; got", "premise_count"),
)


def reason_class(reason: str) -> str:
    """Reduce one rejection reason to a fixed class label."""
    lowered = reason.casefold()
    for needle, label in _REASON_CLASSES:
        if needle in lowered:
            return label
    return "other"


def reason_classes(reasons: list[str]) -> list[str]:
    seen: list[str] = []
    for reason in reasons:
        label = reason_class(reason)
        if label not in seen:
            seen.append(label)
    return seen[:8]


def failure_class(error: str | None) -> str | None:
    """``stage:ExceptionClass: curated reason`` -> ``stage:ExceptionClass``."""
    if not error:
        return None
    head = error.split(": ", 1)[0].strip()
    return head[:120] or None


def _event(seq: int, kind: ActivityKind, at: datetime, **fields: object) -> ActivityEvent:
    return ActivityEvent(seq=seq, event=kind, at=at, **fields)  # type: ignore[arg-type]


def derive_activity(session: StrategySession) -> list[ActivityEvent]:
    """Reconstruct the fixed workflow's structural timeline for one session."""
    started = session.created_at
    finished = session.updated_at
    events: list[ActivityEvent] = []
    seq = 0

    events.append(
        _event(
            seq,
            ActivityKind.RUN_STARTED,
            started,
            session_id=session.session_id,
            agent=COUNCIL_AGENT,
            state=session.state.value,
        )
    )
    seq += 1

    if session.input_manifest or session.state is not SessionState.RUNNING:
        events.append(
            _event(
                seq,
                ActivityKind.AGENT_COMPLETED,
                finished,
                session_id=session.session_id,
                agent=CONTEXT_AGENT,
                state="completed" if session.input_manifest else "empty",
                claim_versions=[
                    f"{entry.claim_id}@{entry.claim_version}"
                    for entry in session.input_manifest[:200]
                ],
            )
        )
        seq += 1

    if session.state is SessionState.FAILED:
        events.append(
            _event(
                seq,
                ActivityKind.RUN_FAILED,
                finished,
                session_id=session.session_id,
                agent=COUNCIL_AGENT,
                state="failed",
                failure_class=failure_class(session.error),
            )
        )
        return events

    if session.state is SessionState.RUNNING:
        events.append(
            _event(
                seq,
                ActivityKind.AGENT_STARTED,
                finished,
                session_id=session.session_id,
                agent=STRATEGIST_AGENT,
                state="running",
            )
        )
        return events

    events.append(
        _event(
            seq,
            ActivityKind.AGENT_COMPLETED,
            finished,
            session_id=session.session_id,
            agent=STRATEGIST_AGENT,
            state="completed",
            card_count=session.metrics.cards_proposed,
        )
    )
    seq += 1

    challenges = {result.card_id: result for result in session.challenges}
    for card in session.cards:
        result = challenges.get(card.card_id)
        if result is None:
            continue
        events.append(
            _event(
                seq,
                ActivityKind.AGENT_COMPLETED,
                finished,
                session_id=session.session_id,
                agent=CHALLENGER_AGENT,
                state=(
                    "passed" if result.verdict is ChallengeVerdict.PASS else "rejected"
                ),
                card_id=card.card_id,
                card_count=1,
            )
        )
        seq += 1

    for card in session.cards:
        if card.status is not CardStatus.REJECTED:
            continue
        events.append(
            _event(
                seq,
                ActivityKind.CARD_REJECTED,
                finished,
                session_id=session.session_id,
                agent=COUNCIL_AGENT,
                state="rejected",
                card_id=card.card_id,
                reason_count=len(card.rejection_reasons),
                reason_classes=reason_classes(list(card.rejection_reasons)),
            )
        )
        seq += 1

    passed = session.metrics.cards_passed
    if passed:
        events.append(
            _event(
                seq,
                ActivityKind.AGENT_COMPLETED,
                finished,
                session_id=session.session_id,
                agent=BRIEF_WRITER_AGENT,
                state="completed",
                card_count=passed,
                passed_count=passed,
            )
        )
        seq += 1

    events.append(
        _event(
            seq,
            ActivityKind.BRIEF_COMPLETED,
            finished,
            session_id=session.session_id,
            agent=COUNCIL_AGENT,
            state="completed",
            brief_id=session.brief_id,
            card_count=session.metrics.cards_proposed,
            passed_count=session.metrics.cards_passed,
            rejected_count=session.metrics.cards_rejected,
        )
    )
    return events
