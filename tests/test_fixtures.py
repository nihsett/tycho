import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemas import (
    Brief,
    Claim,
    DeliveryReceipt,
    Delta,
    Observation,
    SupersessionPair,
    load_config,
)

FIXTURES = Path("schemas/fixtures")


@pytest.mark.parametrize(
    ("filename", "model", "key_path"),
    [
        ("observation.example.json", Observation, ("obs_id",)),
        ("delta.example.json", Delta, ("delta_id",)),
        ("delta.semantic.meaningful.example.json", Delta, ("delta_id",)),
        ("delta.semantic.noise.example.json", Delta, ("delta_id",)),
        ("claim.fact.example.json", Claim, ("claim_id",)),
        ("claim.inference.example.json", Claim, ("claim_id",)),
        ("claim.operational.example.json", Claim, ("claim_id",)),
        ("claim.superseded.example.json", SupersessionPair, ("new", "claim_id")),
        ("brief.example.json", Brief, ("brief_id",)),
        ("receipt.example.json", DeliveryReceipt, ("receipt_id",)),
    ],
)
def test_fixture_validates_and_round_trips(filename, model, key_path):
    payload = json.loads((FIXTURES / filename).read_text())
    parsed = model.model_validate(payload)
    reparsed = model.model_validate_json(parsed.model_dump_json(by_alias=True))

    original_value = parsed
    reparsed_value = reparsed
    for part in key_path:
        original_value = getattr(original_value, part)
        reparsed_value = getattr(reparsed_value, part)
    assert reparsed_value == original_value
    assert reparsed == parsed


def test_inference_rejects_same_source_evidence():
    payload = json.loads((FIXTURES / "claim.inference.example.json").read_text())
    payload["evidence"][1]["source"] = "github_releases"
    with pytest.raises(ValidationError, match="distinct sources"):
        Claim.model_validate(payload)


def test_single_source_inference_requires_disputes_link():
    payload = json.loads((FIXTURES / "claim.inference.example.json").read_text())
    payload["evidence"] = payload["evidence"][:1]
    payload["confidence"] = "speculative"
    with pytest.raises(ValidationError, match="distinct sources"):
        Claim.model_validate(payload)

    payload["disputes"] = "clm_01ARZ3NDEKTSV4RRFFQ69G5H43"
    claim = Claim.model_validate(payload)
    assert claim.disputes == payload["disputes"]


def test_intent_or_future_inference_cannot_be_likely_in_model():
    payload = json.loads((FIXTURES / "claim.inference.example.json").read_text())
    payload["inference_kind"] = "intent_or_future"
    with pytest.raises(ValidationError, match="intent_or_future"):
        Claim.model_validate(payload)


def test_naive_timestamp_is_rejected():
    payload = json.loads((FIXTURES / "observation.example.json").read_text())
    payload["fetched_at"] = "2026-08-20T02:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        Observation.model_validate(payload)


def test_repository_config_validates_at_startup():
    config = load_config("tycho.yaml")
    assert list(config.entities) == ["claude_code", "codex", "gemini_cli", "pi"]
    assert config.entities["pi"].sources.github_releases.repo == "earendil-works/pi"
    assert str(config.entities["pi"].sources.website_changelog.url) == (
        "https://pi.dev/news/releases?page=1"
    )
