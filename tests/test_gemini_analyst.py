import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline.gemini_analyst import (
    ANALYST_VERSION,
    AnalystInputTooLarge,
    AnalystResult,
    AnalystToolbox,
    build_input_envelope,
    run_analyst,
)
from pipeline.local_backend import LocalBackend, LocalSettings
from schemas.config import load_config
from schemas.delta import Delta

FIXTURES = Path("schemas/fixtures")


def fixture_delta() -> Delta:
    return Delta.model_validate_json((FIXTURES / "delta.example.json").read_text())


def test_shadow_tool_validates_but_does_not_write_claim(tmp_path):
    config = load_config("tycho.yaml")
    delta = fixture_delta()
    with LocalBackend(config, LocalSettings(tmp_path / "data")) as store:
        store.insert_delta(delta)
        tools = AnalystToolbox(delta, config, store, mode="shadow")
        result = tools.create_claim(
            delta_id=delta.delta_id,
            scope="product/capabilities",
            claim_class="fact",
            statement="Claude Code published v2.1.237 on 2026-08-20.",
            rationale="The release may change coding-agent workflows.",
            confidence="confirmed",
            severity="notable",
            evidence_delta_ids=[delta.delta_id],
            evidence_notes=["Official changelog delta."],
        )

        assert result["status"] == "accepted"
        assert result["action"] == "create_claim"
        assert store.claims() == []

        second = tools.no_action(delta.delta_id, "Nothing else changed.")
        assert second["status"] == "rejected"
        assert "exactly one" in second["error"]


def test_inference_with_one_source_is_rejected_by_tool_layer(tmp_path):
    config = load_config("tycho.yaml")
    delta = fixture_delta()
    with LocalBackend(config, LocalSettings(tmp_path / "data")) as store:
        store.insert_delta(delta)
        tools = AnalystToolbox(delta, config, store, mode="shadow")
        result = tools.create_claim(
            delta_id=delta.delta_id,
            scope="product/roadmap",
            claim_class="inference",
            statement="Claude Code is accelerating releases.",
            rationale="One release alone is insufficient evidence.",
            confidence="likely",
            severity="notable",
            evidence_delta_ids=[delta.delta_id],
            evidence_notes=["One official release."],
            inference_kind="present_state",
        )

        assert result["status"] == "rejected"
        assert "distinct sources" in result["error"]
        assert tools.actions == []


def test_input_envelope_contains_only_delta_claims_and_entity_context(tmp_path):
    config = load_config("tycho.yaml")
    delta = fixture_delta()
    with LocalBackend(config, LocalSettings(tmp_path / "data")) as store:
        store.insert_delta(delta)
        envelope = build_input_envelope(delta, config, store)

    assert set(envelope) == {
        "delta",
        "scope_claims",
        "entity_context",
        "market_claims",
    }
    assert envelope["delta"]["delta_id"] == delta.delta_id
    assert "content_ref" not in str(envelope)


def test_oversized_input_is_recorded_before_any_model_call(tmp_path, monkeypatch):
    config = load_config("tycho.yaml")
    delta = fixture_delta()
    monkeypatch.setattr(
        "pipeline.gemini_analyst.build_input_envelope",
        lambda *_args: {"delta": "x" * 250_000},
    )

    def unexpected_model_call(*_args, **_kwargs):
        raise AssertionError("Gemini must not receive an oversized analyst input")

    monkeypatch.setattr("pipeline.gemini_analyst.Agent", unexpected_model_call)
    with LocalBackend(config, LocalSettings(tmp_path / "data")) as store:
        store.insert_delta(delta)
        with pytest.raises(AnalystInputTooLarge):
            asyncio.run(run_analyst(delta, config, store, mode="shadow"))
        run = store.analyst_runs()[0]

    assert run["state"] == "failed"
    assert "analyst input is" in run["error"]
    assert "limit is" in run["error"]


def test_shadow_runs_before_authoritative_stub_without_blocking_it(tmp_path, monkeypatch):
    config = load_config("tycho.yaml")
    delta = fixture_delta()
    calls = []

    def fake_shadow(current_delta, current_config, store, *, force=False):
        assert store.claims() == []
        calls.append(current_delta.delta_id)
        return AnalystResult(
            run_id="run_test",
            delta_id=current_delta.delta_id,
            mode="shadow",
            model="fake",
            actions=[{"action": "create_claim"}],
            final_text="",
        )

    monkeypatch.setenv("TYCHO_ANALYST_MODE", "shadow")
    monkeypatch.setattr("pipeline.gemini_analyst.run_shadow_sync", fake_shadow)
    with LocalBackend(config, LocalSettings(tmp_path / "data")) as store:
        store.insert_delta(delta)
        store.publish_delta(delta)
        assert calls == [delta.delta_id]
        assert len(store.claims()) == 1
        assert store.pending_count() == 0


def test_analyst_run_log_is_durable(tmp_path):
    config = load_config("tycho.yaml")
    delta = fixture_delta()
    settings = LocalSettings(tmp_path / "data")
    started = datetime(2026, 8, 20, 5, tzinfo=UTC)
    with LocalBackend(config, settings) as store:
        store.insert_delta(delta)
        store.start_analyst_run(
            "run_01ARZ3NDEKTSV4RRFFQ69G5FBB",
            delta.delta_id,
            "shadow",
            ANALYST_VERSION,
            "fake-model",
            "{}",
            started,
        )
        store.finish_analyst_run(
            "run_01ARZ3NDEKTSV4RRFFQ69G5FBB",
            actions=[{"action": "no_action", "reason": "test"}],
            final_text="done",
            finished_at=started,
        )

    with LocalBackend(config, settings) as store:
        runs = store.analyst_runs()
        assert runs[0]["state"] == "completed"
        assert runs[0]["actions"][0]["action"] == "no_action"
        assert store.has_completed_analyst_run(
            delta.delta_id, "shadow", ANALYST_VERSION
        )
