"""The bounded council workflow: gates, brief contents, and reproducibility."""

import asyncio
from datetime import timedelta

import pytest

from pipeline.strategy_context import default_period
from schemas.claim import ClaimStatus
from schemas.strategy import (
    MAX_CARDS_PER_SESSION,
    SessionState,
)
from strategy_agent.agents import BRIEF_WRITER_NAME, CHALLENGER_NAME, STRATEGIST_NAME
from strategy_agent.citations import CitationError, find_citations
from strategy_agent.council import brief_id_for, run_strategy_session
from strategy_agent.events import assert_event_is_safe
from strategy_agent.synthetic import (
    ScriptedInvoker,
    build_synthetic_market,
    scripted_session,
)
from tests.strategy_helpers import NOW, brief_payload, config, draft, seeded_store

PASS = {"card_id": "unused", "verdict": "pass"}


def run(store, invoker, *, now=NOW, days=7):
    return asyncio.run(
        run_strategy_session(
            store, config(), invoker, period=default_period(now, days), now=now
        )
    )


def accepted_draft(market):
    claude, _, codex, _ = market.claims
    return draft(market, premises=[(claude.claim_id, 1), (codex.claim_id, 1)])


def test_synthetic_session_produces_one_passed_and_one_rejected_card(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        result = run(store, scripted_session(market))
        session = result.session

        assert session.state is SessionState.COMPLETED
        assert len(session.passed_cards()) == 1
        assert len(session.rejected_cards()) == 1
        assert session.metrics.cards_passed == 1
        assert session.metrics.cards_rejected == 1

        passed = session.passed_cards()[0]
        assert passed.entities == ["claude_code", "codex"]
        assert len(passed.source_families) == 2
        rejected = session.rejected_cards()[0]
        assert any("causation" in reason for reason in rejected.rejection_reasons)
        assert any("source families" in reason for reason in rejected.rejection_reasons)


def test_hard_validation_runs_before_the_challenger_sees_a_card(tmp_path):
    market = build_synthetic_market(NOW)
    invoker = scripted_session(market)
    with seeded_store(tmp_path, market) as store:
        run(store, invoker)
        # Two drafts, but only the one that survived hard validation is challenged.
        assert invoker.calls == [STRATEGIST_NAME, CHALLENGER_NAME, BRIEF_WRITER_NAME]


def test_challenger_pass_cannot_override_a_hard_failure(tmp_path):
    """A card that fails Python validation is never offered to the Challenger."""
    market = build_synthetic_market(NOW)
    claude, mirror, _, _ = market.claims
    invoker = ScriptedInvoker(
        strategist={
            "cards": [draft(market, premises=[(claude.claim_id, 1), (mirror.claim_id, 1)])]
        },
        challenger=[PASS],
        brief_writer=brief_payload([claude.claim_id]),
    )
    with seeded_store(tmp_path, market) as store:
        result = run(store, invoker)
        assert invoker.calls == [STRATEGIST_NAME]
        assert result.session.passed_cards() == []
        assert len(result.session.rejected_cards()) == 1
        assert result.brief is not None
        assert result.brief.strategy_card_ids == []


def test_challenger_failure_rejects_an_otherwise_valid_card(tmp_path):
    market = build_synthetic_market(NOW)
    invoker = ScriptedInvoker(
        strategist={"cards": [accepted_draft(market)]},
        challenger=[
            {
                "card_id": "unused",
                "verdict": "fail",
                "policy_violations": ["the falsifier is not an observable future signal"],
                "correction_request": "Name a signal Tycho's configured sources could see.",
            }
        ],
    )
    with seeded_store(tmp_path, market) as store:
        result = run(store, invoker)
        assert invoker.calls == [STRATEGIST_NAME, CHALLENGER_NAME]
        rejected = result.session.rejected_cards()
        assert len(rejected) == 1
        assert any("challenger:" in reason for reason in rejected[0].rejection_reasons)
        assert result.session.challenges[0].verdict.value == "fail"


def test_rejected_card_is_visible_in_the_audit_but_absent_from_the_brief(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        result = run(store, scripted_session(market))
        rejected = result.session.rejected_cards()[0]
        passed = result.session.passed_cards()[0]

        stored = store.get_strategy_session(result.session.session_id)
        assert rejected.card_id in {card.card_id for card in stored.cards}
        assert result.brief.strategy_card_ids == [passed.card_id]
        assert rejected.card_id not in result.brief.strategy_card_ids
        for premise in rejected.premises:
            if premise.claim_id not in {p.claim_id for p in passed.premises}:
                assert premise.claim_id not in result.brief.rendered_md


def test_an_unresolvable_premise_is_recorded_without_fabricated_delta_ids(tmp_path):
    """A rejected card shows exactly what was cited, and claims no evidence."""
    market = build_synthetic_market(NOW)
    claude, _, _, _ = market.claims
    unknown = "clm_01ARZ3NDEKTSV4RRFFQ69G5H99"
    invoker = ScriptedInvoker(
        strategist={"cards": [draft(market, premises=[(claude.claim_id, 1), (unknown, 1)])]},
        challenger=[PASS],
    )
    with seeded_store(tmp_path, market) as store:
        result = run(store, invoker)
        assert invoker.calls == [STRATEGIST_NAME]

        rejected = result.session.rejected_cards()[0]
        cited = {premise.claim_id: premise.delta_ids for premise in rejected.premises}
        assert cited[unknown] == []
        assert cited[claude.claim_id]
        assert any("unknown premise claim" in r for r in rejected.rejection_reasons)


def test_brief_pins_exact_claim_versions_and_links_citations(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        result = run(store, scripted_session(market))
        brief = result.brief
        passed = result.session.passed_cards()[0]

        pinned = {(p.claim_id, p.claim_version) for p in passed.premises}
        assert {(r.claim_id, r.version) for r in brief.claims_referenced} == pinned
        assert brief.strategy_session_id == result.session.session_id
        assert "<claim id=" not in brief.rendered_md
        for claim_id, version in pinned:
            assert f"/claims/{claim_id}?version={version}" in brief.rendered_md
        assert "http://" not in brief.rendered_md and "https://" not in brief.rendered_md


def test_brief_citing_an_unpinned_claim_fails_the_run(tmp_path):
    market = build_synthetic_market(NOW)
    _, mirror, _, _ = market.claims
    invoker = ScriptedInvoker(
        strategist={"cards": [accepted_draft(market)]},
        challenger=[PASS],
        brief_writer=brief_payload([mirror.claim_id]),
    )
    with seeded_store(tmp_path, market) as store:
        with pytest.raises(CitationError, match="unpinned"):
            run(store, invoker)
        session = store.strategy_sessions()[0]
        assert session.state is SessionState.FAILED
        assert session.error
        assert store.briefs() == []


def test_brief_stays_reproducible_after_a_later_supersession(tmp_path):
    market = build_synthetic_market(NOW)
    claude, _, codex, _ = market.claims
    with seeded_store(tmp_path, market) as store:
        result = run(store, scripted_session(market))
        rendered = result.brief.rendered_md
        pinned = [(r.claim_id, r.version) for r in result.brief.claims_referenced]

        # The market moves on: the premise claim is superseded afterwards.
        store.update_claim(
            claude.claim_id,
            {"status": ClaimStatus.SUPERSEDED.value, "superseded_by": codex.claim_id},
        )

        reread = store.get_brief(result.brief.brief_id)
        assert reread.rendered_md == rendered
        assert [(r.claim_id, r.version) for r in reread.claims_referenced] == pinned
        assert find_citations(rendered) == []  # markers were replaced deterministically
        assert store.get_strategy_session(result.session.session_id).cards == result.session.cards


def test_zero_cards_is_a_valid_result_and_stores_an_empty_brief(tmp_path):
    market = build_synthetic_market(NOW)
    invoker = ScriptedInvoker(
        strategist={
            "cards": [],
            "no_pattern_reason": "No cross-entity pattern is supported by two source families.",
        },
        challenger=[],
    )
    with seeded_store(tmp_path, market) as store:
        result = run(store, invoker)
        assert invoker.calls == [STRATEGIST_NAME]
        assert result.session.state is SessionState.COMPLETED
        assert result.session.cards == []
        assert result.brief.strategy_card_ids == []
        assert result.brief.claims_referenced == []
        assert "No defensible cross-entity pattern" in result.brief.rendered_md


def test_at_most_three_cards_survive_one_session(tmp_path):
    market = build_synthetic_market(NOW)
    claude, _, codex, gemini = market.claims
    pairs = [
        (claude.claim_id, codex.claim_id),
        (codex.claim_id, gemini.claim_id),
        (claude.claim_id, gemini.claim_id),
    ]
    cards = [
        draft(
            market,
            premises=[(first, 1), (second, 1)],
            statement=f"Vendors {index} converged on comparable workspace controls.",
        )
        for index, (first, second) in enumerate(pairs)
    ]
    invoker = ScriptedInvoker(
        strategist={"cards": cards},
        challenger=[PASS, PASS, PASS],
        brief_writer=brief_payload([claude.claim_id]),
    )
    with seeded_store(tmp_path, market) as store:
        result = run(store, invoker)
        assert len(result.session.passed_cards()) <= MAX_CARDS_PER_SESSION
        assert len(result.session.passed_cards()) == 3


def test_session_never_mutates_claims_or_publishes(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        before = sorted(claim.model_dump_json() for claim in store.claims())
        deltas_before = sorted(delta.delta_id for delta in store.deltas())

        run(store, scripted_session(market))

        assert sorted(claim.model_dump_json() for claim in store.claims()) == before
        assert sorted(delta.delta_id for delta in store.deltas()) == deltas_before
        assert store.pending_count() == 0
        assert store.alerts() == []
        assert store.analyst_runs() == []
        assert store.receipts() == []


def test_events_are_redacted_and_name_the_agents(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        result = run(store, scripted_session(market))
        agents = [event.agent for event in result.events]
        assert STRATEGIST_NAME in agents
        assert CHALLENGER_NAME in agents
        assert BRIEF_WRITER_NAME in agents

        for event in result.events:
            payload = event.as_dict()
            assert_event_is_safe(payload)
            serialized = str(payload)
            assert "sandbox" not in serialized.lower()
            assert "isolation" not in serialized.lower()


def test_session_records_the_manifest_hash_and_agent_versions(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        session = run(store, scripted_session(market)).session
        assert len(session.manifest_hash) == 64
        assert session.agent_versions.strategist == "tycho_strategist@1"
        assert session.agent_versions.challenger == "tycho_challenger@1"
        assert session.agent_versions.brief_writer == "tycho_brief_writer@1"
        assert session.model_versions.strategist
        assert session.run_ids


def test_brief_id_tracks_the_period_and_stays_unique_per_session(tmp_path):
    period = default_period(NOW, 7)
    assert brief_id_for(period) == "brf_2026w35"
    earlier = default_period(NOW - timedelta(days=7), 7)
    assert brief_id_for(earlier) == "brf_2026w34"

    # Two attempts over the same week must not collide on the write-once brief.
    first = brief_id_for(period, "sts_01M0YT0HBMPYQRZG1FJ1HJEDKJ")
    second = brief_id_for(period, "sts_01M0YTEATHCED4T3JJ8FEGHQ8Q")
    assert first.startswith("brf_2026w35-")
    assert second.startswith("brf_2026w35-")
    assert first != second
