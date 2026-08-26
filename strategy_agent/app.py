"""The Tycho Strategy Council ADK application.

The deployed root agent is a deterministic wrapper, not a raw agent sequence.
A ``SequentialAgent`` of the three LlmAgents would run them back to back with no
Python between them, which is exactly where every hard evidence check lives.
The Runtime therefore enters through ``StrategyCouncilAgent``, which parses only
a bounded ``StrategyRequest`` and delegates to the governed workflow in
``strategy_agent.council``.

This module builds the app; it does not deploy or start one.  Nothing here runs
at import time, so importing ``strategy_agent`` never contacts Google Cloud.
Deployment of the managed ``Tycho Strategy Council`` Agent Runtime is a separate,
explicitly gated step and is not performed by this task.
"""

from __future__ import annotations

import json
import os
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from strategy_agent.agents import (
    BRIEF_WRITER_NAME,
    CHALLENGER_NAME,
    DEFAULT_STRATEGY_MODEL,
    STRATEGIST_NAME,
)
from strategy_agent.errors import Stage, safe_error_text
from strategy_agent.request import StrategyRequestError, parse_strategy_request

STRATEGY_APP_NAME = "tycho-strategy-council"
COUNCIL_AGENT_NAME = "tycho_strategy_council"

#: The named sub-agents the council drives.  They are invoked only through the
#: governed workflow, never as a free-running sequence.
COUNCIL_SUB_AGENT_NAMES = (STRATEGIST_NAME, CHALLENGER_NAME, BRIEF_WRITER_NAME)

#: The identity the Runtime needs, and nothing more.  No GCS, no Pub/Sub, no
#: broad Storage, and no Memory Bank: Firestore claims remain institutional truth.
REQUIRED_ROLES = (
    "roles/datastore.user",
    "roles/bigquery.dataViewer",
    "roles/bigquery.jobUser",
    "roles/telemetry.tracesWriter",
)
FORBIDDEN_ROLE_PREFIXES = (
    "roles/storage",
    "roles/pubsub",
    "roles/aiplatform.memoryBank",
    "roles/cloudtrace",
)


def bounded_session_result(result: Any) -> dict[str, Any]:
    """The only shape the Runtime returns: IDs, state, and counts.

    No card statements, rationales, premises, brief prose, or claim text.
    """
    session = result.session
    return {
        "session_id": session.session_id,
        "strategy_version": session.strategy_version,
        "state": session.state.value,
        "cards_proposed": session.metrics.cards_proposed,
        "cards_passed": session.metrics.cards_passed,
        "cards_rejected": session.metrics.cards_rejected,
        "brief_id": session.brief_id,
        "skipped": bool(result.skipped),
    }


async def run_strategy_request(payload: bytes | str | dict[str, Any]) -> dict[str, Any]:
    """Parse one bounded trigger and run the governed council workflow.

    The backend and config are constructed inside the request, exactly like the
    analyst Runtime, so the deployed object holds no live client.
    """
    from pipeline.cloud import CloudBackend, CloudSettings
    from schemas.config import load_config
    from strategy_agent.council import run_strategy_session
    from strategy_agent.invoker import AdkAgentInvoker

    parsed = parse_strategy_request(payload)
    settings = CloudSettings.from_env()
    store = CloudBackend(settings)
    config = load_config(settings.config_path)
    model = os.getenv("TYCHO_STRATEGY_MODEL", DEFAULT_STRATEGY_MODEL)

    result = await run_strategy_session(
        store,
        config,
        AdkAgentInvoker(),
        period=parsed.period,
        model=model,
        strategy_version=parsed.request.strategy_version,
    )
    return bounded_session_result(result)


class StrategyCouncilAgent(BaseAgent):
    """Deterministic Runtime adapter for the governed strategy workflow.

    There is no outer model here and no prompt input.  The request contract
    accepts a bounded period and a request ID; anything else is rejected before
    a single agent runs.
    """

    async def _run_async_impl(self, ctx: InvocationContext):
        try:
            payload = _request_text(ctx.user_content)
            result = await run_strategy_request(payload)
        except Exception as exc:
            stage = (
                Stage.UNKNOWN
                if isinstance(exc, StrategyRequestError)
                else Stage.PERSISTENCE
            )
            result = {"state": "failed", "error": safe_error_text(exc, stage)}
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=json.dumps(result, sort_keys=True))],
            ),
        )


def _request_text(message: types.Content | None) -> str:
    """Accept only the dispatcher contract: one JSON text part."""
    if message is None or not message.parts or len(message.parts) != 1:
        raise StrategyRequestError("strategy request must contain one JSON text part")
    text = message.parts[0].text
    if not text:
        raise StrategyRequestError("strategy request must contain JSON text")
    return text


def build_council_agent(model: str = DEFAULT_STRATEGY_MODEL) -> StrategyCouncilAgent:
    """The Runtime root: a deterministic wrapper around the governed workflow.

    ``model`` is accepted for symmetry with the agent builders and is resolved
    per request from ``TYCHO_STRATEGY_MODEL``; the wrapper itself calls no model.
    """
    del model
    return StrategyCouncilAgent(
        name=COUNCIL_AGENT_NAME,
        description=(
            "Runs Tycho's bounded strategy council for one period: strategist, "
            "challenger, and brief writer behind Python evidence gates."
        ),
    )


def build_strategy_app(model: str = DEFAULT_STRATEGY_MODEL, **kwargs: Any) -> Any:
    """Construct the managed Agent Runtime app.

    Imported lazily so that the strategy package stays usable - and testable -
    without Agent Platform credentials.
    """
    import agentplatform
    from agentplatform.agent_engines import AdkApp

    from runtime_agent.agent import _project, _runtime_location
    from runtime_agent.telemetry import build_redacted_instrumentor

    agentplatform.init(project=_project(), location=_runtime_location())
    return AdkApp(
        agent=build_council_agent(model),
        app_name=STRATEGY_APP_NAME,
        # Keep the span graph, drop message content: strategy traces must carry
        # no prompt, response, claim text, or evidence.
        enable_tracing=None,
        instrumentor_builder=build_redacted_instrumentor,
        **kwargs,
    )
