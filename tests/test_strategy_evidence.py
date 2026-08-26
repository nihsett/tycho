"""Deterministic evidence rules: pinning, canonical-v2, diversity, and policy."""

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.strategy_evidence import (
    confidence_ceiling,
    corroboration_units,
    evidence_fingerprint,
    language_policy_hits,
    source_family,
    staleness_threshold,
    validate_card_draft,
)
from schemas.claim import (
    Claim,
    ClaimClass,
    ClaimStatus,
    Confidence,
    Evidence,
    InferenceKind,
    Severity,
)
from schemas.delta import ChangeCategory, ChangeScope, Delta, DeltaSchemaVersion
from schemas.strategy import StrategyCardDraft, StrategyConfidence
from strategy_agent.synthetic import (
    build_synthetic_market,
    make_delta,
    seed_market,
    synthetic_id,
)
from tests.strategy_helpers import NOW, config, draft, seeded_store, stale_market


def validated(store, payload):
    return validate_card_draft(StrategyCardDraft.model_validate(payload), store, config(), NOW)


def test_exact_claim_version_pinning_is_required(tmp_path):
    market = build_synthetic_market(NOW)
    claude, _, codex, _ = market.claims
    with seeded_store(tmp_path, market) as store:
        matching = validated(
            store, draft(market, premises=[(claude.claim_id, 1), (codex.claim_id, 1)])
        )
        assert matching.passed

        wrong_version = validated(
            store, draft(market, premises=[(claude.claim_id, 2), (codex.claim_id, 1)])
        )
        assert not wrong_version.passed
        assert any("version 1, not 2" in reason for reason in wrong_version.violations)


def test_superseded_premise_is_not_admissible(tmp_path):
    market = build_synthetic_market(NOW)
    claude, _, codex, _ = market.claims
    with seeded_store(tmp_path, market) as store:
        replacement_id = codex.claim_id
        store.update_claim(
            claude.claim_id,
            {"status": ClaimStatus.SUPERSEDED.value, "superseded_by": replacement_id},
        )
        result = validated(
            store, draft(market, premises=[(claude.claim_id, 1), (codex.claim_id, 1)])
        )
        assert not result.passed
        assert any("not active" in reason for reason in result.violations)


LEGACY_FIXTURE = Path("schemas/fixtures/delta.archive.legacy.example.json")


def test_canonical_store_refuses_to_write_a_legacy_delta(tmp_path):
    legacy = Delta.model_validate_json(LEGACY_FIXTURE.read_text())
    assert legacy.schema_version is DeltaSchemaVersion.V1
    with seeded_store(tmp_path, build_synthetic_market(NOW)) as store:
        with pytest.raises(ValueError, match="only delta@2"):
            store.insert_delta(legacy, enqueue=False)


def test_premise_citing_archived_legacy_evidence_is_rejected(tmp_path):
    """A delta@1 row is invisible to the canonical read path, so the premise fails."""
    market = build_synthetic_market(NOW)
    claude, _, codex, _ = market.claims
    legacy = Delta.model_validate_json(LEGACY_FIXTURE.read_text())

    with seeded_store(tmp_path, market) as store:
        # Force the archived row in behind the canonical write guard, exactly
        # as a pre-migration table would have held it.
        store.connection.execute(
            """
            INSERT INTO deltas (delta_id, entity, source, computed_at, triage, document)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                legacy.delta_id,
                legacy.entity,
                legacy.source,
                legacy.computed_at.isoformat(),
                legacy.triage.value,
                legacy.model_dump_json(),
            ),
        )
        store.connection.commit()
        assert store.get_delta(legacy.delta_id) is None
        assert legacy.delta_id not in {delta.delta_id for delta in store.list_canonical_deltas()}

        store.update_claim(
            claude.claim_id,
            {
                "evidence": [
                    {
                        "delta_id": legacy.delta_id,
                        "source": legacy.source,
                        "note": "Archived mechanical delta@1 row.",
                    }
                ]
            },
        )
        result = validated(
            store, draft(market, premises=[(claude.claim_id, 1), (codex.claim_id, 1)])
        )
        assert not result.passed
        assert any("unresolvable evidence" in reason for reason in result.violations)


def test_mirrored_release_text_is_one_source_family(tmp_path):
    market = build_synthetic_market(NOW)
    claude_primary, claude_mirror, _, _ = market.claims

    # Same vendor, two of its own channels, identical grounded text.
    assert source_family("claude_code", "github_releases") == "claude_code/official_release"
    assert source_family("claude_code", "website_changelog") == "claude_code/official_release"
    assert evidence_fingerprint(market.deltas[0]) == evidence_fingerprint(market.deltas[1])
    assert corroboration_units(market.deltas[:2]) == {"claude_code/official_release"}

    with seeded_store(tmp_path, market) as store:
        result = validated(
            store,
            draft(market, premises=[(claude_primary.claim_id, 1), (claude_mirror.claim_id, 1)]),
        )
        assert not result.passed
        assert any("independent source families" in reason for reason in result.violations)
        assert any("distinct entities" in reason for reason in result.violations)


def test_identical_text_across_unrelated_sources_still_collapses():
    """Republished text is one witness even when the source names differ."""
    quote = "Sandboxed shell execution is enabled by default for all workspaces."
    original = make_delta(
        seed=901,
        entity="claude_code",
        source="github_releases",
        computed_at=NOW,
        statement="Claude Code enables sandboxed shell execution by default.",
        quote=quote,
    )
    mirror = make_delta(
        seed=902,
        entity="claude_code",
        source="community_forum",
        computed_at=NOW,
        statement="A community post reproduces the sandbox announcement verbatim.",
        quote=quote,
    )
    assert source_family("claude_code", "community_forum") == (
        "claude_code/independent:community_forum"
    )
    assert len(corroboration_units([original, mirror])) == 1


def test_minimum_entity_and_source_diversity(tmp_path):
    market = build_synthetic_market(NOW)
    claude, _, codex, gemini = market.claims
    with seeded_store(tmp_path, market) as store:
        two_entities = validated(
            store, draft(market, premises=[(claude.claim_id, 1), (codex.claim_id, 1)])
        )
        assert two_entities.entities == ["claude_code", "codex"]
        assert len(two_entities.source_families) == 2
        assert two_entities.passed

        three_entities = validated(
            store,
            draft(
                market,
                premises=[
                    (claude.claim_id, 1),
                    (codex.claim_id, 1),
                    (gemini.claim_id, 1),
                ],
            ),
        )
        assert three_entities.entities == ["claude_code", "codex", "gemini_cli"]


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("Claude Code removed the legacy shell tool.", "removal"),
        ("Codex hardened isolation because rivals shipped first.", "causation"),
        ("Claude Code plans to unify its execution model.", "intent"),
        ("Codex is the market leader in agent isolation.", "leadership"),
        ("Claude Code will ship a hosted runtime next quarter.", "future_action"),
    ],
)
def test_prohibited_conclusion_language_is_detected(statement, expected):
    assert expected in language_policy_hits(statement)


def test_intent_causation_and_leadership_conclusions_are_rejected(tmp_path):
    market = build_synthetic_market(NOW)
    claude, _, codex, _ = market.claims
    with seeded_store(tmp_path, market) as store:
        result = validated(
            store,
            draft(
                market,
                premises=[(claude.claim_id, 1), (codex.claim_id, 1)],
                statement=(
                    "Claude Code intends to lead the market because Codex shipped "
                    "isolation first."
                ),
            ),
        )
        assert not result.passed
        assert "conclusion asserts unsupported intent" in result.violations
        assert "conclusion asserts unsupported causation" in result.violations
        assert "conclusion asserts unsupported leadership" in result.violations


def test_removal_is_allowed_only_with_deprecation_evidence(tmp_path):
    market = build_synthetic_market(NOW)
    claude, _, codex, _ = market.claims
    statement = "Claude Code and Codex both removed unsandboxed shell execution."

    with seeded_store(tmp_path, market) as store:
        without_evidence = validated(
            store,
            draft(market, premises=[(claude.claim_id, 1), (codex.claim_id, 1)], statement=statement),
        )
        assert "conclusion asserts unsupported removal" in without_evidence.violations

    deprecation = make_delta(
        seed=903,
        entity="codex",
        source="website_changelog",
        computed_at=NOW,
        statement="Codex withdrew the unsandboxed execution path.",
        quote="The unsandboxed execution path is gone.",
        category=ChangeCategory.DEPRECATION,
        scope=ChangeScope.PRODUCT_CAPABILITIES,
    )
    evidenced = market.claims[2].model_copy(
        update={
            "evidence": [
                *market.claims[2].evidence,
                market.claims[2].evidence[0].model_copy(
                    update={"delta_id": deprecation.delta_id}
                ),
            ]
        }
    )
    with seeded_store(tmp_path / "second", market) as store:
        store.insert_delta(deprecation, enqueue=False)
        store.update_claim(
            evidenced.claim_id,
            {"evidence": [item.model_dump(mode="json") for item in evidenced.evidence]},
        )
        with_evidence = validated(
            store,
            draft(market, premises=[(claude.claim_id, 1), (codex.claim_id, 1)], statement=statement),
        )
        assert "conclusion asserts unsupported removal" not in with_evidence.violations


def test_stale_premise_must_be_labelled_and_speculative(tmp_path):
    market = stale_market()
    claude, _, codex, _ = market.claims
    premises = [(claude.claim_id, 1), (codex.claim_id, 1)]

    with seeded_store(tmp_path, market) as store:
        unlabelled = validated(store, draft(market, premises=premises))
        assert not unlabelled.passed
        assert any("is stale and is not labelled" in r for r in unlabelled.violations)

        labelled_but_likely = validated(
            store,
            draft(
                market,
                premises=premises,
                confidence="likely",
                limitations=[
                    {
                        "kind": "stale_premise",
                        "claim_id": claim_id,
                        "detail": "Not re-verified inside the staleness budget.",
                    }
                    for claim_id, _ in premises
                ],
            ),
        )
        assert not labelled_but_likely.passed
        assert any("exceeds the evidence ceiling" in r for r in labelled_but_likely.violations)

        labelled_and_speculative = validated(
            store,
            draft(
                market,
                premises=premises,
                confidence="speculative",
                limitations=[
                    {
                        "kind": "stale_premise",
                        "claim_id": claim_id,
                        "detail": "Not re-verified inside the staleness budget.",
                    }
                    for claim_id, _ in premises
                ],
            ),
        )
        assert labelled_and_speculative.passed
        assert labelled_and_speculative.confidence is StrategyConfidence.SPECULATIVE


def test_staleness_threshold_uses_tycho_yaml_budgets():
    settings = config()
    assert staleness_threshold(settings, "pricing") == 30
    assert staleness_threshold(settings, "product/capabilities") == 60
    assert staleness_threshold(settings, "gtm") == settings.staleness_days["default"]


def speculative_inference(market) -> Claim:
    """A speculative present-state inference over two distinct sources."""
    codex_delta, gemini_delta = market.deltas[2], market.deltas[3]
    return Claim(
        claim_id=synthetic_id("clm", 777),
        entity="codex",
        scope="product/capabilities",
        class_=ClaimClass.INFERENCE,
        inference_kind=InferenceKind.PRESENT_STATE,
        statement="Codex is likely standardizing isolation across its surfaces.",
        rationale="Two differently sourced signals point the same way, weakly.",
        confidence=Confidence.SPECULATIVE,
        severity=Severity.CONTEXT,
        evidence=[
            Evidence(
                delta_id=codex_delta.delta_id,
                source=codex_delta.source,
                note="Canonical Gemini semantic Delta.",
            ),
            Evidence(
                delta_id=gemini_delta.delta_id,
                source=gemini_delta.source,
                note="Canonical Gemini semantic Delta from another source.",
            ),
        ],
        status=ClaimStatus.ACTIVE,
        version=1,
        created_at=NOW,
        last_verified_at=NOW,
        created_by="gemini-analyst@1",
    )


def test_confidence_never_exceeds_the_weakest_premise(tmp_path):
    market = build_synthetic_market(NOW)
    claude, _, codex, _ = market.claims
    weak = speculative_inference(market)

    assert confidence_ceiling([claude, weak]) is StrategyConfidence.SPECULATIVE
    assert confidence_ceiling([claude, codex]) is StrategyConfidence.LIKELY

    with seeded_store(tmp_path, market) as store:
        store.create_claim(weak)
        likely = validated(
            store, draft(market, premises=[(claude.claim_id, 1), (weak.claim_id, 1)])
        )
        assert not likely.passed
        assert any("exceeds the evidence ceiling speculative" in r for r in likely.violations)

        speculative = validated(
            store,
            draft(
                market,
                premises=[(claude.claim_id, 1), (weak.claim_id, 1)],
                confidence="speculative",
            ),
        )
        assert speculative.passed
        assert speculative.confidence is StrategyConfidence.SPECULATIVE


def test_strategy_confidence_can_never_be_confirmed():
    with pytest.raises(ValidationError):
        StrategyCardDraft.model_validate(
            draft(build_synthetic_market(NOW), premises=[], confidence="confirmed")
        )


def test_premise_count_bounds(tmp_path):
    market = build_synthetic_market(NOW)
    claude, mirror, codex, gemini = market.claims
    with seeded_store(tmp_path, market) as store:
        single = validated(store, draft(market, premises=[(claude.claim_id, 1)]))
        assert not single.passed
        assert any("needs 2-5 premises" in reason for reason in single.violations)

    with pytest.raises(ValidationError):
        StrategyCardDraft.model_validate(
            draft(
                market,
                premises=[
                    (claude.claim_id, 1),
                    (mirror.claim_id, 1),
                    (codex.claim_id, 1),
                    (gemini.claim_id, 1),
                    (claude.claim_id, 2),
                    (mirror.claim_id, 2),
                ],
            )
        )


def test_no_claim_mutation_during_a_normal_validation_pass(tmp_path):
    market = build_synthetic_market(NOW)
    claude, _, codex, _ = market.claims
    with seeded_store(tmp_path, market) as store:
        before = {claim.claim_id: claim.model_dump_json() for claim in store.claims()}
        validated(store, draft(market, premises=[(claude.claim_id, 1), (codex.claim_id, 1)]))
        after = {claim.claim_id: claim.model_dump_json() for claim in store.claims()}
        assert before == after
        assert store.stats()["alerts"] == 0
        assert store.pending_count() == 0


def test_period_metric_recency_uses_real_delta_timestamps(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        seed_market(store, market.__class__(deltas=[], claims=[]))
        latest = max(delta.computed_at for delta in market.deltas)
        assert NOW - latest == timedelta(days=2)
