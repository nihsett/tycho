"""Strict strategy schemas: bounds, closed fields, and fixture round-trips."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemas.brief import Brief
from schemas.strategy import (
    MAX_CARDS_PER_SESSION,
    STRATEGY_QUESTION,
    CardStatus,
    ChallengeResult,
    SessionMetrics,
    StrategyCard,
    StrategyCardDraft,
    StrategyProposal,
    StrategySession,
    manifest_hash,
)

FIXTURES = Path("schemas/fixtures")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize(
    ("filename", "model", "key"),
    [
        ("strategy.card.passed.example.json", StrategyCard, "card_id"),
        ("strategy.card.rejected.example.json", StrategyCard, "card_id"),
        ("strategy.challenge.example.json", ChallengeResult, "card_id"),
        ("strategy.session.example.json", StrategySession, "session_id"),
        ("brief.strategy.example.json", Brief, "brief_id"),
    ],
)
def test_strategy_fixture_validates_and_round_trips(filename, model, key):
    payload = fixture(filename)
    parsed = model.model_validate(payload)
    reparsed = model.model_validate_json(parsed.model_dump_json(by_alias=True))
    assert getattr(reparsed, key) == getattr(parsed, key)
    assert reparsed == parsed


@pytest.mark.parametrize(
    "filename",
    [
        "strategy.card.passed.example.json",
        "strategy.session.example.json",
    ],
)
def test_unknown_fields_are_rejected(filename):
    payload = fixture(filename)
    payload["surprise"] = "extra"
    model = StrategyCard if "card" in filename else StrategySession
    with pytest.raises(ValidationError, match="surprise"):
        model.model_validate(payload)


def test_a_passed_card_needs_two_entities_and_two_source_families():
    payload = fixture("strategy.card.passed.example.json")

    single_entity = {**payload, "entities": ["claude_code"]}
    with pytest.raises(ValidationError, match="two distinct entities"):
        StrategyCard.model_validate(single_entity)

    single_family = {**payload, "source_families": ["claude_code/official_release"]}
    with pytest.raises(ValidationError, match="two distinct source families"):
        StrategyCard.model_validate(single_family)


def test_a_rejected_card_must_say_why_and_a_passed_one_must_not():
    payload = fixture("strategy.card.rejected.example.json")
    with pytest.raises(ValidationError, match="must record why"):
        StrategyCard.model_validate({**payload, "rejection_reasons": []})

    passed = fixture("strategy.card.passed.example.json")
    with pytest.raises(ValidationError, match="only a rejected card"):
        StrategyCard.model_validate({**passed, "rejection_reasons": ["late doubt"]})


def test_a_labelled_limitation_forces_speculative_confidence():
    payload = fixture("strategy.card.passed.example.json")
    limited = {
        **payload,
        "limitations": [
            {
                "kind": "stale_premise",
                "claim_id": payload["premises"][0]["claim_id"],
                "detail": "Not re-verified inside the staleness budget.",
            }
        ],
    }
    with pytest.raises(ValidationError, match="speculative"):
        StrategyCard.model_validate(limited)
    assert StrategyCard.model_validate({**limited, "confidence": "speculative"})


def test_a_limitation_must_name_one_of_the_cards_own_premises():
    payload = fixture("strategy.card.passed.example.json")
    with pytest.raises(ValidationError, match="own premises"):
        StrategyCard.model_validate(
            {
                **payload,
                "confidence": "speculative",
                "limitations": [
                    {
                        "kind": "stale_premise",
                        "claim_id": "clm_01ARZ3NDEKTSV4RRFFQ69G5H99",
                        "detail": "Unrelated claim.",
                    }
                ],
            }
        )


def test_a_passed_card_cannot_have_a_premise_without_canonical_deltas():
    payload = fixture("strategy.card.passed.example.json")
    stripped = {
        **payload,
        "premises": [
            {**payload["premises"][0], "delta_ids": []},
            payload["premises"][1],
        ],
    }
    with pytest.raises(ValidationError, match="canonical Delta IDs"):
        StrategyCard.model_validate(stripped)

    # A rejected card may record an unresolvable premise with no evidence.
    rejected = fixture("strategy.card.rejected.example.json")
    assert StrategyCard.model_validate(
        {
            **rejected,
            "premises": [
                {**rejected["premises"][0], "delta_ids": []},
                rejected["premises"][1],
            ],
        }
    )


def test_ids_and_lengths_are_bounded():
    payload = fixture("strategy.card.passed.example.json")
    with pytest.raises(ValidationError):
        StrategyCard.model_validate({**payload, "card_id": "card-1"})
    with pytest.raises(ValidationError):
        StrategyCard.model_validate({**payload, "statement": "x" * 401})
    with pytest.raises(ValidationError):
        StrategyCard.model_validate({**payload, "rationale": "x" * 801})


def test_a_proposal_is_capped_at_three_cards():
    card = fixture("strategy.card.passed.example.json")
    draft = {
        "statement": card["statement"],
        "rationale": card["rationale"],
        "confidence": "likely",
        "competing_explanation": card["competing_explanation"],
        "falsifier": card["falsifier"],
        "premises": [
            {"claim_id": premise["claim_id"], "claim_version": premise["claim_version"]}
            for premise in card["premises"]
        ],
    }
    assert StrategyProposal.model_validate({"cards": [draft] * MAX_CARDS_PER_SESSION})
    with pytest.raises(ValidationError):
        StrategyProposal.model_validate({"cards": [draft] * (MAX_CARDS_PER_SESSION + 1)})


def test_a_zero_card_proposal_must_explain_itself():
    with pytest.raises(ValidationError, match="zero-card"):
        StrategyProposal.model_validate({"cards": []})
    assert StrategyProposal.model_validate(
        {"cards": [], "no_pattern_reason": "No two source families agree this period."}
    )


def test_a_draft_cannot_cite_the_same_claim_twice():
    card = fixture("strategy.card.passed.example.json")
    premise = card["premises"][0]
    with pytest.raises(ValidationError, match="duplicates"):
        StrategyCardDraft.model_validate(
            {
                "statement": card["statement"],
                "rationale": card["rationale"],
                "confidence": "likely",
                "competing_explanation": card["competing_explanation"],
                "falsifier": card["falsifier"],
                "premises": [
                    {"claim_id": premise["claim_id"], "claim_version": 1},
                    {"claim_id": premise["claim_id"], "claim_version": 1},
                ],
            }
        )


def test_a_challenge_verdict_must_match_its_findings():
    with pytest.raises(ValidationError, match="must name a premise"):
        ChallengeResult.model_validate(
            {"card_id": fixture("strategy.card.passed.example.json")["card_id"], "verdict": "fail"}
        )
    with pytest.raises(ValidationError, match="cannot also report defects"):
        ChallengeResult.model_validate(
            {
                "card_id": fixture("strategy.card.passed.example.json")["card_id"],
                "verdict": "pass",
                "policy_violations": ["stale premise"],
            }
        )


def test_a_session_answers_only_the_fixed_market_question():
    payload = fixture("strategy.session.example.json")
    assert payload["question"] == STRATEGY_QUESTION
    with pytest.raises(ValidationError, match="fixed market question"):
        StrategySession.model_validate({**payload, "question": "what should we build?"})


def test_a_session_caps_surviving_cards_and_explains_failure():
    payload = fixture("strategy.session.example.json")
    with pytest.raises(ValidationError, match="must record a bounded error"):
        StrategySession.model_validate({**payload, "state": "failed", "brief_id": None})
    with pytest.raises(ValidationError, match="only a completed session"):
        StrategySession.model_validate(
            {**payload, "state": "running", "error": None, "brief_id": "brf_2026w35"}
        )
    with pytest.raises(ValidationError, match="at most 3"):
        SessionMetrics.model_validate({"cards_passed": 4})


def test_a_challenge_must_reference_a_card_in_its_own_session():
    payload = fixture("strategy.session.example.json")
    stray = {**payload["challenges"][0], "card_id": "stc_01ARZ3NDEKTSV4RRFFQ69G5H99"}
    with pytest.raises(ValidationError, match="reference a card in this session"):
        StrategySession.model_validate({**payload, "challenges": [stray]})


def test_brief_strategy_links_stay_consistent():
    payload = fixture("brief.strategy.example.json")
    with pytest.raises(ValidationError, match="owning strategy_session_id"):
        Brief.model_validate({**payload, "strategy_session_id": None})

    legacy = json.loads((FIXTURES / "brief.example.json").read_text())
    analyst_brief = Brief.model_validate(legacy)
    assert analyst_brief.strategy_session_id is None
    assert analyst_brief.strategy_card_ids == []


def test_manifest_hash_is_order_independent():
    payload = fixture("strategy.session.example.json")
    session = StrategySession.model_validate(payload)
    assert manifest_hash(session.input_manifest) == session.manifest_hash
    assert manifest_hash(list(reversed(session.input_manifest))) == session.manifest_hash


def test_session_helpers_split_passed_from_rejected():
    session = StrategySession.model_validate(fixture("strategy.session.example.json"))
    assert [card.status for card in session.passed_cards()] == [CardStatus.PASSED]
    assert [card.status for card in session.rejected_cards()] == [CardStatus.REJECTED]
