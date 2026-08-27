"""The dashboard deployment boundary: what it may and may not touch."""

from __future__ import annotations

import pytest

from infra import deploy_dashboard as deploy
from infra.deploy_strategy_council import ProtectedResourceError

DASHBOARD_SA = "serviceAccount:tycho-dashboard@gen-lang-client-0110801105.iam.gserviceaccount.com"


def allowed(*tokens: str) -> bool:
    try:
        deploy.guard_command(tokens)
    except ProtectedResourceError:
        return False
    return True


# --- Refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    "tokens",
    [
        ("gcloud", "pubsub", "subscriptions", "modify-push-config", "tycho-analyst-push"),
        ("gcloud", "scheduler", "jobs", "pause", "tycho-nightly"),
        ("gcloud", "scheduler", "jobs", "delete", "tycho-strategy-weekly"),
        ("gcloud", "run", "services", "delete", "tycho-strategy-dispatcher"),
        ("gcloud", "run", "services", "update", "tycho-analyst-dispatcher"),
        ("gcloud", "run", "jobs", "update", "tycho-acquire"),
        ("gcloud", "pubsub", "topics", "delete", "tycho-deltas"),
        ("gcloud", "run", "services", "update", "--service=tycho-nightly"),
        ("gcloud", "ai", "reasoningEngines", "delete", "projects/x/reasoningEngines/1"),
    ],
)
def test_a_write_naming_a_protected_resource_is_refused(tokens):
    assert allowed(*tokens) is False


def test_the_dispatcher_exemption_is_exactly_one_binding():
    assert allowed(
        "gcloud",
        "run",
        "services",
        "add-iam-policy-binding",
        "tycho-strategy-dispatcher",
        "--region=us-central1",
        f"--member={DASHBOARD_SA}",
        "--role=roles/run.invoker",
    )


@pytest.mark.parametrize(
    "role,member",
    [
        ("roles/run.admin", DASHBOARD_SA),
        ("roles/editor", DASHBOARD_SA),
        ("roles/run.invoker", "allUsers"),
        ("roles/run.invoker", "user:someone@example.com"),
        ("roles/run.invoker", "serviceAccount:tycho-agent-dispatcher@p.iam.gserviceaccount.com"),
    ],
)
def test_a_wider_role_or_other_member_is_not_the_exemption(role, member):
    assert (
        allowed(
            "gcloud",
            "run",
            "services",
            "add-iam-policy-binding",
            "tycho-strategy-dispatcher",
            f"--member={member}",
            f"--role={role}",
        )
        is False
    )


def test_removing_a_binding_is_not_the_exemption():
    assert (
        allowed(
            "gcloud",
            "run",
            "services",
            "remove-iam-policy-binding",
            "tycho-strategy-dispatcher",
            f"--member={DASHBOARD_SA}",
            "--role=roles/run.invoker",
        )
        is False
    )


# --- Permitted --------------------------------------------------------------


@pytest.mark.parametrize(
    "tokens",
    [
        ("gcloud", "run", "services", "describe", "tycho-strategy-dispatcher"),
        ("gcloud", "run", "services", "get-iam-policy", "tycho-strategy-dispatcher"),
        ("gcloud", "scheduler", "jobs", "describe", "tycho-nightly"),
        ("gcloud", "pubsub", "subscriptions", "describe", "tycho-analyst-push"),
    ],
)
def test_reads_against_protected_resources_still_work_for_evidence(tokens):
    assert allowed(*tokens) is True


def test_deploying_the_dashboard_is_not_caught_by_the_guard():
    assert allowed(
        "gcloud",
        "run",
        "deploy",
        "tycho-dashboard",
        "--source=.",
        "--set-env-vars=TYCHO_STRATEGY_DISPATCHER_URL="
        "https://tycho-strategy-dispatcher-u2s544lf5a-uc.a.run.app",
        "--no-allow-unauthenticated",
    )


def test_a_dispatcher_url_in_a_flag_value_is_not_a_target():
    assert deploy.names_protected_resource(
        ("gcloud", "run", "deploy", "tycho-dashboard", "--set-env-vars=URL=https://tycho-strategy-dispatcher-x.run.app")
    ) == []
    assert deploy.names_protected_resource(
        ("gcloud", "run", "services", "update", "--service=tycho-strategy-dispatcher")
    ) == ["tycho-strategy-dispatcher"]


# --- Identity ---------------------------------------------------------------


def test_the_dashboard_identity_is_read_only_on_data():
    assert set(deploy.DASHBOARD_ROLES) == {
        "roles/bigquery.dataViewer",
        "roles/bigquery.jobUser",
        "roles/datastore.viewer",
        "roles/logging.logWriter",
    }
    assert not any(
        role.startswith(deploy.FORBIDDEN_ROLE_PREFIXES) for role in deploy.DASHBOARD_ROLES
    )


def test_the_forbidden_prefixes_cover_writes_gcs_and_pubsub():
    for role in (
        "roles/datastore.user",
        "roles/bigquery.dataEditor",
        "roles/storage.objectViewer",
        "roles/pubsub.publisher",
        "roles/editor",
        "roles/owner",
    ):
        assert role.startswith(deploy.FORBIDDEN_ROLE_PREFIXES)


def test_the_readback_fails_closed_on_a_wider_or_narrower_role_set(monkeypatch):
    state = {"steps": {"service_account": {"email": "tycho-dashboard@p.iam.gserviceaccount.com"}}}
    args = type("Args", (), {"project": "p", "region": "us-central1"})()

    monkeypatch.setattr(deploy, "project_roles_for", lambda *_: list(deploy.DASHBOARD_ROLES))
    assert deploy.readback_roles(args, state)["roles"] == list(deploy.DASHBOARD_ROLES)

    monkeypatch.setattr(
        deploy, "project_roles_for", lambda *_: [*deploy.DASHBOARD_ROLES, "roles/editor"]
    )
    with pytest.raises(Exception, match="unexpected roles"):
        deploy.readback_roles(args, state)

    monkeypatch.setattr(deploy, "project_roles_for", lambda *_: ["roles/bigquery.jobUser"])
    with pytest.raises(Exception, match="missing"):
        deploy.readback_roles(args, state)


def _service_payload(**overrides):
    payload = {
        "status": {
            "url": "https://x.run.app",
            "latestReadyRevisionName": "r",
            "conditions": [{"type": "Ready", "status": "True"}],
        },
        "spec": {
            "template": {
                "metadata": {"annotations": {"autoscaling.knative.dev/maxScale": "1"}},
                "spec": {
                    "serviceAccountName": "tycho-dashboard@p.iam.gserviceaccount.com",
                    "timeoutSeconds": 900,
                    "containers": [{"image": "img", "env": []}],
                },
            }
        },
        "metadata": {"annotations": {}},
        "bindings": [],
    }
    payload.update(overrides)
    return payload


def test_an_absent_min_scale_annotation_reads_back_as_zero(monkeypatch):
    state = {"steps": {"service_account": {"email": "tycho-dashboard@p.iam.gserviceaccount.com"}}}
    args = type("Args", (), {"project": "p", "region": "us-central1"})()
    monkeypatch.setattr(deploy, "gcloud_json", lambda *tokens: _service_payload())
    record = deploy.readback_service(args, state)
    assert record["min_instances"] == "0"
    assert record["max_instances"] == "1"
    assert record["invokers"] == []


def test_a_drifted_scaling_setting_is_a_hard_failure(monkeypatch):
    state = {"steps": {"service_account": {"email": "tycho-dashboard@p.iam.gserviceaccount.com"}}}
    args = type("Args", (), {"project": "p", "region": "us-central1"})()
    payload = _service_payload()
    payload["spec"]["template"]["metadata"]["annotations"] = {
        "autoscaling.knative.dev/minScale": "2",
        "autoscaling.knative.dev/maxScale": "4",
    }
    monkeypatch.setattr(deploy, "gcloud_json", lambda *tokens: payload)
    with pytest.raises(Exception, match="scales 2/4"):
        deploy.readback_service(args, state)


def test_a_public_dashboard_is_a_hard_failure(monkeypatch):
    state = {"steps": {"service_account": {"email": "tycho-dashboard@p.iam.gserviceaccount.com"}}}
    args = type("Args", (), {"project": "p", "region": "us-central1"})()
    monkeypatch.setattr(
        deploy,
        "gcloud_json",
        lambda *tokens: {
            "status": {"url": "https://x.run.app", "latestReadyRevisionName": "r", "conditions": []},
            "spec": {
                "template": {
                    "metadata": {"annotations": {"autoscaling.knative.dev/maxScale": "1"}},
                    "spec": {
                        "serviceAccountName": "tycho-dashboard@p.iam.gserviceaccount.com",
                        "containers": [{"image": "img", "env": []}],
                    },
                }
            },
            "metadata": {"annotations": {}},
            "bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}],
        },
    )
    with pytest.raises(Exception, match="public"):
        deploy.readback_service(args, state)


def test_grant_viewer_refuses_a_public_member():
    args = type(
        "Args", (), {"project": "p", "region": "us-central1", "member": "allUsers"}
    )()
    with pytest.raises(SystemExit):
        deploy.grant_viewer(args)


def test_the_service_is_scale_to_zero_and_private_by_configuration():
    assert deploy.MIN_INSTANCES == "0"
    assert deploy.MAX_INSTANCES == "1"
    source = deploy.apply_service.__code__.co_consts
    assert "--no-allow-unauthenticated" in source
    assert "--allow-unauthenticated" not in source
