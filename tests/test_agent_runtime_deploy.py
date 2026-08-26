import sys
from argparse import Namespace
from types import SimpleNamespace

from infra import deploy_agent_runtime


def deployment_args(mode: str) -> Namespace:
    return Namespace(
        project="test-project",
        location="us-central1",
        bucket="test-staging",
        analyst_mode=mode,
    )


def production_state() -> dict:
    return {
        "project": "test-project",
        "dispatcher_url": "https://dispatcher.example.run.app",
        "dispatcher_service_account": "dispatcher@test-project.iam.gserviceaccount.com",
        "live_subscription": {
            "name": "tycho-analyst-push",
            "recorded_old_endpoint": "https://old.example.run.app",
        },
    }


def test_runtime_config_propagates_live_mode() -> None:
    config = deploy_agent_runtime.runtime_deployment_config(deployment_args("live"))

    assert config["env_vars"]["TYCHO_ANALYST_MODE"] == "live"
    assert config["labels"]["environment"] == "production-live"
    assert "prepared for production traffic" in config["description"]


def test_prepare_live_sets_live_mode_before_resume(monkeypatch) -> None:
    captured = {}

    def fake_resume(args):
        captured["mode"] = args.analyst_mode

    monkeypatch.setattr(deploy_agent_runtime, "resume", fake_resume)
    monkeypatch.setattr(sys, "argv", ["deploy-agent-runtime", "prepare-live"])

    deploy_agent_runtime.main()

    assert captured == {"mode": "live"}


def test_cutover_command_sets_endpoint_identity_and_audience() -> None:
    command = deploy_agent_runtime.build_cutover_command(production_state())

    assert command[:5] == [
        "gcloud",
        "pubsub",
        "subscriptions",
        "modify-push-config",
        "tycho-analyst-push",
    ]
    assert "--push-endpoint=https://dispatcher.example.run.app" in command
    assert (
        "--push-auth-service-account="
        "dispatcher@test-project.iam.gserviceaccount.com"
    ) in command
    assert "--push-auth-token-audience=https://dispatcher.example.run.app" in command
    assert "--project=test-project" in command


def test_pubsub_service_agent_gets_token_creator_on_dispatcher_only(monkeypatch) -> None:
    calls = []

    def fake_run(*args, capture=False):
        calls.append((args, capture))
        if args[:3] == ("gcloud", "projects", "describe"):
            return SimpleNamespace(stdout="123456789\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(deploy_agent_runtime, "run", fake_run)
    member = deploy_agent_runtime.grant_pubsub_token_creator(
        "test-project", "dispatcher@test-project.iam.gserviceaccount.com"
    )

    assert member == (
        "serviceAccount:service-123456789@gcp-sa-pubsub.iam.gserviceaccount.com"
    )
    binding = calls[1][0]
    assert binding[:5] == (
        "gcloud",
        "iam",
        "service-accounts",
        "add-iam-policy-binding",
        "dispatcher@test-project.iam.gserviceaccount.com",
    )
    assert "--role=roles/iam.serviceAccountTokenCreator" in binding
    assert member in binding
