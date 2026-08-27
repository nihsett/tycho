"""Deployment boundaries for the Strategy Council production path.

These tests never contact Google Cloud.  They cover the two properties that
make this tooling safe to point at a live project: it cannot modify the analyst
path, and it can be re-run without creating a second anything.
"""

import json
from argparse import Namespace
from pathlib import Path

import pytest

from infra import deploy_strategy_council as deploy
from strategy_agent.app import FORBIDDEN_ROLE_PREFIXES, REQUIRED_ROLES

RUNTIME_NAME = "projects/548847028907/locations/us-central1/reasoningEngines/999"
IDENTITY = (
    "agents.global.proj-548847028907.system.id.goog/resources/aiplatform/"
    "projects/548847028907/locations/us-central1/reasoningEngines/999"
)


def args(tmp_path: Path, **overrides) -> Namespace:
    values = {
        "project": "gen-lang-client-0110801105",
        "location": "us-central1",
        "bucket": "gen-lang-client-0110801105-tycho-agent-staging",
        "state": tmp_path / "strategy_council_production.json",
        "resource_name": None,
        "resume": False,
        "snapshot_key": "untouched_after",
    }
    values.update(overrides)
    return Namespace(**values)


# --- The analyst path is out of reach --------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        (
            "gcloud", "pubsub", "subscriptions", "modify-push-config", "tycho-analyst-push",
            "--push-endpoint=https://evil.example.run.app",
        ),
        ("gcloud", "pubsub", "subscriptions", "delete", "tycho-analyst-push"),
        ("gcloud", "pubsub", "subscriptions", "update", "tycho-analyst-push", "--ack-deadline=10"),
        ("gcloud", "run", "services", "delete", "tycho-analyst-dispatcher"),
        ("gcloud", "run", "services", "update", "tycho-analyst"),
        ("gcloud", "scheduler", "jobs", "pause", "tycho-nightly"),
        ("gcloud", "run", "jobs", "update", "tycho-acquire", "--set-env-vars=TYCHO_DIFFER_MODE=legacy"),
        ("gcloud", "pubsub", "topics", "delete", "tycho-deltas"),
        ("gcloud", "iam", "service-accounts", "delete", "tycho-agent-dispatcher@x.iam.gserviceaccount.com"),
    ],
)
def test_deploy_tooling_cannot_modify_the_analyst_production_path(command):
    with pytest.raises(deploy.ProtectedResourceError, match="may not modify"):
        deploy.guard_command(command)


@pytest.mark.parametrize(
    "command",
    [
        ("gcloud", "pubsub", "subscriptions", "describe", "tycho-analyst-push"),
        ("gcloud", "scheduler", "jobs", "describe", "tycho-nightly"),
        ("gcloud", "run", "jobs", "describe", "tycho-acquire"),
        ("gcloud", "run", "services", "list", "--region=us-central1"),
    ],
)
def test_the_analyst_path_may_still_be_read_for_evidence(command):
    deploy.guard_command(command)


@pytest.mark.parametrize(
    "command",
    [
        ("gcloud", "run", "deploy", "tycho-strategy-dispatcher", "--source=."),
        ("gcloud", "scheduler", "jobs", "create", "http", "tycho-strategy-weekly"),
        ("gcloud", "iam", "service-accounts", "create", "tycho-strategy-dispatcher"),
    ],
)
def test_the_strategy_resources_are_not_caught_by_the_guard(command):
    deploy.guard_command(command)


def test_every_shell_out_goes_through_the_guard(monkeypatch):
    """`run` is the only path to a subprocess, and it always guards first."""
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **k: pytest.fail("unguarded"))
    with pytest.raises(deploy.ProtectedResourceError):
        deploy.run("gcloud", "pubsub", "subscriptions", "delete", "tycho-analyst-push")
    with pytest.raises(deploy.ProtectedResourceError):
        deploy.gcloud_exists("run", "services", "delete", "tycho-analyst-dispatcher")


def test_the_analyst_subscription_is_never_a_deployment_target():
    """No step's command template mentions the live analyst subscription."""
    source = Path("infra/deploy_strategy_council.py").read_text()
    assert "modify-push-config" not in source
    for name, apply_step, _ in deploy.STEPS:
        body = apply_step.__code__.co_consts
        rendered = " ".join(str(const) for const in body)
        assert "tycho-analyst-push" not in rendered, name


def test_pointing_the_tooling_at_the_analyst_runtime_is_refused(monkeypatch, tmp_path):
    class FakeResource:
        name = "projects/548847028907/locations/us-central1/reasoningEngines/8577815225082839040"
        display_name = deploy.ANALYST_RUNTIME_DISPLAY_NAME
        spec = None

    monkeypatch.setattr(
        deploy,
        "client",
        lambda *a, **k: Namespace(
            agent_engines=Namespace(get=lambda name: Namespace(api_resource=FakeResource()))
        ),
    )
    state = {"steps": {"runtime": {"resource_name": FakeResource.name}}}
    with pytest.raises(deploy.ProtectedResourceError, match="analyst Runtime"):
        deploy.readback_runtime(args(tmp_path), state)


# --- The Runtime identity is exactly the narrow set ------------------------


def test_the_runtime_requests_exactly_the_allowlisted_roles():
    assert set(REQUIRED_ROLES) == {
        "roles/datastore.user",
        "roles/bigquery.dataViewer",
        "roles/bigquery.jobUser",
        "roles/telemetry.tracesWriter",
    }
    for role in REQUIRED_ROLES:
        assert not any(role.startswith(prefix) for prefix in FORBIDDEN_ROLE_PREFIXES)


@pytest.mark.parametrize(
    "role",
    [
        "roles/storage.objectAdmin",
        "roles/storage.objectViewer",
        "roles/pubsub.publisher",
        "roles/pubsub.subscriber",
        "roles/aiplatform.memoryBankUser",
        "roles/cloudtrace.agent",
    ],
)
def test_forbidden_roles_are_named_and_excluded(role):
    assert role not in REQUIRED_ROLES
    assert any(role.startswith(prefix) for prefix in FORBIDDEN_ROLE_PREFIXES)


def test_editor_and_owner_are_never_requested():
    for role in REQUIRED_ROLES:
        assert role not in {"roles/editor", "roles/owner", "roles/aiplatform.admin"}


def test_the_iam_readback_fails_closed_on_a_wider_grant(monkeypatch, tmp_path):
    state = {"steps": {"runtime": {"effective_identity": IDENTITY}}}
    granted = [*REQUIRED_ROLES, "roles/storage.objectAdmin"]
    monkeypatch.setattr(deploy, "project_roles_for", lambda project, member: sorted(granted))

    with pytest.raises(deploy.AmbiguousStateError, match="expected exactly"):
        deploy.readback_runtime_roles(args(tmp_path), state)


def test_the_iam_readback_fails_closed_on_a_missing_grant(monkeypatch, tmp_path):
    state = {"steps": {"runtime": {"effective_identity": IDENTITY}}}
    monkeypatch.setattr(deploy, "project_roles_for", lambda project, member: ["roles/datastore.user"])

    with pytest.raises(deploy.AmbiguousStateError, match="expected exactly"):
        deploy.readback_runtime_roles(args(tmp_path), state)


def test_the_iam_readback_accepts_exactly_the_allowlist(monkeypatch, tmp_path):
    state = {"steps": {"runtime": {"effective_identity": IDENTITY}}}
    monkeypatch.setattr(deploy, "project_roles_for", lambda project, member: sorted(REQUIRED_ROLES))

    record = deploy.readback_runtime_roles(args(tmp_path), state)

    assert record["member"] == f"principal://{IDENTITY}"
    assert set(record["roles"]) == set(REQUIRED_ROLES)
    assert record["forbidden_roles"] == []


def test_the_dispatcher_identity_gets_only_invoke_and_log_roles():
    assert set(deploy.DISPATCHER_ROLES) == {
        "roles/aiplatform.viewer",
        "roles/logging.logWriter",
    }
    for role in deploy.DISPATCHER_ROLES:
        assert not any(role.startswith(prefix) for prefix in ("roles/storage", "roles/pubsub"))
        assert role not in {"roles/editor", "roles/owner", "roles/datastore.user"}


# --- Runtime deployment shape ----------------------------------------------


def test_the_runtime_config_is_a_scale_to_zero_agent_identity(tmp_path):
    config = deploy.runtime_deployment_config(args(tmp_path))

    assert config["display_name"] == "Tycho Strategy Council"
    assert config["identity_type"] == "AGENT_IDENTITY"
    assert (config["min_instances"], config["max_instances"]) == (0, 1)
    assert config["env_vars"]["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] == "false"
    # No bucket variable: the council never reads a raw snapshot.
    assert "TYCHO_BUCKET" not in config["env_vars"]
    assert "TYCHO_TOPIC" not in config["env_vars"]


def test_the_scheduler_contract_is_a_monday_utc_trigger_with_no_prompt():
    from pipeline.strategy_dispatcher import parse_trigger

    assert deploy.SCHEDULER_CRON == "0 6 * * 1"
    assert deploy.SCHEDULER_TIMEZONE == "Etc/UTC"
    body = json.loads(deploy.scheduler_body())
    assert body == {"trigger": "scheduler", "period": "previous_complete_week"}
    # The static body the job posts every Monday is a valid bounded trigger.
    assert parse_trigger(body).trigger == "scheduler"


def test_a_public_dispatcher_is_a_hard_failure(monkeypatch, tmp_path):
    service = {
        "status": {"url": "https://tycho-strategy-dispatcher-uc.a.run.app"},
        "spec": {"template": {"spec": {"serviceAccountName": "sa@p.iam.gserviceaccount.com"}}},
        "metadata": {"annotations": {}},
    }
    policy = {"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]}
    responses = iter([service, policy])
    monkeypatch.setattr(deploy, "gcloud_json", lambda *a, **k: next(responses))
    state = {"steps": {"dispatcher_service_account": {"email": "sa@p.iam.gserviceaccount.com"}}}

    with pytest.raises(deploy.AmbiguousStateError, match="public"):
        deploy.readback_dispatcher(args(tmp_path), state)


def test_a_scheduler_job_that_drifted_is_a_hard_failure(monkeypatch, tmp_path):
    url = "https://tycho-strategy-dispatcher-uc.a.run.app"
    job = {
        "name": "projects/p/locations/us-central1/jobs/tycho-strategy-weekly",
        "schedule": "0 6 * * *",
        "timeZone": "Etc/UTC",
        "state": "ENABLED",
        "httpTarget": {
            "uri": url,
            "httpMethod": "POST",
            "oidcToken": {"serviceAccountEmail": "sa@p.iam.gserviceaccount.com", "audience": url},
        },
    }
    monkeypatch.setattr(deploy, "gcloud_json", lambda *a, **k: job)
    state = {
        "steps": {
            "dispatcher": {"url": url},
            "dispatcher_service_account": {"email": "sa@p.iam.gserviceaccount.com"},
        }
    }

    with pytest.raises(deploy.AmbiguousStateError, match="does not match its contract"):
        deploy.readback_scheduler(args(tmp_path), state)


# --- Resume and idempotency -------------------------------------------------


class FakeSteps:
    """A step registry that records how often each half actually ran."""

    def __init__(self, fail_at: str | None = None):
        self.applied: list[str] = []
        self.read: list[str] = []
        self.fail_at = fail_at

    def registry(self, names):
        return tuple((name, self._apply(name), self._readback(name)) for name in names)

    def _apply(self, name):
        def apply_step(args, state):
            if name == self.fail_at:
                raise RuntimeError("transient cloud failure")
            self.applied.append(name)
            return {"resource": f"{name}-1"}

        return apply_step

    def _readback(self, name):
        def readback_step(args, state):
            self.read.append(name)
            recorded = deploy.step_record(state, name).get("resource")
            if recorded != f"{name}-1":
                raise deploy.AmbiguousStateError(f"{name} drifted")
            return {"resource": recorded, "verified": True}

        return readback_step


@pytest.fixture
def offline_deploy(monkeypatch):
    monkeypatch.setattr(deploy, "full_snapshot", lambda args: {"captured": True})
    return monkeypatch


def test_deployment_is_idempotent_and_creates_nothing_twice(offline_deploy, tmp_path):
    steps = FakeSteps()
    offline_deploy.setattr(deploy, "STEPS", steps.registry(["runtime", "dispatcher", "scheduler"]))

    deploy.deploy(args(tmp_path))
    assert steps.applied == ["runtime", "dispatcher", "scheduler"]

    deploy.deploy(args(tmp_path, resume=True))
    # Second run applied nothing and read every resource back again.
    assert steps.applied == ["runtime", "dispatcher", "scheduler"]
    assert steps.read == ["runtime", "dispatcher", "scheduler"] * 2


def test_a_failed_step_leaves_the_earlier_resources_recorded_and_resumable(
    offline_deploy, tmp_path
):
    crashed = FakeSteps(fail_at="dispatcher")
    offline_deploy.setattr(deploy, "STEPS", crashed.registry(["runtime", "dispatcher", "scheduler"]))

    with pytest.raises(RuntimeError, match="transient cloud failure"):
        deploy.deploy(args(tmp_path))

    state = deploy.load_state(args(tmp_path).state)
    assert state["steps"]["runtime"]["done"] is True
    assert "dispatcher" not in state["steps"]
    assert crashed.applied == ["runtime"]

    resumed = FakeSteps()
    offline_deploy.setattr(deploy, "STEPS", resumed.registry(["runtime", "dispatcher", "scheduler"]))
    deploy.deploy(args(tmp_path, resume=True))

    # The Runtime was NOT created a second time; only the missing steps ran.
    assert resumed.applied == ["dispatcher", "scheduler"]
    assert resumed.read[0] == "runtime"


def test_a_second_deploy_without_resume_refuses_rather_than_redeploying(
    offline_deploy, tmp_path
):
    steps = FakeSteps()
    offline_deploy.setattr(deploy, "STEPS", steps.registry(["runtime"]))
    deploy.deploy(args(tmp_path))

    with pytest.raises(SystemExit, match="already exists"):
        deploy.deploy(args(tmp_path))


def test_the_before_snapshot_is_captured_once_and_never_overwritten(
    offline_deploy, tmp_path
):
    steps = FakeSteps()
    offline_deploy.setattr(deploy, "STEPS", steps.registry(["runtime"]))
    offline_deploy.setattr(deploy, "full_snapshot", lambda args: {"call": len(steps.read)})

    deploy.deploy(args(tmp_path))
    before = deploy.load_state(args(tmp_path).state)["untouched_before"]
    deploy.deploy(args(tmp_path, resume=True))

    assert deploy.load_state(args(tmp_path).state)["untouched_before"] == before


def test_a_state_file_for_another_project_is_a_hard_failure(offline_deploy, tmp_path):
    steps = FakeSteps()
    offline_deploy.setattr(deploy, "STEPS", steps.registry(["runtime"]))
    deploy.deploy(args(tmp_path))

    with pytest.raises(deploy.AmbiguousStateError, match="recorded deployment targets"):
        deploy.deploy(args(tmp_path, resume=True, project="some-other-project"))


def test_an_orphaned_runtime_blocks_a_second_create(monkeypatch, tmp_path):
    class Orphan:
        api_resource = Namespace(name=RUNTIME_NAME, display_name=deploy.DISPLAY_NAME)

    monkeypatch.setattr(deploy, "existing_runtimes", lambda args: [Orphan()])

    with pytest.raises(deploy.AmbiguousStateError, match="already exists"):
        deploy.apply_runtime(args(tmp_path), {"steps": {}})


def test_readback_refuses_a_deployment_that_never_finished(tmp_path):
    state_path = args(tmp_path).state
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"project": "p", "location": "l", "steps": {}}))

    with pytest.raises(deploy.AmbiguousStateError, match="never completed"):
        deploy.readback(args(tmp_path))


# --- Recorded state is structural only --------------------------------------


def test_the_persisted_state_carries_no_prose(offline_deploy, tmp_path):
    steps = FakeSteps()
    offline_deploy.setattr(deploy, "STEPS", steps.registry(["runtime", "dispatcher"]))
    deploy.deploy(args(tmp_path))

    serialized = args(tmp_path).state.read_text().lower()
    for prose in (
        "prompt",
        "response",
        "system instruction",
        "statement",
        "rationale",
        "quote",
        "rendered_md",
        "brief_prose",
        "model_output",
    ):
        assert prose not in serialized, prose


# --- Readback comparisons -----------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://svc-uc.a.run.app", "https://svc-uc.a.run.app/"),
        ("https://svc-uc.a.run.app/", "https://svc-uc.a.run.app"),
        ("https://svc-uc.a.run.app", "https://svc-uc.a.run.app"),
    ],
)
def test_the_scheduler_uri_matches_across_the_trailing_slash_the_api_adds(left, right):
    assert deploy.same_endpoint(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://svc-uc.a.run.app", "https://other-uc.a.run.app"),
        ("https://svc-uc.a.run.app", None),
        (None, "https://svc-uc.a.run.app"),
    ],
)
def test_a_genuinely_different_endpoint_is_still_drift(left, right):
    assert not deploy.same_endpoint(left, right)


def test_a_scheduler_job_whose_endpoint_moved_is_still_a_hard_failure(monkeypatch, tmp_path):
    url = "https://tycho-strategy-dispatcher-uc.a.run.app"
    job = {
        "name": "projects/p/locations/us-central1/jobs/tycho-strategy-weekly",
        "schedule": deploy.SCHEDULER_CRON,
        "timeZone": deploy.SCHEDULER_TIMEZONE,
        "state": "ENABLED",
        "httpTarget": {
            "uri": "https://tycho-analyst-dispatcher-uc.a.run.app/",
            "httpMethod": "POST",
            "oidcToken": {"serviceAccountEmail": "sa@p.iam.gserviceaccount.com", "audience": url},
        },
    }
    monkeypatch.setattr(deploy, "gcloud_json", lambda *a, **k: job)
    state = {
        "steps": {
            "dispatcher": {"url": url},
            "dispatcher_service_account": {"email": "sa@p.iam.gserviceaccount.com"},
        }
    }

    with pytest.raises(deploy.AmbiguousStateError, match="does not match its contract"):
        deploy.readback_scheduler(args(tmp_path), state)


# --- Updating the package cannot wander onto another Runtime -----------------


def test_update_refuses_when_no_runtime_is_recorded(tmp_path):
    state_path = args(tmp_path).state
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"project": "gen-lang-client-0110801105", "location": "us-central1", "steps": {}})
    )

    with pytest.raises(deploy.AmbiguousStateError, match="no Strategy Council Runtime"):
        deploy.update(args(tmp_path))


def test_update_reads_the_runtime_back_before_touching_it(monkeypatch, tmp_path):
    """readback_runtime runs first, so the analyst Runtime is unreachable here."""
    calls = []

    def refuse(args_, state):
        calls.append("readback")
        raise deploy.ProtectedResourceError("that is the analyst Runtime")

    monkeypatch.setattr(deploy, "readback_runtime", refuse)
    monkeypatch.setattr(
        deploy,
        "client",
        lambda *a, **k: pytest.fail("update must not reach the API before readback"),
    )

    with pytest.raises(deploy.ProtectedResourceError):
        deploy.update_runtime(args(tmp_path), {"steps": {"runtime": {"resource_name": RUNTIME_NAME}}})
    assert calls == ["readback"]
