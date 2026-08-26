"""The Tycho Strategy Council ADK application.

This module builds the app; it does not deploy or start one.  Nothing here runs
at import time, so importing ``strategy_agent`` never contacts Google Cloud.
Deployment of the managed ``Tycho Strategy Council`` Agent Runtime is a separate,
explicitly gated step and is not performed by this task.
"""

from __future__ import annotations

from typing import Any

from google.adk.agents import SequentialAgent

from strategy_agent.agents import (
    DEFAULT_STRATEGY_MODEL,
    build_brief_writer,
    build_challenger,
    build_strategist,
)

STRATEGY_APP_NAME = "tycho-strategy-council"
COUNCIL_AGENT_NAME = "tycho_strategy_council"

#: The identity the Runtime needs, and nothing more.  No GCS, no Pub/Sub, no
#: broad Storage, and no Memory Bank: Firestore claims remain institutional truth.
REQUIRED_ROLES = (
    "roles/datastore.user",
    "roles/bigquery.dataViewer",
    "roles/bigquery.jobUser",
    "roles/cloudtrace.agent",
)
FORBIDDEN_ROLE_PREFIXES = (
    "roles/storage",
    "roles/pubsub",
    "roles/aiplatform.memoryBank",
)


def build_council_agent(model: str = DEFAULT_STRATEGY_MODEL) -> SequentialAgent:
    """The named sub-agent sequence, so traces read left to right.

    The Python gates between the agents are not ADK steps: they run in
    ``strategy_agent.council`` around this sequence, because a model must never
    be able to route around a hard check.
    """
    return SequentialAgent(
        name=COUNCIL_AGENT_NAME,
        description="Tycho's bounded strategy council: strategist, challenger, brief writer.",
        sub_agents=[
            build_strategist(model),
            build_challenger(model),
            build_brief_writer(model),
        ],
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
