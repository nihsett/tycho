from pathlib import Path

from pipeline.calibrate_scenarios import CalibrationSuite
from pipeline.gemini_analyst import AnalystToolbox
from pipeline.local_backend import LocalBackend, LocalSettings
from schemas.config import load_config

FIXTURE = Path("schemas/fixtures/analyst.scenarios.json")


def load_suite() -> CalibrationSuite:
    return CalibrationSuite.model_validate_json(FIXTURE.read_text())


def test_all_five_stateful_scenarios_validate():
    suite = load_suite()
    assert [item.name for item in suite.scenarios] == [
        "price_change_then_redundancy",
        "price_reversal_supersession",
        "cross_source_fusion",
        "boring_tutorial_no_action",
        "third_party_dispute",
        "primary_source_dispute_resolution",
    ]
    fusion = suite.scenarios[2]
    assert len(fusion.prior_deltas) == 2
    assert len(fusion.prior_claims) == 1
    assert len({e.source for e in fusion.prior_claims[0].evidence}) == 2


def test_single_source_dispute_clamps_confidence_and_inherits_severity(tmp_path):
    config = load_config("tycho.yaml")
    scenario = load_suite().scenarios[4]
    with LocalBackend(config, LocalSettings(tmp_path / "data")) as store:
        for delta in scenario.prior_deltas:
            store.insert_delta(delta)
        for claim in scenario.prior_claims:
            store.create_claim(claim)
        store.insert_delta(scenario.delta)
        tools = AnalystToolbox(scenario.delta, config, store, mode="live")
        result = tools.create_claim(
            delta_id=scenario.delta.delta_id,
            scope="traction",
            claim_class="inference",
            inference_kind="intent_or_future",
            statement="A third-party signal claims Codex may shut down next month.",
            rationale="The signal conflicts with established official activity.",
            confidence="likely",
            severity="context",
            evidence_delta_ids=[scenario.delta.delta_id],
            evidence_notes=["Anonymous shutdown rumor."],
            disputes_claim_id=scenario.prior_claims[0].claim_id,
        )

        assert result["status"] == "accepted"
        assert result["claim"]["confidence"] == "speculative"
        assert result["claim"]["severity"] == "critical"
        assert result["claim"]["disputes"] == scenario.prior_claims[0].claim_id
        assert len(result["claim"]["evidence"]) == 1
        assert store.alerts()[0]["kind"] == "speculative_critical_claim"


def test_demotion_is_code_enforced_with_dispute_and_alert(tmp_path):
    config = load_config("tycho.yaml")
    scenario = load_suite().scenarios[4]
    with LocalBackend(config, LocalSettings(tmp_path / "data")) as store:
        for delta in scenario.prior_deltas:
            store.insert_delta(delta)
        for claim in scenario.prior_claims:
            store.create_claim(claim)
        store.insert_delta(scenario.delta)
        tools = AnalystToolbox(scenario.delta, config, store, mode="live")

        assert not hasattr(tools, "flag_disputed")
        result = tools.supersede_claim(
            delta_id=scenario.delta.delta_id,
            old_claim_id=scenario.prior_claims[0].claim_id,
            claim_class="inference",
            statement="Codex may shut down after August 2026.",
            rationale="An unverified search result contradicts official activity.",
            confidence="speculative",
            severity="critical",
            evidence_delta_ids=[
                scenario.delta.delta_id,
                scenario.prior_deltas[0].delta_id,
            ],
            evidence_notes=[
                "Unverified shutdown rumor.",
                "Official release activity contradicts the rumor.",
            ],
            inference_kind="intent_or_future",
        )

        assert result["status"] == "accepted"
        assert result["action"] == "demotion_blocked"
        assert result["attempted_action"] == "supersede_claim"
        protected = store.get_claim(scenario.prior_claims[0].claim_id)
        assert protected.status.value == "active"
        assert protected.history[-1]["event"] == "disputed"
        assert store.alerts()[0]["kind"] == "critical_claim_disputed"


def test_primary_source_supersedes_dispute_and_clears_badge(tmp_path):
    config = load_config("tycho.yaml")
    scenario = load_suite().scenarios[5]
    with LocalBackend(config, LocalSettings(tmp_path / "data")) as store:
        for delta in scenario.prior_deltas:
            store.insert_delta(delta)
        for claim in scenario.prior_claims:
            store.create_claim(claim)
        store.insert_delta(scenario.delta)
        target_id = scenario.prior_claims[0].claim_id
        dispute_id = scenario.prior_claims[1].claim_id
        assert len(store.active_disputes(target_id)) == 1
        tools = AnalystToolbox(scenario.delta, config, store, mode="live")

        result = tools.supersede_claim(
            delta_id=scenario.delta.delta_id,
            old_claim_id=dispute_id,
            claim_class="fact",
            statement="Codex remains actively developed as of 2026-08-21.",
            rationale="The official changelog directly resolves the shutdown rumor.",
            confidence="confirmed",
            severity="critical",
            evidence_delta_ids=[scenario.delta.delta_id],
            evidence_notes=["Official continuity statement."],
        )

        assert result["status"] == "accepted"
        assert result["action"] == "supersede_claim"
        assert store.get_claim(dispute_id).status.value == "superseded"
        assert store.get_claim(target_id).status.value == "active"
        assert store.active_disputes(target_id) == []
        assert store.alerts()[0]["kind"] == "dispute_resolved"
