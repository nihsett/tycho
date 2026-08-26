import json
from datetime import UTC, datetime
import pytest

from adapters.github import GithubFetch
from pipeline.acquire import acquire_github_releases
from pipeline.local_backend import LocalBackend, LocalSettings
from pipeline.local_tracer import StaticAdapter
from pipeline.semantic_differ import (
    SemanticDiffer,
    SemanticValidationError,
    build_comparison_bundle,
    comparison_id_for,
    normalized_snapshot,
    quote_is_grounded,
    retry_incomplete_generation_pairs,
)
from schemas.config import load_config


class Usage:
    prompt_token_count = 120
    candidates_token_count = 40
    thoughts_token_count = 10


class Response:
    usage_metadata = Usage()

    def __init__(self, payload):
        self.text = json.dumps(payload)


class FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        return Response(self.responses.pop(0))


class FakeClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)


def release(tag, body):
    return {
        "tag_name": tag,
        "name": tag,
        "body": body,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-08-25T00:00:00Z",
        "target_commitish": "main",
        "html_url": "https://example.test/release",
    }


def adapter(releases):
    payload = json.dumps(releases, separators=(",", ":")).encode()
    return StaticAdapter(GithubFetch("earendil-works/pi", payload, releases))


def meaningful_response():
    return {
        "status": "meaningful",
        "summary": "Pi added optional PowerShell execution.",
        "reason": "The release adds a durable user-facing capability.",
        "changes": [
            {
                "category": "capability",
                "scope": "product/capabilities",
                "statement": "Pi added optional native PowerShell execution on Windows.",
                "before": "",
                "after": "Native PowerShell execution is available as an optional tool.",
                "evidence_before": "",
                "evidence_after": "Added an optional `powershell` tool for Windows.",
            }
        ],
    }


def test_github_bundle_keeps_only_semantic_release_fields():
    payload = json.dumps([release("v1", "old")]).encode()
    snapshot = normalized_snapshot("github_releases", payload)
    assert set(snapshot[0]) == {
        "tag_name",
        "name",
        "body",
        "draft",
        "prerelease",
        "published_at",
    }
    bundle = build_comparison_bundle("pi", "github_releases", payload, payload)
    assert "target_commitish" not in bundle.document
    assert "html_url" not in bundle.document


def test_grounding_never_assembles_a_quote_across_source_fields():
    snapshot = {"first": "Added native", "second": "PowerShell execution"}
    assert quote_is_grounded("Added native", snapshot)
    assert not quote_is_grounded("Added native PowerShell execution", snapshot)


def test_comparison_identity_includes_model_and_prompt_contract():
    baseline = comparison_id_for("obs_before", "obs_after")
    assert baseline != comparison_id_for(
        "obs_before", "obs_after", generated_by="gemini-3.7-flash@semantic-differ-2"
    )
    assert baseline != comparison_id_for(
        "obs_before", "obs_after", prompt_version="semantic-delta@3"
    )


def test_wrong_side_quote_rejects_the_whole_proposal():
    client = FakeClient(
        [
            {
                **meaningful_response(),
                "changes": [
                    {
                        **meaningful_response()["changes"][0],
                        "evidence_after": "old",
                    }
                ],
            }
        ]
    )
    differ = SemanticDiffer(client=client)
    before = json.dumps([release("v1", "old")]).encode()
    after = json.dumps([release("v2", "new")]).encode()
    with pytest.raises(SemanticValidationError, match="evidence_after"):
        differ.compare(
            "pi",
            "github_releases",
            before,
            after,
            obs_before="obs_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            obs_after="obs_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        )


def test_semantic_generation_failure_is_retryable(tmp_path, monkeypatch):
    monkeypatch.setenv("TYCHO_ANALYST_MODE", "stub")
    config = load_config("tycho.yaml")
    entity = config.entities["pi"]
    bad = {
        **meaningful_response(),
        "changes": [
            {**meaningful_response()["changes"][0], "evidence_after": "invented quote"}
        ],
    }
    client = FakeClient([bad, meaningful_response()])
    differ = SemanticDiffer(client=client)
    settings = LocalSettings(tmp_path / "data")
    before_at = datetime(2026, 8, 25, tzinfo=UTC)
    with LocalBackend(config, settings) as backend:
        assert (
            acquire_github_releases(
                "pi",
                entity,
                backend,
                adapter([release("v1", "old")]),
                now=before_at,
                mode="semantic",
                differ=differ,
            ).outcome
            == "bootstrapped"
        )
        failed = acquire_github_releases(
            "pi",
            entity,
            backend,
            adapter(
                [
                    release("v2", "Added an optional `powershell` tool for Windows."),
                    release("v1", "old"),
                ]
            ),
            now=before_at.replace(day=26),
            mode="semantic",
            differ=differ,
        )
        assert failed.outcome == "generation_failed"
        assert backend.deltas() == []
        assert backend.generation_runs()[0]["state"] == "failed"

        retried = retry_incomplete_generation_pairs(backend, differ, mode="semantic")
        assert retried[0].outcome == "meaningful"
        assert len(backend.deltas()) == 1
        assert backend.deltas()[0].schema_version.value == "delta@2"
        assert client.models.calls == 2


def test_noise_semantic_delta_has_no_outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("TYCHO_ANALYST_MODE", "stub")
    config = load_config("tycho.yaml")
    entity = config.entities["pi"]
    response = {
        "status": "noise",
        "summary": "No durable product change was found.",
        "reason": "Only routine nightly bookkeeping changed.",
        "changes": [],
    }
    differ = SemanticDiffer(client=FakeClient([response]))
    with LocalBackend(config, LocalSettings(tmp_path / "data")) as backend:
        acquire_github_releases(
            "pi", entity, backend, adapter([release("v1", "old")]), mode="semantic", differ=differ
        )
        result = acquire_github_releases(
            "pi",
            entity,
            backend,
            adapter([release("v2", "routine"), release("v1", "old")]),
            now=datetime(2026, 8, 26, tzinfo=UTC),
            mode="semantic",
            differ=differ,
        )
        assert result.outcome == "noise"
        assert backend.deltas()[0].changes == []
        assert backend.pending_count() == 0
