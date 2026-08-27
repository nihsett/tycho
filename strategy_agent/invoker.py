"""The single boundary where a strategy agent actually reaches a model.

Keeping this behind a protocol means the whole council workflow - context,
validation, challenge gating, citation checking, persistence - runs and is
tested end to end without a provider call.  ``AdkAgentInvoker`` is the only
class here that talks to Gemini, and it does so through Vertex/Agent Platform
ADC exactly like the rest of Tycho.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from google.adk.agents import LlmAgent
from google.genai import types


@dataclass(frozen=True)
class AgentInvocation:
    """One bounded agent turn: structured payload plus safe usage metadata."""

    payload: dict[str, Any]
    run_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    model: str = ""


class AgentInvoker(Protocol):
    async def invoke(
        self, agent: LlmAgent, request: str, *, run_id: str
    ) -> AgentInvocation: ...


class StrategyModelError(RuntimeError):
    """An agent turn produced no usable structured output."""


#: ADK's task mode does not answer with text.  A strict-structured task agent
#: delivers its output as the arguments of one ``finish_task`` function call,
#: and the runner's final event carries only the tool's confirmation string.
#: Reading the text alone therefore finds nothing at all.
FINISH_TASK_TOOL_NAME = "finish_task"


def structured_payload(events: list[Any]) -> dict[str, Any] | None:
    """Pull the structured output out of the last ``finish_task`` call.

    The last call is the right one: a schema-validation failure makes ADK hand
    the error back to the model and retry, so earlier calls are rejected drafts.
    """
    for event in reversed(events):
        content = getattr(event, "content", None)
        for part in reversed(getattr(content, "parts", None) or []):
            call = getattr(part, "function_call", None)
            if call is None or getattr(call, "name", None) != FINISH_TASK_TOOL_NAME:
                continue
            args = dict(getattr(call, "args", None) or {})
            # A non-object output schema is wrapped under a single "result"
            # key; every council schema is an object and arrives unwrapped.
            if set(args) == {"result"} and isinstance(args["result"], dict):
                return args["result"]
            return args
    return None


@dataclass
class AdkAgentInvoker:
    """Run one ADK agent turn through an in-memory session and return its JSON.

    The runner is deliberately single-turn: the council has exactly one
    Strategist pass and one Challenger pass, so there is no open-ended loop for
    a model to talk its way around a failed check.
    """

    app_name: str = "tycho-strategy"
    user_id: str = "strategy"
    _configured: bool = field(default=False, repr=False)

    def _configure(self) -> None:
        if self._configured:
            return
        import os

        from pipeline.semantic_differ import configure_vertex_adc

        configure_vertex_adc(
            os.getenv("TYCHO_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
        )
        self._configured = True

    async def invoke(
        self, agent: LlmAgent, request: str, *, run_id: str
    ) -> AgentInvocation:
        self._configure()
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        session_service = InMemorySessionService()
        runner = Runner(
            agent=agent,
            app_name=self.app_name,
            session_service=session_service,
        )
        await session_service.create_session(
            app_name=self.app_name, user_id=self.user_id, session_id=run_id
        )
        message = types.Content(role="user", parts=[types.Part(text=request)])
        started = time.monotonic()
        events: list[Any] = []
        parts: list[str] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        async for event in runner.run_async(
            user_id=self.user_id, session_id=run_id, new_message=message
        ):
            events.append(event)
            metadata = getattr(event, "usage_metadata", None)
            if metadata is not None:
                usage["input_tokens"] += int(getattr(metadata, "prompt_token_count", 0) or 0)
                usage["output_tokens"] += int(
                    getattr(metadata, "candidates_token_count", 0) or 0
                )
                usage["total_tokens"] += int(getattr(metadata, "total_token_count", 0) or 0)
            if event.is_final_response() and event.content:
                parts.extend(part.text for part in event.content.parts or [] if part.text)
        latency_ms = int((time.monotonic() - started) * 1000)

        payload = structured_payload(events)
        if payload is None:
            # Fall back to a JSON text answer for any non-task-mode agent.
            text = "\n".join(part for part in parts if part).strip()
            if not text:
                raise StrategyModelError(f"{agent.name} returned no structured output")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                # Never echo the response: it may contain claim text or quotes.
                raise StrategyModelError(f"{agent.name} returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise StrategyModelError(f"{agent.name} returned a non-object response")
        return AgentInvocation(
            payload=payload,
            run_id=run_id,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            total_tokens=usage["total_tokens"],
            latency_ms=latency_ms,
            model=str(agent.model),
        )
