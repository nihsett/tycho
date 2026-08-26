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
        parts: list[str] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        async for event in runner.run_async(
            user_id=self.user_id, session_id=run_id, new_message=message
        ):
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
