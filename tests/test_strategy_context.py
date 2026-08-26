"""The deterministic context builder: selection, metrics, budget, and boundaries."""

import json
from datetime import timedelta

import pytest

from pipeline.strategy_context import (
    MAX_CONTEXT_BYTES,
    StrategyContextTooLarge,
    build_strategy_context,
    default_period,
    enforce_context_budget,
)
from schemas.claim import (
    Claim,
    ClaimClass,
    ClaimStatus,
    Confidence,
    Evidence,
    Severity,
)
from strategy_agent.synthetic import build_synthetic_market, make_delta, synthetic_id
from tests.strategy_helpers import NOW, config, seeded_store, stale_market


def context_for(store, *, now=NOW, days=7, max_claims=60):
    return build_strategy_context(
        store, config(), period=default_period(now, days), now=now, max_claims=max_claims
    )


def test_context_admits_only_active_claims_backed_by_canonical_v2(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        context = context_for(store)
        assert {entry.claim_id for entry in context.manifest} == {
            claim.claim_id for claim in market.claims
        }
        assert all(entry.claim_version == 1 for entry in context.manifest)
        assert context.excluded_claim_ids == []


def test_context_excludes_a_claim_whose_evidence_is_not_canonical(tmp_path):
    market = build_synthetic_market(NOW)
    claude = market.claims[0]
    with seeded_store(tmp_path, market) as store:
        store.update_claim(
            claude.claim_id,
            {
                "evidence": [
                    {
                        "delta_id": "dlt_01ARZ3NDEKTSV4RRFFQ69G5FAX",
                        "source": "github_releases",
                        "note": "Not present in the canonical table.",
                    }
                ]
            },
        )
        context = context_for(store)
        assert claude.claim_id in context.excluded_claim_ids
        assert claude.claim_id not in {entry.claim_id for entry in context.manifest}


def test_context_excludes_operational_and_superseded_claims(tmp_path):
    market = build_synthetic_market(NOW)
    claude, mirror, codex, _ = market.claims
    with seeded_store(tmp_path, market) as store:
        store.update_claim(
            mirror.claim_id,
            {"status": ClaimStatus.SUPERSEDED.value, "superseded_by": codex.claim_id},
        )
        operational = Claim(
            claim_id=synthetic_id("clm", 555),
            entity="claude_code",
            scope="sources/github_releases",
            class_=ClaimClass.OPERATIONAL,
            statement="The GitHub feed occasionally repeats a prerelease tag.",
            rationale="Repeated tags cause duplicate observations for one release.",
            confidence=Confidence.CONFIRMED,
            severity=Severity.CONTEXT,
            evidence=[
                Evidence(
                    delta_id=market.deltas[0].delta_id,
                    source=market.deltas[0].source,
                    note="Canonical Gemini semantic Delta.",
                )
            ],
            status=ClaimStatus.ACTIVE,
            version=1,
            created_at=NOW,
            last_verified_at=NOW,
            created_by="gemini-analyst@1",
        )
        store.create_claim(operational)

        manifest_ids = {entry.claim_id for entry in context_for(store).manifest}
        assert mirror.claim_id not in manifest_ids
        assert operational.claim_id not in manifest_ids
        assert claude.claim_id in manifest_ids


def test_manifest_pins_versions_and_hashes_them_stably(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        first = context_for(store)
        second = context_for(store)
        assert first.manifest_hash == second.manifest_hash
        assert first.document == second.document

    with seeded_store(tmp_path / "other", market) as store:
        store.update_claim(market.claims[0].claim_id, {"version": 2})
        assert context_for(store).manifest_hash != first.manifest_hash


def test_metrics_carry_their_contributing_delta_ids(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        metrics = {metric.name: metric for metric in context_for(store).metrics}
        assert metrics["entity_coverage"].value == 3
        assert metrics["meaningful_delta_count"].value == 4
        assert metrics["meaningful_change_count"].value == 4
        assert metrics["change_scope_count"].value == 2
        assert metrics["days_since_latest_change"].value == 2
        for name in ("meaningful_delta_count", "entity_coverage", "change_scope_count"):
            assert metrics[name].delta_ids, f"{name} must name its evidence"
            assert set(metrics[name].delta_ids) <= {d.delta_id for d in market.deltas}


def test_metrics_are_bounded_to_the_period(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        old = make_delta(
            seed=808,
            entity="pi",
            source="github_releases",
            computed_at=NOW - timedelta(days=90),
            statement="Pi added a durable transcript export.",
            quote="Transcripts can now be exported.",
        )
        store.insert_delta(old, enqueue=False)
        metrics = {metric.name: metric for metric in context_for(store).metrics}
        assert old.delta_id not in metrics["meaningful_delta_count"].delta_ids
        assert metrics["entity_coverage"].value == 3


def test_context_carries_no_raw_payload_or_snapshot(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        payload = json.loads(context_for(store).document)
        assert set(payload) == {
            "question",
            "period",
            "claims",
            "deltas",
            "metrics",
            "period_activity",
            "selection",
        }
        for delta in payload["deltas"]:
            assert set(delta) == {
                "delta_id",
                "entity",
                "source",
                "source_family",
                "computed_at",
                "triage",
                "routed_to",
                "changes",
            }
        assert "content_ref" not in context_for(store).document
        assert "gs://" not in context_for(store).document


def test_selection_is_deterministic_and_capped(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        capped = context_for(store, max_claims=2)
        assert len(capped.manifest) == 2
        # The critical pricing claim outranks the notable capability claims.
        assert capped.manifest[0].severity == "critical"
        assert capped.manifest == context_for(store, max_claims=2).manifest
        assert json.loads(capped.document)["selection"] == {
            "admitted_claims": 4,
            "selected_claims": 2,
            "max_claims": 2,
            "excluded_noncanonical_claims": 0,
        }


def test_staleness_is_computed_against_tycho_yaml(tmp_path):
    with seeded_store(tmp_path, stale_market()) as store:
        entries = {entry.claim_id: entry for entry in context_for(store).manifest}
        assert all(entry.stale for entry in entries.values())
        pricing = next(entry for entry in entries.values() if entry.scope == "pricing")
        assert pricing.staleness_days == 30

    with seeded_store(tmp_path / "fresh", build_synthetic_market(NOW)) as store:
        assert not any(entry.stale for entry in context_for(store).manifest)


def test_oversized_context_fails_before_any_model_call():
    oversized = "x" * (MAX_CONTEXT_BYTES + 1)
    with pytest.raises(StrategyContextTooLarge, match="limit is"):
        enforce_context_budget(oversized)
    assert enforce_context_budget("y" * 400) == (400, 100)


def test_period_activity_names_new_and_superseded_claims(tmp_path):
    market = build_synthetic_market(NOW)
    claude, mirror, codex, _ = market.claims
    with seeded_store(tmp_path, market) as store:
        store.update_claim(
            mirror.claim_id,
            {"status": ClaimStatus.SUPERSEDED.value, "superseded_by": codex.claim_id},
        )
        activity = json.loads(context_for(store).document)["period_activity"]
        assert claude.claim_id in activity["new_claim_ids"]
        assert mirror.claim_id in activity["superseded_claim_ids"]


def test_default_period_rejects_a_zero_day_window():
    with pytest.raises(ValueError, match="at least one day"):
        default_period(NOW, 0)
    assert default_period(NOW).to - default_period(NOW).from_ == timedelta(days=7)
