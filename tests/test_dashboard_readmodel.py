"""Read-model rules: canonical-v2 only, exact versions, deterministic counts."""

from __future__ import annotations

import inspect
from datetime import timedelta

import pytest

from dashboard.api import source as source_module
from dashboard.api.readmodel import MAX_TIMELINE_LIMIT, ReadModel, UnknownResource
from dashboard.api.source import READ_SOURCE_METHODS, CloudReadSource, decode_delta_row
from schemas.claim import ClaimStatus
from schemas.delta import Triage
from tests.dashboard_helpers import (
    FORBIDDEN_METHODS,
    NOW,
    RecordingSource,
    build_dashboard_market,
    config,
)
from strategy_agent.synthetic import synthetic_id


@pytest.fixture()
def market():
    return build_dashboard_market()


@pytest.fixture()
def source(market):
    return RecordingSource(market)


@pytest.fixture()
def model(source):
    return ReadModel(source, config())


# --- The read surface itself ------------------------------------------------


def test_the_cloud_read_source_exposes_exactly_the_read_methods():
    public = {
        name
        for name in dir(CloudReadSource)
        if not name.startswith("_") and callable(getattr(CloudReadSource, name, None))
    }
    assert public == set(READ_SOURCE_METHODS)


def test_the_read_source_has_no_write_gcs_or_archive_method():
    public = {name for name in dir(CloudReadSource) if not name.startswith("_")}
    assert not public & FORBIDDEN_METHODS


def test_no_read_source_query_names_the_archive_table():
    text = inspect.getsource(source_module)
    assert "delta_audit_log_20260826" not in text
    queries = [line for line in text.splitlines() if "FROM `" in line]
    assert queries
    assert not any("audit" in line for line in queries)


def test_every_delta_query_pins_the_canonical_schema_version():
    text = inspect.getsource(source_module)
    delta_queries = [line for line in text.splitlines() if "FROM `{self._deltas_table}`" in line]
    assert delta_queries
    # Each Delta query body must constrain schema_version to delta@2.
    assert text.count("schema_version = '{CANONICAL_SCHEMA_VERSION}'") == len(delta_queries)


def test_decoding_refuses_a_legacy_row():
    with pytest.raises(ValueError, match="canonical delta@2"):
        decode_delta_row(
            {
                "schema_version": "delta@1",
                "delta_id": synthetic_id("dlt", 9),
                "entity": "codex",
                "source": "github_releases",
                "obs_before": synthetic_id("obs", 18),
                "obs_after": synthetic_id("obs", 19),
                "computed_at": "2026-08-20T00:00:00+00:00",
                "diff_kind": "structured",
                "changes": [{"path": "tag", "before": '"a"', "after": '"b"'}],
                "summary": "legacy",
                "triage": "meaningful",
                "triage_by": "python-differ@1",
                "routed_to": ["identity"],
            }
        )


def test_a_full_read_pass_never_touches_a_forbidden_method(model, source, market):
    snapshot = model.snapshot(now=NOW)
    model.health(snapshot)
    model.overview(snapshot)
    model.timeline("codex", now=NOW)
    model.provenance(market.claims[0].claim_id, 1, now=NOW)
    model.latest_session(now=NOW)
    model.activity(market.sessions[0].session_id)
    assert set(source.calls) <= set(READ_SOURCE_METHODS)
    assert not set(source.calls) & FORBIDDEN_METHODS


# --- Overview and counts ----------------------------------------------------


def test_counts_are_computed_deterministically_from_the_store(model, market):
    overview = model.overview(model.snapshot(now=NOW))
    assert overview.totals.active_claims == sum(
        1 for claim in market.claims if claim.status is ClaimStatus.ACTIVE
    )
    assert overview.totals.superseded_claims == 1
    assert overview.totals.retired_claims == 1
    assert overview.totals.meaningful_deltas == sum(
        1 for delta in market.deltas if delta.triage is Triage.MEANINGFUL
    )
    assert overview.totals.noise_deltas == 1
    again = model.overview(model.snapshot(now=NOW))
    assert again.model_dump(mode="json") == overview.model_dump(mode="json")


def test_every_configured_entity_gets_a_card_in_config_order(model):
    overview = model.overview(model.snapshot(now=NOW))
    assert [card.entity for card in overview.entities] == list(config().entities)


def test_an_entity_with_no_active_claim_explains_what_it_is_waiting_for(model):
    overview = model.overview(model.snapshot(now=NOW))
    pi = next(card for card in overview.entities if card.entity == "pi")
    assert pi.active_claim_count == 0
    assert pi.latest_change is None
    assert pi.waiting_for
    gemini = next(card for card in overview.entities if card.entity == "gemini_cli")
    assert gemini.active_claim_count == 0
    assert gemini.waiting_for


def test_a_noise_delta_is_never_the_latest_meaningful_change(model):
    overview = model.overview(model.snapshot(now=NOW))
    pi = next(card for card in overview.entities if card.entity == "pi")
    assert pi.latest_change is None


def test_the_disputed_badge_is_derived_from_an_active_inbound_link(model):
    overview = model.overview(model.snapshot(now=NOW))
    claude = next(card for card in overview.entities if card.entity == "claude_code")
    codex = next(card for card in overview.entities if card.entity == "codex")
    assert claude.disputed is True
    assert codex.disputed is False


def test_staleness_uses_the_configured_threshold(model, market):
    overview = model.overview(model.snapshot(now=NOW))
    codex = next(card for card in overview.entities if card.entity == "codex")
    assert codex.stale is False
    old = model.snapshot(now=NOW + timedelta(days=365))
    stale_overview = model.overview(old)
    stale_codex = next(
        card for card in stale_overview.entities if card.entity == "codex"
    )
    assert stale_codex.stale is True


def test_watcher_targets_come_from_config_not_from_a_payload(model):
    overview = model.overview(model.snapshot(now=NOW))
    claude = next(card for card in overview.entities if card.entity == "claude_code")
    targets = {watcher.source: watcher.target for watcher in claude.watchers}
    assert targets["github_releases"] == "https://github.com/anthropics/claude-code"
    assert targets["website_changelog"].startswith("https://code.claude.com/")


# --- Health -----------------------------------------------------------------


def test_health_reports_every_fleet_component(model):
    health = model.health(model.snapshot(now=NOW))
    assert [item.key for item in health.components] == [
        "acquisition",
        "differ",
        "analyst",
        "strategy",
    ]
    assert all(item.detail for item in health.components)


def test_health_goes_stale_when_acquisition_stops(model):
    health = model.health(model.snapshot(now=NOW + timedelta(days=9)))
    acquisition = next(item for item in health.components if item.key == "acquisition")
    assert acquisition.state.value == "stale"
    assert health.state.value in {"stale", "idle", "failed"}


# --- Belief timeline --------------------------------------------------------


def test_every_timeline_event_pins_a_claim_id_and_version(model):
    timeline = model.timeline("codex", now=NOW)
    assert timeline.events
    for event in timeline.events:
        assert event.claim.claim_id.startswith("clm_")
        assert event.claim.version >= 1
        assert event.event_id.startswith(event.claim.claim_id)


def test_supersession_shows_the_old_and_the_replacement_together(model):
    timeline = model.timeline("codex", scope="pricing", now=NOW)
    superseded = [event for event in timeline.events if event.kind.value == "superseded"]
    assert len(superseded) == 1
    event = superseded[0]
    assert event.replacement is not None
    assert event.replacement.claim_id != event.claim.claim_id
    assert "$45" in event.replacement.statement
    assert "$30" in event.claim.statement


def test_the_timeline_records_created_verified_disputed_and_retired(model):
    claude = model.timeline("claude_code", now=NOW)
    kinds = {event.kind.value for event in claude.events}
    assert {"created", "verified", "disputed"} <= kinds
    gemini = model.timeline("gemini_cli", now=NOW)
    assert {event.kind.value for event in gemini.events} == {"created", "retired"}


def test_the_timeline_is_newest_first_and_stable(model):
    timeline = model.timeline("codex", now=NOW)
    stamps = [event.at for event in timeline.events]
    assert stamps == sorted(stamps, reverse=True)
    assert [event.event_id for event in timeline.events] == [
        event.event_id for event in model.timeline("codex", now=NOW).events
    ]


def test_the_timeline_filters_by_scope_only_within_the_ontology(model):
    pricing = model.timeline("codex", scope="pricing", now=NOW)
    assert {event.claim.scope for event in pricing.events} == {"pricing"}
    with pytest.raises(UnknownResource):
        model.timeline("codex", scope="not-a-scope", now=NOW)
    with pytest.raises(UnknownResource):
        model.timeline("not-an-entity", now=NOW)


def test_timeline_pagination_is_bounded_and_stable(model):
    full = model.timeline("codex", limit=MAX_TIMELINE_LIMIT, now=NOW)
    first = model.timeline("codex", limit=2, offset=0, now=NOW)
    second = model.timeline("codex", limit=2, offset=2, now=NOW)
    assert first.next_offset == 2
    assert [event.event_id for event in first.events + second.events] == [
        event.event_id for event in full.events
    ]
    assert model.timeline("codex", limit=10_000, now=NOW).limit == MAX_TIMELINE_LIMIT


def test_evidence_chips_mark_admissibility(model, market):
    timeline = model.timeline("claude_code", now=NOW)
    chips = [chip for event in timeline.events for chip in event.evidence]
    assert chips
    assert all(chip.canonical for chip in chips)
    assert all(chip.source_family.startswith("claude_code/") for chip in chips)


# --- Provenance -------------------------------------------------------------


def test_provenance_resolves_the_exact_current_version(model, market):
    claim = market.claims[0]
    result = model.provenance(claim.claim_id, claim.version, now=NOW)
    assert result.exact_version is True
    assert result.current_version == claim.version
    assert result.reconstruction_note is None
    assert result.claim.statement.startswith("Claude Code ships sandboxed")


def test_provenance_refuses_a_version_that_never_existed(model, market):
    claim = market.claims[0]
    with pytest.raises(UnknownResource):
        model.provenance(claim.claim_id, claim.version + 5, now=NOW)
    with pytest.raises(UnknownResource):
        model.provenance(synthetic_id("clm", 999), 1, now=NOW)


def test_provenance_reconstructs_an_older_version_from_history(model, market):
    retired = next(claim for claim in market.claims if claim.history)
    result = model.provenance(retired.claim_id, 1, now=NOW)
    assert result.history
    assert result.history[0].action == "retired"
    assert result.history[0].delta_ids


def test_provenance_carries_the_grounded_quote_and_observation_metadata(model, market):
    claim = market.claims[0]
    result = model.provenance(claim.claim_id, claim.version, now=NOW)
    evidence = result.evidence[0]
    assert evidence.admissible is True
    assert evidence.triage == "meaningful"
    assert evidence.changes[0].quote_after
    roles = {ref.role for ref in evidence.observations}
    assert roles == {"before", "after"}
    assert all(ref.obs_id.startswith("obs_") for ref in evidence.observations)
    assert evidence.source_ref is not None
    assert evidence.source_ref.target.startswith("https://")


def test_provenance_never_returns_a_raw_payload_pointer(model, market):
    claim = market.claims[0]
    result = model.provenance(claim.claim_id, claim.version, now=NOW).model_dump_json()
    assert "gs://" not in result
    assert "content_ref" not in result
    assert "content_hash" not in result


def test_provenance_marks_inadmissible_evidence_rather_than_hiding_it(market):
    market.deltas.clear()
    model = ReadModel(RecordingSource(market), config())
    claim = market.claims[0]
    result = model.provenance(claim.claim_id, claim.version, now=NOW)
    assert result.evidence[0].admissible is False
    assert result.evidence[0].defect


def test_provenance_lists_active_inbound_disputes(model, market):
    target = market.claims[0]
    result = model.provenance(target.claim_id, target.version, now=NOW)
    assert result.lifecycle.disputed_by == [market.claims[-1].claim_id]


# --- Strategy ---------------------------------------------------------------


def test_the_latest_session_carries_passed_and_rejected_cards(model):
    result = model.latest_session(now=NOW)
    assert result.session is not None
    assert len(result.passed_cards) == 1
    assert len(result.rejected_cards) == 1
    assert result.rejected_cards[0].rejection_reasons
    assert result.passed_cards[0].challenger_verdict == "pass"


def test_passed_card_premises_resolve_to_exact_claim_versions(model):
    result = model.latest_session(now=NOW)
    premises = result.passed_cards[0].premises
    assert len(premises) == 2
    for premise in premises:
        assert premise.resolved is True
        assert premise.delta_ids
        assert premise.statement


def test_a_brief_is_loaded_by_its_immutable_id(model, market):
    result = model.latest_session(now=NOW)
    assert result.brief is not None
    assert result.brief.brief_id == market.briefs[0].brief_id
    assert result.brief.empty is False
    assert len(result.brief.claims_referenced) == 2


def test_an_empty_brief_is_reported_honestly(market):
    brief = market.briefs[0].model_copy(update={"strategy_card_ids": []})
    market.briefs[0] = brief
    session = market.sessions[0]
    market.sessions[0] = session.model_copy(
        update={
            "cards": [card for card in session.cards if card.status.value == "rejected"],
            "challenges": [],
            "metrics": session.metrics.model_copy(
                update={"cards_passed": 0, "cards_proposed": 1, "cards_rejected": 1}
            ),
        }
    )
    model = ReadModel(RecordingSource(market), config())
    result = model.latest_session(now=NOW)
    assert result.passed_cards == []
    assert result.brief.empty is True
    assert result.waiting_for


def test_no_completed_session_is_an_explained_empty_state(market):
    market.sessions.clear()
    model = ReadModel(RecordingSource(market), config())
    result = model.latest_session(now=NOW)
    assert result.session is None
    assert result.waiting_for


def test_an_unknown_session_is_a_lookup_failure(model):
    with pytest.raises(UnknownResource):
        model.session(synthetic_id("sts", 999))
