import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from google.genai import types
from opentelemetry.sdk.trace import SynchronousMultiSpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pipeline.gemini_analyst import ANALYST_VERSION, AnalystResult
from pipeline.local_backend import LocalBackend, LocalSettings
from runtime_agent.agent import analyze_delta_async, parse_runtime_request
from runtime_agent.telemetry import RedactingSpanProcessor
from schemas.config import load_config
from schemas.delta import Delta

FIXTURE = Path("schemas/fixtures/delta.example.json")
DELTA_ID = "dlt_01ARZ3NDEKTSV4RRFFQ69G5FAX"


def fixture_delta() -> Delta:
    return Delta.model_validate_json(FIXTURE.read_text())


def test_local_analyst_lease_blocks_concurrent_delivery_and_skips_completed(tmp_path):
    config = load_config("tycho.yaml")
    now = datetime(2026, 8, 25, 2, tzinfo=UTC)
    with LocalBackend(config, LocalSettings(tmp_path / "data")) as store:
        first = store.acquire_analyst_lease(
            DELTA_ID,
            "shadow",
            ANALYST_VERSION,
            "run_first",
            now,
            now + timedelta(minutes=15),
        )
        active = store.acquire_analyst_lease(
            DELTA_ID,
            "shadow",
            ANALYST_VERSION,
            "run_second",
            now + timedelta(seconds=1),
            now + timedelta(minutes=15),
        )
        assert first.state == "acquired"
        assert active.state == "active"
        assert active.run_id == "run_first"

        store.complete_analyst_lease(
            DELTA_ID, "shadow", ANALYST_VERSION, "run_first", now + timedelta(seconds=2)
        )
        completed = store.acquire_analyst_lease(
            DELTA_ID,
            "shadow",
            ANALYST_VERSION,
            "run_third",
            now + timedelta(seconds=3),
            now + timedelta(minutes=15),
        )
        assert completed.state == "completed"
        assert completed.run_id == "run_first"


def test_local_analyst_lease_retries_failed_and_expired_attempts(tmp_path):
    config = load_config("tycho.yaml")
    now = datetime(2026, 8, 25, 2, tzinfo=UTC)
    with LocalBackend(config, LocalSettings(tmp_path / "data")) as store:
        first = store.acquire_analyst_lease(
            DELTA_ID,
            "shadow",
            ANALYST_VERSION,
            "run_first",
            now,
            now + timedelta(seconds=1),
        )
        store.fail_analyst_lease(
            DELTA_ID,
            "shadow",
            ANALYST_VERSION,
            "run_first",
            now + timedelta(milliseconds=1),
            "provider unavailable",
        )
        retry = store.acquire_analyst_lease(
            DELTA_ID,
            "shadow",
            ANALYST_VERSION,
            "run_retry",
            now + timedelta(milliseconds=2),
            now + timedelta(minutes=15),
        )
        assert first.attempt == 1
        assert retry.state == "acquired"
        assert retry.attempt == 2

        store.fail_analyst_lease(
            DELTA_ID,
            "shadow",
            ANALYST_VERSION,
            "run_retry",
            now + timedelta(seconds=2),
            "timeout",
        )
        expired_retry = store.acquire_analyst_lease(
            DELTA_ID,
            "shadow",
            ANALYST_VERSION,
            "run_expired_retry",
            now + timedelta(seconds=3),
            now + timedelta(minutes=15),
        )
        assert expired_retry.state == "acquired"
        assert expired_retry.attempt == 3


def test_runtime_request_accepts_only_delta_id():
    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=json.dumps({"delta_id": DELTA_ID}, separators=(",", ":"))
            )
        ],
    )
    assert parse_runtime_request(message) == DELTA_ID

    with pytest.raises(ValueError, match="only delta_id"):
        parse_runtime_request(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=json.dumps({"delta_id": DELTA_ID, "raw": "no"}))],
            )
        )


def test_runtime_operation_returns_bounded_metadata_without_claim_body(monkeypatch):
    class FakeBackend:
        def get_delta(self, delta_id):
            assert delta_id == DELTA_ID
            return fixture_delta()

    async def fake_run_analyst(delta, config, store, *, mode):
        assert delta.delta_id == DELTA_ID
        assert mode == "shadow"
        return AnalystResult(
            run_id="run_shadow",
            delta_id=delta.delta_id,
            mode=mode,
            model="fake-model",
            actions=[{"action": "create_claim", "claim": {"statement": "secret"}}],
            final_text="private model response",
        )

    monkeypatch.setenv("TYCHO_PROJECT", "test-project")
    monkeypatch.setattr("runtime_agent.agent.run_analyst", fake_run_analyst)
    result = asyncio.run(
        analyze_delta_async(
            DELTA_ID,
            backend=FakeBackend(),
            config=load_config("tycho.yaml"),
            mode="shadow",
        )
    )
    assert result == {
        "delta_id": DELTA_ID,
        "run_id": "run_shadow",
        "state": "completed",
        "action": "create_claim",
        "analyst_version": ANALYST_VERSION,
        "model": "fake-model",
    }
    assert "secret" not in json.dumps(result)
    assert "private model response" not in json.dumps(result)


def test_trace_redactor_keeps_structure_and_safe_fields():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processors = SynchronousMultiSpanProcessor()
    processors.add_span_processor(RedactingSpanProcessor())
    processors.add_span_processor(SimpleSpanProcessor(exporter))
    provider.add_span_processor(processors)
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("tycho.analyze_delta") as span:
        span.set_attribute("tycho.delta_id", DELTA_ID)
        span.set_attribute("gcp.vertex.agent.llm_request", '{"secret":"prompt"}')
        span.set_attribute("gcp.vertex.agent.llm_response", '{"secret":"answer"}')
        span.add_event("model_payload", {"prompt": "secret"})

    exported = exporter.get_finished_spans()
    assert len(exported) == 1
    assert exported[0].attributes["tycho.delta_id"] == DELTA_ID
    assert "gcp.vertex.agent.llm_request" not in exported[0].attributes
    assert "gcp.vertex.agent.llm_response" not in exported[0].attributes
    assert exported[0].events == ()
