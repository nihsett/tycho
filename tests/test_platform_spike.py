from agentplatform.agent_engines import AdkApp

from platform_spike.agent import SPIKE_VERSION, app, platform_probe, root_agent


def test_platform_probe_is_bounded_and_deterministic() -> None:
    result = platform_probe("agent-runtime")
    assert result == {
        "status": "ready",
        "component": "agent-runtime",
        "agent_role": "non-production platform probe",
        "version": SPIKE_VERSION,
    }


def test_platform_spike_is_an_adk_app_with_one_tool() -> None:
    assert isinstance(app, AdkApp)
    assert root_agent.name == "tycho_platform_probe"
    assert len(root_agent.tools) == 1
