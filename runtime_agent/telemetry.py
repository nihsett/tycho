"""Safe Agent Runtime telemetry configuration.

Agent Platform's default ADK instrumentation can attach full model payloads to
Vertex agent span attributes. Tycho keeps the span graph, but strips all content
before the managed OTLP exporter receives a span.
"""

from __future__ import annotations

import os
from typing import Any

_SAFE_ATTRIBUTE_NAMES = frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.agent.name",
        "gen_ai.agent.description",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.response.finish_reasons",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gcp.vertex.agent.invocation_id",
        "gcp.vertex.agent.event_id",
        "gcp.vertex.agent.associated_event_ids",
        "tycho.delta_id",
        "tycho.run_id",
        "tycho.entity",
        "tycho.source",
        "tycho.action",
        "tycho.analyst_version",
        "tycho.mode",
        "tycho.result_state",
    }
)


class RedactingSpanProcessor:
    """Remove model/content attributes before downstream processors export."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        del span, parent_context

    def _on_ending(self, span: Any) -> None:
        del span

    def on_end(self, span: Any) -> None:
        attributes = {
            key: value
            for key, value in dict(span.attributes).items()
            if key in _SAFE_ATTRIBUTE_NAMES
        }
        # ReadableSpan exposes immutable views, but the SDK retains mutable
        # bounded collections underneath until the exporter consumes them.
        # Mutating those collections here keeps the existing span identity and
        # parent/child structure while removing unsafe payloads.
        if hasattr(span, "_attributes"):
            span._attributes.clear()  # noqa: SLF001 - deliberate exporter boundary
            span._attributes.update(attributes)  # noqa: SLF001
        for collection_name in ("_events", "_links"):
            collection = getattr(span, collection_name, None)
            if collection is not None and hasattr(collection, "_dq"):
                collection._dq.clear()  # noqa: SLF001 - strip exported content

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True


def build_redacted_instrumentor(project_id: str | None) -> None:
    """Install Agent Platform's exporter behind Tycho's redaction processor."""
    if not project_id:
        raise RuntimeError("a project is required for Agent Runtime telemetry")

    # Reuse the SDK's authenticated OTLP exporter and GenAI instrumentation,
    # then put our processor before its exporter processor.
    from agentplatform.agent_engines.templates.adk import (
        _default_instrumentor_builder,
    )
    from opentelemetry import trace
    from opentelemetry.sdk.trace import SynchronousMultiSpanProcessor

    _default_instrumentor_builder(
        project_id,
        enable_tracing=True,
        enable_logging=False,
    )
    provider = trace.get_tracer_provider()
    active = getattr(provider, "_active_span_processor", None)
    processors = tuple(getattr(active, "_span_processors", ()))
    if not processors:
        raise RuntimeError("Agent Platform did not install a trace exporter")

    redacted = SynchronousMultiSpanProcessor()
    redacted.add_span_processor(RedactingSpanProcessor())
    for processor in processors:
        redacted.add_span_processor(processor)
    provider._active_span_processor = redacted  # noqa: SLF001 - SDK integration

    # The ADK app sets this false as well, but keep the requirement explicit in
    # the wrapper because this is a hard production safety boundary.
    os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] = "false"
