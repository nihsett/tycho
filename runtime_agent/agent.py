"""Production Tycho analyst wrapped as a no-extra-model ADK application."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import agentplatform
from agentplatform.agent_engines import AdkApp
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.genai import types
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import TypeAdapter

from pipeline.cloud import CloudBackend, CloudSettings
from pipeline.gemini_analyst import ANALYST_VERSION, run_analyst
from runtime_agent.telemetry import build_redacted_instrumentor
from schemas.common import DeltaId
from schemas.config import TychoConfig, load_config
from schemas.delta import DeltaSchemaVersion, Triage

DEFAULT_PROJECT = "gen-lang-client-0110801105"
DEFAULT_RUNTIME_LOCATION = "us-central1"
RUNTIME_APP_NAME = "tycho-analyst-runtime"
_runtime_delta_id = TypeAdapter(DeltaId)


def _project() -> str:
    return os.getenv("TYCHO_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT") or DEFAULT_PROJECT


def _runtime_location() -> str:
    return os.getenv(
        "TYCHO_RUNTIME_LOCATION",
        os.getenv("TYCHO_PLATFORM_RUNTIME_LOCATION", DEFAULT_RUNTIME_LOCATION),
    )


def _validated_delta_id(value: Any) -> str:
    try:
        return _runtime_delta_id.validate_python(value)
    except Exception as exc:
        raise ValueError("delta_id must be a valid Tycho delta ID") from exc


def parse_runtime_request(message: types.Content | None) -> str:
    """Accept only the dispatcher contract: a JSON object containing delta_id."""
    if message is None or not message.parts or len(message.parts) != 1:
        raise ValueError("runtime request must contain one JSON text part")
    text = message.parts[0].text
    if not text:
        raise ValueError("runtime request must contain JSON text")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("runtime request is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"delta_id"}:
        raise ValueError("runtime request must contain only delta_id")
    return _validated_delta_id(payload["delta_id"])


def _bounded_result(result: Any) -> dict[str, Any]:
    action = None
    if result.actions:
        action = result.actions[0].get("action")
    return {
        "delta_id": result.delta_id,
        "run_id": result.run_id,
        "state": "skipped" if result.skipped else "completed",
        "action": action,
        "analyst_version": ANALYST_VERSION,
        "model": result.model,
    }


async def analyze_delta_async(
    delta_id: str,
    *,
    backend: CloudBackend | None = None,
    config: TychoConfig | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Run the existing analyst for one canonical meaningful BigQuery delta."""
    validated_delta_id = _validated_delta_id(delta_id)
    settings = CloudSettings.from_env()
    store = backend or CloudBackend(settings)
    runtime_config = config or load_config(settings.config_path)
    analyst_mode = mode or os.getenv("TYCHO_ANALYST_MODE", "shadow")
    if analyst_mode not in {"shadow", "live"}:
        raise ValueError("TYCHO_ANALYST_MODE must be shadow or live")

    delta = store.get_delta(validated_delta_id)
    if delta is None:
        raise ValueError(f"unknown canonical delta: {validated_delta_id}")
    if delta.schema_version is not DeltaSchemaVersion.V2:
        raise ValueError(f"noncanonical delta delivery rejected: {validated_delta_id}")
    if delta.triage is not Triage.MEANINGFUL:
        raise ValueError(f"delta is not meaningful: {validated_delta_id}")

    tracer = trace.get_tracer("tycho.runtime")
    span = tracer.start_span("tycho.analyze_delta")
    span.set_attributes(
        {
            "tycho.delta_id": delta.delta_id,
            "tycho.entity": delta.entity,
            "tycho.source": delta.source,
            "tycho.analyst_version": ANALYST_VERSION,
            "tycho.mode": analyst_mode,
        }
    )
    try:
        with trace.use_span(span, end_on_exit=False):
            result = await run_analyst(
                delta,
                runtime_config,
                store,
                mode=analyst_mode,
            )
        bounded = _bounded_result(result)
        span.set_attributes(
            {
                "tycho.run_id": result.run_id or "",
                "tycho.action": bounded["action"] or "",
                "tycho.result_state": bounded["state"],
            }
        )
        return bounded
    except Exception:
        span.set_attribute("tycho.result_state", "failed")
        span.set_status(Status(StatusCode.ERROR))
        raise
    finally:
        span.end()


def analyze_delta(
    delta_id: str,
    *,
    backend: CloudBackend | None = None,
    config: TychoConfig | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Synchronous helper for local verification and operational scripts."""
    return asyncio.run(
        analyze_delta_async(
            delta_id,
            backend=backend,
            config=config,
            mode=mode,
        )
    )


class RuntimeAnalystAgent(BaseAgent):
    """Deterministic runtime adapter that delegates directly to run_analyst."""

    async def _run_async_impl(self, ctx: InvocationContext):
        delta_id = parse_runtime_request(ctx.user_content)
        result = await analyze_delta_async(delta_id)
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(text=json.dumps(result, sort_keys=True))
                ],
            ),
        )


def build_memory_service() -> InMemoryMemoryService:
    """Keep Tycho's claims as authority; do not enable Memory Bank retrieval."""
    return InMemoryMemoryService()


agentplatform.init(project=_project(), location=_runtime_location())
root_agent = RuntimeAnalystAgent(
    name="tycho_analyst_runtime",
    description="Runs Tycho's existing bounded analyst for one canonical delta.",
)
app = AdkApp(
    agent=root_agent,
    app_name=RUNTIME_APP_NAME,
    # None lets the managed telemetry flag enable tracing without forcing ADK
    # to turn message-content capture back on.
    enable_tracing=None,
    memory_service_builder=build_memory_service,
    instrumentor_builder=build_redacted_instrumentor,
)
