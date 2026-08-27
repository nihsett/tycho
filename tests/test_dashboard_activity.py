"""Agent-activity derivation: structural only, and never content-bearing."""

from __future__ import annotations

import json

from dashboard.api.activity import derive_activity, failure_class, reason_class
from dashboard.api.models import ActivityKind
from dashboard.api.readmodel import ReadModel
from schemas.strategy import SessionState
from tests.dashboard_helpers import (
    RecordingSource,
    build_dashboard_market,
    config,
)

#: Every content-bearing string the fixture session actually holds.  None of it
#: may appear anywhere in a derived activity event.
def governed_prose(market) -> list[str]:
    session = market.sessions[0]
    values = [
        *(card.statement for card in session.cards),
        *(card.rationale for card in session.cards),
        *(card.competing_explanation for card in session.cards),
        *(card.falsifier for card in session.cards),
        *(reason for card in session.cards for reason in card.rejection_reasons),
        *(claim.statement for claim in market.claims),
        *(claim.rationale for claim in market.claims),
        *(delta.summary for delta in market.deltas),
        *(
            quote.quote
            for delta in market.deltas
            for change in delta.changes
            for quote in (change.evidence_before, change.evidence_after)
            if quote is not None
        ),
        market.briefs[0].rendered_md,
    ]
    return [value for value in values if value and len(value) > 12]


def test_the_derived_timeline_names_the_workflow_agents_in_order():
    market = build_dashboard_market()
    events = derive_activity(market.sessions[0])
    assert [event.event for event in events] == [
        ActivityKind.RUN_STARTED,
        ActivityKind.AGENT_COMPLETED,
        ActivityKind.AGENT_COMPLETED,
        ActivityKind.AGENT_COMPLETED,
        ActivityKind.CARD_REJECTED,
        ActivityKind.AGENT_COMPLETED,
        ActivityKind.BRIEF_COMPLETED,
    ]
    assert [event.agent for event in events] == [
        "tycho_strategy_council",
        "strategy_context",
        "tycho_strategist",
        "tycho_challenger",
        "tycho_strategy_council",
        "tycho_brief_writer",
        "tycho_strategy_council",
    ]
    assert [event.seq for event in events] == list(range(len(events)))


def test_the_strategist_event_carries_the_recomputed_card_count():
    market = build_dashboard_market()
    events = derive_activity(market.sessions[0])
    strategist = next(event for event in events if event.agent == "tycho_strategist")
    assert strategist.card_count == market.sessions[0].metrics.cards_proposed


def test_the_challenger_event_reports_its_verdict_per_card():
    market = build_dashboard_market()
    events = derive_activity(market.sessions[0])
    challenger = next(event for event in events if event.agent == "tycho_challenger")
    assert challenger.state == "passed"
    assert challenger.card_id == market.sessions[0].passed_cards()[0].card_id


def test_a_rejected_card_event_carries_classes_not_reasons():
    market = build_dashboard_market()
    events = derive_activity(market.sessions[0])
    rejected = next(event for event in events if event.event is ActivityKind.CARD_REJECTED)
    assert rejected.reason_count == 3
    assert set(rejected.reason_classes) == {
        "entity_diversity",
        "source_diversity",
        "conclusion_language",
    }
    payload = json.dumps(rejected.model_dump(mode="json"))
    assert "causation" not in payload
    assert "mirrored" not in payload


def test_no_derived_event_carries_governed_prose():
    market = build_dashboard_market()
    payload = json.dumps(
        [event.model_dump(mode="json") for event in derive_activity(market.sessions[0])]
    ).casefold()
    for prose in governed_prose(market):
        assert prose.casefold() not in payload


def test_a_failed_session_reports_a_failure_class_and_no_message():
    market = build_dashboard_market()
    failed = market.sessions[0].model_copy(
        update={
            "state": SessionState.FAILED,
            "brief_id": None,
            "error": "strategist:StrategyModelError: agent returned no usable structured output",
        }
    )
    events = derive_activity(failed)
    assert events[-1].event is ActivityKind.RUN_FAILED
    assert events[-1].failure_class == "strategist:StrategyModelError"
    assert "structured output" not in json.dumps(events[-1].model_dump(mode="json"))


def test_a_running_session_shows_the_strategist_as_started_only():
    market = build_dashboard_market()
    running = market.sessions[0].model_copy(
        update={
            "state": SessionState.RUNNING,
            "brief_id": None,
            "cards": [],
            "challenges": [],
        }
    )
    events = derive_activity(running)
    assert events[-1].event is ActivityKind.AGENT_STARTED
    assert events[-1].agent == "tycho_strategist"


def test_the_context_event_pins_the_claim_versions_it_read():
    market = build_dashboard_market()
    events = derive_activity(market.sessions[0])
    context = next(event for event in events if event.agent == "strategy_context")
    assert context.claim_versions
    assert all("@" in value for value in context.claim_versions)


def test_reason_classes_are_a_closed_deterministic_mapping():
    assert reason_class("challenger: unsupported premise clm_x") == "challenger"
    assert reason_class("conclusion asserts unsupported intent") == "conclusion_language"
    assert reason_class("premise claim clm_x is at version 2, not 1") == "premise_version"
    assert reason_class("something entirely new") == "other"
    assert failure_class(None) is None


def test_the_events_endpoint_labels_the_timeline_as_derived():
    market = build_dashboard_market()
    model = ReadModel(RecordingSource(market), config())
    response = model.activity(market.sessions[0].session_id)
    assert response.derived_from == "persisted strategy session record"
    assert all(event.derived for event in response.events)
