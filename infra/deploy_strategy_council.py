"""Deploy the Tycho Strategy Council production path, and nothing else.

    uv run python -m infra.deploy_strategy_council plan
    uv run python -m infra.deploy_strategy_council deploy --resume
    uv run python -m infra.deploy_strategy_council readback
    uv run python -m infra.deploy_strategy_council snapshot

This module creates one Agent Runtime, its managed-identity grants, one private
Cloud Run dispatcher with its own service account, and one weekly Scheduler job.

It is deliberately incapable of touching the analyst production path.  Every
shell-out goes through :func:`run`, which refuses any non-read-only command that
mentions ``tycho-analyst-push``, the analyst dispatcher, the analyst Runtime, the
acquisition job, the nightly Scheduler, or the Delta topic.  The analyst
resources are read, recorded as before/after evidence, and never written.

Deployment is resumable and idempotent:

- every durable resource is persisted to the state file the moment it exists, so
  a transient failure never creates a second Runtime on the next attempt;
- every step reads its resource back from the API rather than trusting the state
  file, and a resource that exists but does not match the recorded identity is a
  hard failure rather than something to overwrite;
- nothing is ever deleted or replaced.

Nothing here records a prompt, a response, a claim statement, an evidence quote,
brief prose, or source content: the state file holds resource names, identities,
roles, IDs, and counts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.strategy_dispatcher import (
    DEFAULT_PERIOD_SELECTOR,
    previous_complete_week,
)
from strategy_agent.app import (
    FORBIDDEN_ROLE_PREFIXES,
    REQUIRED_ROLES,
    STRATEGY_APP_NAME,
)

DEFAULT_PROJECT = "gen-lang-client-0110801105"
DEFAULT_LOCATION = "us-central1"
DEFAULT_BUCKET = f"{DEFAULT_PROJECT}-tycho-agent-staging"
DEFAULT_STATE_PATH = Path("data/strategy_council_production.json")

DISPLAY_NAME = "Tycho Strategy Council"
RUNTIME_VERSION = "tycho-strategy-council@1"
RUNTIME_LABEL_VERSION = "tycho-strategy-council-1"
DISPATCHER_SERVICE = "tycho-strategy-dispatcher"
DISPATCHER_SERVICE_ACCOUNT = "tycho-strategy-dispatcher"
SCHEDULER_JOB = "tycho-strategy-weekly"
SCHEDULER_CRON = "0 6 * * 1"
SCHEDULER_TIMEZONE = "Etc/UTC"
SCHEDULER_ATTEMPT_DEADLINE = "1800s"
DISPATCHER_TIMEOUT_SECONDS = 840
CLOUD_RUN_TIMEOUT = "900"
STRATEGY_MODEL = "gemini-3.7-flash"

#: The dispatcher identity gets exactly what it needs: invoke the Strategy
#: Runtime, write structural logs, and be invoked by its own Scheduler job
#: (that last one is a binding on the dispatcher service, not a project role).
DISPATCHER_ROLES = (
    "roles/aiplatform.viewer",
    "roles/logging.logWriter",
)

#: Production resources this module must never modify.  A command naming one of
#: these is allowed only if it is unambiguously read-only.
PROTECTED_RESOURCES = frozenset(
    {
        "tycho-analyst-push",
        "tycho-analyst-dispatcher",
        "tycho-analyst-runtime-shadow",
        "tycho-analyst",
        "tycho-nightly",
        "tycho-acquire",
        "tycho-deltas",
        "tycho-agent-dispatcher",
        "tycho-runtime",
    }
)
READ_ONLY_VERBS = frozenset({"describe", "list", "get-iam-policy", "read", "tail"})

#: The analyst Runtime.  Recorded so an operator cannot point strategy tooling
#: at it by passing --resource-name.
ANALYST_RUNTIME_DISPLAY_NAME = "Tycho Analyst"

DEPLOYMENT_REQUIREMENTS = [
    "google-cloud-aiplatform[agent_engines,adk]==1.165.1",
    "google-adk==2.7.1",
    "google-genai==2.19.0",
    "cloudpickle==3.1.2",
    "google-cloud-bigquery==3.43.0",
    "google-cloud-firestore==2.28.1",
    "google-cloud-pubsub==2.39.1",
    "google-cloud-storage==3.13.1",
    "pydantic==2.13.4",
    "python-dotenv==1.2.3",
    "pyyaml==6.0.3",
]


class ProtectedResourceError(RuntimeError):
    """A command would have modified a resource this module must not touch."""


class AmbiguousStateError(RuntimeError):
    """The cloud state does not match the recorded state; fail closed."""


# --- Command boundary -------------------------------------------------------


def guard_command(args: tuple[str, ...]) -> None:
    """Refuse any non-read-only command that names an analyst-path resource."""
    tokens = tuple(str(arg) for arg in args)
    mentioned = sorted(
        {name for name in PROTECTED_RESOURCES for token in tokens if name in token}
    )
    if not mentioned:
        return
    if set(tokens) & READ_ONLY_VERBS:
        return
    raise ProtectedResourceError(
        "strategy deployment may not modify existing production resources: "
        f"{mentioned}"
    )


def run(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    guard_command(args)
    print("+", " ".join(args))
    return subprocess.run(args, check=True, text=True, capture_output=capture)


def gcloud_exists(*args: str) -> bool:
    guard_command(("gcloud", *args))
    return (
        subprocess.run(
            ("gcloud", *args),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def gcloud_json(*args: str) -> Any:
    result = run("gcloud", *args, "--format=json", capture=True)
    return json.loads(result.stdout or "null")


def client(project: str, location: str) -> Any:
    import agentplatform
    from google.genai import types

    return agentplatform.Client(
        project=project,
        location=location,
        http_options=types.HttpOptions(api_version="v1beta1"),
    )


# --- State ------------------------------------------------------------------


def new_state(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "state_version": "strategy-council-deployment@1",
        "project": args.project,
        "location": args.location,
        "display_name": DISPLAY_NAME,
        "version": RUNTIME_VERSION,
        "app_name": STRATEGY_APP_NAME,
        "staging_bucket": args.bucket,
        "steps": {},
        "created_at": datetime.now(UTC).isoformat(),
    }


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def write_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(UTC).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def step_record(state: dict[str, Any], name: str) -> dict[str, Any]:
    return state.setdefault("steps", {}).get(name, {})


def completed(state: dict[str, Any], name: str) -> bool:
    return bool(step_record(state, name).get("done"))


# --- Resource steps ---------------------------------------------------------


def apply_staging_bucket(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    from google.cloud import storage

    storage_client = storage.Client(project=args.project)
    bucket = storage_client.bucket(args.bucket)
    if not bucket.exists():
        bucket.storage_class = "STANDARD"
        bucket.labels = {
            "app": "tycho",
            "purpose": "agent-runtime-staging",
            "environment": "production",
        }
        storage_client.create_bucket(bucket, location=args.location)
    return {"bucket": args.bucket}


def readback_staging_bucket(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    from google.cloud import storage

    storage_client = storage.Client(project=args.project)
    if not storage_client.bucket(args.bucket).exists():
        raise AmbiguousStateError(f"staging bucket {args.bucket} is not readable")
    return {"bucket": args.bucket}


def runtime_deployment_config(args: argparse.Namespace) -> dict[str, Any]:
    """One config, so deploy and resume can never diverge."""
    return {
        "display_name": DISPLAY_NAME,
        "description": (
            "Tycho's bounded strategy council: strategist, challenger, and brief "
            "writer driven by a deterministic wrapper behind Python evidence gates."
        ),
        "labels": {
            "app": "tycho",
            "environment": "production",
            "version": RUNTIME_LABEL_VERSION,
        },
        "requirements": DEPLOYMENT_REQUIREMENTS,
        "extra_packages": [
            "strategy_agent",
            "runtime_agent",
            "pipeline",
            "schemas",
            "tycho.yaml",
        ],
        "staging_bucket": f"gs://{args.bucket}",
        "gcs_dir_name": "tycho-strategy-council-v1",
        "identity_type": "AGENT_IDENTITY",
        "agent_framework": "google-adk",
        "min_instances": 0,
        "max_instances": 1,
        "env_vars": {
            "TYCHO_PROJECT": args.project,
            "TYCHO_DATASET": "tycho",
            "TYCHO_CONFIG": "tycho.yaml",
            "TYCHO_STRATEGY_MODEL": STRATEGY_MODEL,
            "TYCHO_RUNTIME_LOCATION": args.location,
            "GOOGLE_CLOUD_LOCATION": "global",
            # Agent Identity tokens are certificate-bound, so Firestore must use
            # the mTLS endpoint exactly as the analyst Runtime does.
            "GOOGLE_API_USE_CLIENT_CERTIFICATE": "true",
            "GOOGLE_API_USE_MTLS_ENDPOINT": "always",
            "GOOGLE_GENAI_USE_ENTERPRISE": "true",
            "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
            "OTEL_SERVICE_NAME": "tycho-strategy-runtime",
            "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
            "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false",
        },
    }


def resource_record(remote: Any) -> dict[str, Any]:
    resource = remote.api_resource
    spec = getattr(resource, "spec", None)
    deployment = getattr(spec, "deployment_spec", None)
    return {
        "resource_name": resource.name,
        "display_name": getattr(resource, "display_name", DISPLAY_NAME),
        "effective_identity": getattr(spec, "effective_identity", None),
        "identity_type": str(getattr(spec, "identity_type", "") or ""),
        "agent_framework": str(getattr(spec, "agent_framework", "") or ""),
        "min_instances": getattr(deployment, "min_instances", None),
        "max_instances": getattr(deployment, "max_instances", None),
        "version": RUNTIME_VERSION,
    }


def existing_runtimes(args: argparse.Namespace) -> list[Any]:
    return list(client(args.project, args.location).agent_engines.list())


def apply_runtime(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    from strategy_agent.app import build_strategy_app

    recorded = step_record(state, "runtime").get("resource_name")
    if recorded:
        remote = client(args.project, args.location).agent_engines.get(name=recorded)
        return {**resource_record(remote), "created": False}

    # Fail closed rather than create a second council Runtime: a previous
    # attempt may have created one and died before its state was written.
    orphans = [
        engine
        for engine in existing_runtimes(args)
        if getattr(engine.api_resource, "display_name", None) == DISPLAY_NAME
    ]
    if orphans:
        raise AmbiguousStateError(
            f"a Runtime named {DISPLAY_NAME!r} already exists "
            f"({[engine.api_resource.name for engine in orphans]}); "
            "resume with --resource-name instead of creating another"
        )
    remote = client(args.project, args.location).agent_engines.create(
        agent=build_strategy_app(STRATEGY_MODEL),
        config=runtime_deployment_config(args),
    )
    return {**resource_record(remote), "created": True}


def readback_runtime(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    name = step_record(state, "runtime").get("resource_name")
    if not name:
        raise AmbiguousStateError("no Strategy Council Runtime is recorded")
    remote = client(args.project, args.location).agent_engines.get(name=name)
    record = resource_record(remote)
    if record["display_name"] == ANALYST_RUNTIME_DISPLAY_NAME:
        raise ProtectedResourceError(
            f"{name} is the analyst Runtime; strategy tooling may not target it"
        )
    if record["display_name"] != DISPLAY_NAME:
        raise AmbiguousStateError(
            f"{name} is {record['display_name']!r}, not {DISPLAY_NAME!r}"
        )
    if (record["min_instances"], record["max_instances"]) != (0, 1):
        raise AmbiguousStateError(
            f"{name} scales {record['min_instances']}/{record['max_instances']}, expected 0/1"
        )
    if not record["effective_identity"]:
        raise AmbiguousStateError(f"{name} has no managed Agent Identity")
    return record


def update_runtime(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    """Push a new package to the recorded Runtime; never create another one.

    ``readback_runtime`` runs first, so this refuses to touch anything that is
    not the Strategy Council Runtime this state file already owns.
    """
    from strategy_agent.app import build_strategy_app

    verified = readback_runtime(args, state)
    remote = client(args.project, args.location).agent_engines.update(
        name=verified["resource_name"],
        agent=build_strategy_app(STRATEGY_MODEL),
        config=runtime_deployment_config(args),
    )
    return {**resource_record(remote), "created": False}


def identity_member(effective_identity: str) -> str:
    if not effective_identity:
        raise AmbiguousStateError("Agent Runtime returned no managed identity")
    return (
        effective_identity
        if effective_identity.startswith("principal:")
        else f"principal://{effective_identity}"
    )


def project_binding(project: str, member: str, role: str) -> None:
    run(
        "gcloud",
        "projects",
        "add-iam-policy-binding",
        project,
        "--member",
        member,
        "--role",
        role,
        "--condition=None",
        "--quiet",
    )


def project_roles_for(project: str, member: str) -> list[str]:
    policy = gcloud_json("projects", "get-iam-policy", project)
    return sorted(
        binding["role"]
        for binding in policy.get("bindings", [])
        if member in binding.get("members", [])
    )


def apply_runtime_roles(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    member = identity_member(step_record(state, "runtime")["effective_identity"])
    for role in REQUIRED_ROLES:
        project_binding(args.project, member, role)
    return {"member": member, "roles": list(REQUIRED_ROLES)}


def readback_runtime_roles(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    member = identity_member(step_record(state, "runtime")["effective_identity"])
    granted = project_roles_for(args.project, member)
    if set(granted) != set(REQUIRED_ROLES):
        raise AmbiguousStateError(
            f"Runtime identity holds {granted}, expected exactly {sorted(REQUIRED_ROLES)}"
        )
    forbidden = [
        role
        for role in granted
        if any(role.startswith(prefix) for prefix in FORBIDDEN_ROLE_PREFIXES)
    ]
    if forbidden:
        raise AmbiguousStateError(f"Runtime identity holds forbidden roles: {forbidden}")
    return {"member": member, "roles": granted, "forbidden_roles": []}


def registry_entry(args: argparse.Namespace, resource_name: str) -> dict[str, Any] | None:
    entries = gcloud_json(
        "agent-registry",
        "agents",
        "list",
        f"--project={args.project}",
        f"--location={args.location}",
    )
    runtime_uri = f"//aiplatform.googleapis.com/{resource_name}"
    for entry in entries or []:
        attributes = entry.get("attributes") or {}
        reference = attributes.get(
            "agentregistry.googleapis.com/system/RuntimeReference", {}
        ).get("uri")
        if reference == runtime_uri:
            return entry
    return None


def apply_registry(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    resource_name = step_record(state, "runtime")["resource_name"]
    for attempt in range(12):
        entry = registry_entry(args, resource_name)
        if entry is not None:
            return {"entry": entry, "name": entry.get("name")}
        if attempt < 11:
            time.sleep(5)
    raise AmbiguousStateError(
        "the Strategy Council Runtime deployed but never appeared in Agent Registry"
    )


def readback_registry(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    return apply_registry(args, state)


def apply_dispatcher_service_account(
    args: argparse.Namespace, state: dict[str, Any]
) -> dict[str, Any]:
    email = f"{DISPATCHER_SERVICE_ACCOUNT}@{args.project}.iam.gserviceaccount.com"
    if not gcloud_exists(
        "iam", "service-accounts", "describe", email, "--project", args.project
    ):
        run(
            "gcloud",
            "iam",
            "service-accounts",
            "create",
            DISPATCHER_SERVICE_ACCOUNT,
            "--display-name",
            "Tycho Strategy Council dispatcher",
            "--project",
            args.project,
            "--quiet",
        )
    for attempt in range(10):
        if gcloud_exists(
            "iam", "service-accounts", "describe", email, "--project", args.project
        ):
            return {"email": email}
        if attempt < 9:
            time.sleep(3)
    raise AmbiguousStateError(f"service account never became visible: {email}")


def readback_dispatcher_service_account(
    args: argparse.Namespace, state: dict[str, Any]
) -> dict[str, Any]:
    email = f"{DISPATCHER_SERVICE_ACCOUNT}@{args.project}.iam.gserviceaccount.com"
    account = gcloud_json(
        "iam", "service-accounts", "describe", email, "--project", args.project
    )
    if account.get("email") != email:
        raise AmbiguousStateError(f"dispatcher service account is not readable: {email}")
    return {"email": email, "disabled": bool(account.get("disabled"))}


def apply_dispatcher_iam(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    email = step_record(state, "dispatcher_service_account")["email"]
    member = f"serviceAccount:{email}"
    for role in DISPATCHER_ROLES:
        project_binding(args.project, member, role)
    return {"member": member, "roles": list(DISPATCHER_ROLES)}


def readback_dispatcher_iam(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    email = step_record(state, "dispatcher_service_account")["email"]
    member = f"serviceAccount:{email}"
    granted = project_roles_for(args.project, member)
    missing = sorted(set(DISPATCHER_ROLES) - set(granted))
    if missing:
        raise AmbiguousStateError(f"dispatcher identity is missing {missing}")
    extra = sorted(set(granted) - set(DISPATCHER_ROLES))
    if extra:
        raise AmbiguousStateError(f"dispatcher identity holds unexpected roles: {extra}")
    return {"member": member, "roles": granted}


def apply_dispatcher(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    email = step_record(state, "dispatcher_service_account")["email"]
    resource_name = step_record(state, "runtime")["resource_name"]
    env = ",".join(
        [
            f"TYCHO_PROJECT={args.project}",
            f"TYCHO_RUNTIME_LOCATION={args.location}",
            f"TYCHO_STRATEGY_RUNTIME_RESOURCE={resource_name}",
            f"TYCHO_DISPATCHER_TIMEOUT_SECONDS={DISPATCHER_TIMEOUT_SECONDS}",
        ]
    )
    run(
        "gcloud",
        "run",
        "deploy",
        DISPATCHER_SERVICE,
        "--source=.",
        f"--region={args.location}",
        f"--service-account={email}",
        f"--set-env-vars={env}",
        "--set-build-env-vars=GOOGLE_ENTRYPOINT=python -m pipeline.strategy_dispatcher",
        "--command=",
        "--args=",
        f"--timeout={CLOUD_RUN_TIMEOUT}",
        "--no-allow-unauthenticated",
        "--ingress=all",
        "--min-instances=0",
        "--max-instances=1",
        "--project",
        args.project,
        "--quiet",
    )
    return readback_dispatcher(args, state)


def readback_dispatcher(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    service = gcloud_json(
        "run",
        "services",
        "describe",
        DISPATCHER_SERVICE,
        f"--region={args.location}",
        "--project",
        args.project,
    )
    url = (service.get("status") or {}).get("url")
    account = (
        ((service.get("spec") or {}).get("template") or {}).get("spec") or {}
    ).get("serviceAccountName")
    expected = step_record(state, "dispatcher_service_account").get("email")
    if not url:
        raise AmbiguousStateError("the strategy dispatcher has no Cloud Run URL")
    if expected and account != expected:
        raise AmbiguousStateError(
            f"strategy dispatcher runs as {account}, expected {expected}"
        )
    policy = gcloud_json(
        "run",
        "services",
        "get-iam-policy",
        DISPATCHER_SERVICE,
        f"--region={args.location}",
        "--project",
        args.project,
    )
    invokers = sorted(
        {
            member
            for binding in (policy.get("bindings") or [])
            if binding.get("role") == "roles/run.invoker"
            for member in binding.get("members", [])
        }
    )
    public = sorted({member for member in invokers if member in {"allUsers", "allAuthenticatedUsers"}})
    if public:
        raise AmbiguousStateError(f"the strategy dispatcher is public: {public}")
    return {
        "service": DISPATCHER_SERVICE,
        "url": url,
        "service_account": account,
        "invokers": invokers,
        "ingress": (service.get("metadata") or {})
        .get("annotations", {})
        .get("run.googleapis.com/ingress"),
    }


def apply_dispatcher_invoker(
    args: argparse.Namespace, state: dict[str, Any]
) -> dict[str, Any]:
    email = step_record(state, "dispatcher_service_account")["email"]
    run(
        "gcloud",
        "run",
        "services",
        "add-iam-policy-binding",
        DISPATCHER_SERVICE,
        f"--region={args.location}",
        f"--member=serviceAccount:{email}",
        "--role=roles/run.invoker",
        "--project",
        args.project,
        "--quiet",
    )
    return readback_dispatcher_invoker(args, state)


def readback_dispatcher_invoker(
    args: argparse.Namespace, state: dict[str, Any]
) -> dict[str, Any]:
    email = step_record(state, "dispatcher_service_account")["email"]
    record = readback_dispatcher(args, state)
    member = f"serviceAccount:{email}"
    if member not in record["invokers"]:
        raise AmbiguousStateError(f"{member} cannot invoke the strategy dispatcher")
    return {"member": member, "invokers": record["invokers"]}


def scheduler_body() -> str:
    """The static trigger body.  The dispatcher resolves the week, not the job."""
    return json.dumps(
        {"trigger": "scheduler", "period": DEFAULT_PERIOD_SELECTOR},
        sort_keys=True,
        separators=(",", ":"),
    )


def apply_scheduler(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    email = step_record(state, "dispatcher_service_account")["email"]
    url = step_record(state, "dispatcher")["url"]
    exists = gcloud_exists(
        "scheduler",
        "jobs",
        "describe",
        SCHEDULER_JOB,
        f"--location={args.location}",
        "--project",
        args.project,
    )
    verb = "update" if exists else "create"
    # `create` takes --headers; `update` takes --update-headers for the same map.
    headers_flag = "--update-headers" if exists else "--headers"
    run(
        "gcloud",
        "scheduler",
        "jobs",
        verb,
        "http",
        SCHEDULER_JOB,
        f"--location={args.location}",
        f"--schedule={SCHEDULER_CRON}",
        f"--time-zone={SCHEDULER_TIMEZONE}",
        f"--uri={url}",
        "--http-method=POST",
        f"{headers_flag}=Content-Type=application/json",
        f"--message-body={scheduler_body()}",
        f"--oidc-service-account-email={email}",
        f"--oidc-token-audience={url}",
        f"--attempt-deadline={SCHEDULER_ATTEMPT_DEADLINE}",
        "--max-retry-attempts=1",
        "--project",
        args.project,
        "--quiet",
    )
    return readback_scheduler(args, state)


def same_endpoint(left: str | None, right: str | None) -> bool:
    """Compare endpoints modulo the trailing slash Cloud Scheduler adds.

    The API normalizes ``https://host`` to ``https://host/``; that is the same
    endpoint, and treating it as drift would fail every readback forever.
    Anything else is still a hard mismatch.
    """
    if left is None or right is None:
        return left == right
    return left.rstrip("/") == right.rstrip("/")


def readback_scheduler(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    job = gcloud_json(
        "scheduler",
        "jobs",
        "describe",
        SCHEDULER_JOB,
        f"--location={args.location}",
        "--project",
        args.project,
    )
    target = job.get("httpTarget") or {}
    oidc = target.get("oidcToken") or {}
    url = step_record(state, "dispatcher").get("url")
    email = step_record(state, "dispatcher_service_account").get("email")
    record = {
        "name": job.get("name"),
        "schedule": job.get("schedule"),
        "time_zone": job.get("timeZone"),
        "state": job.get("state"),
        "uri": target.get("uri"),
        "http_method": target.get("httpMethod"),
        "oidc_service_account": oidc.get("serviceAccountEmail"),
        "oidc_audience": oidc.get("audience"),
        "attempt_deadline": job.get("attemptDeadline"),
    }
    problems = []
    if record["schedule"] != SCHEDULER_CRON:
        problems.append(f"schedule {record['schedule']!r}")
    if record["time_zone"] != SCHEDULER_TIMEZONE:
        problems.append(f"time zone {record['time_zone']!r}")
    if url and not same_endpoint(record["uri"], url):
        problems.append(f"uri {record['uri']!r}")
    if email and record["oidc_service_account"] != email:
        problems.append(f"oidc identity {record['oidc_service_account']!r}")
    if url and not same_endpoint(record["oidc_audience"], url):
        problems.append(f"oidc audience {record['oidc_audience']!r}")
    if record["state"] != "ENABLED":
        problems.append(f"state {record['state']!r}")
    if problems:
        raise AmbiguousStateError(f"{SCHEDULER_JOB} does not match its contract: {problems}")
    return record


Step = tuple[str, Callable[..., dict[str, Any]], Callable[..., dict[str, Any]]]

STEPS: tuple[Step, ...] = (
    ("staging_bucket", apply_staging_bucket, readback_staging_bucket),
    ("runtime", apply_runtime, readback_runtime),
    ("runtime_roles", apply_runtime_roles, readback_runtime_roles),
    ("registry", apply_registry, readback_registry),
    ("dispatcher_service_account", apply_dispatcher_service_account, readback_dispatcher_service_account),
    ("dispatcher_iam", apply_dispatcher_iam, readback_dispatcher_iam),
    ("dispatcher", apply_dispatcher, readback_dispatcher),
    ("dispatcher_invoker", apply_dispatcher_invoker, readback_dispatcher_invoker),
    ("scheduler", apply_scheduler, readback_scheduler),
)


# --- Untouched-production evidence ------------------------------------------


def _acquire_container(job: dict[str, Any]) -> dict[str, Any]:
    """Dig the single acquisition container out of the Cloud Run job resource."""
    node: Any = job
    for key in ("spec", "template", "spec", "template", "spec"):
        node = (node or {}).get(key) or {}
    containers = node.get("containers") or [{}]
    return containers[0]


def untouched_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    """Read the analyst and acquisition production path without writing to it."""
    subscription = gcloud_json(
        "pubsub",
        "subscriptions",
        "describe",
        "tycho-analyst-push",
        "--project",
        args.project,
    )
    push = subscription.get("pushConfig") or {}
    oidc = push.get("oidcToken") or {}
    nightly = gcloud_json(
        "scheduler",
        "jobs",
        "describe",
        "tycho-nightly",
        f"--location={args.location}",
        "--project",
        args.project,
    )
    acquire = gcloud_json(
        "run",
        "jobs",
        "describe",
        "tycho-acquire",
        f"--region={args.location}",
        "--project",
        args.project,
    )
    container = _acquire_container(acquire)
    env = {item["name"]: item.get("value") for item in container.get("env", [])}
    services = gcloud_json(
        "run", "services", "list", f"--region={args.location}", "--project", args.project
    )
    runtimes = {
        engine.api_resource.name: getattr(engine.api_resource, "display_name", None)
        for engine in existing_runtimes(args)
    }
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "analyst_subscription": {
            "name": subscription.get("name"),
            "topic": subscription.get("topic"),
            "push_endpoint": push.get("pushEndpoint"),
            "oidc_service_account": oidc.get("serviceAccountEmail"),
            "oidc_audience": oidc.get("audience"),
            "ack_deadline_seconds": subscription.get("ackDeadlineSeconds"),
        },
        "acquisition_scheduler": {
            "name": nightly.get("name"),
            "schedule": nightly.get("schedule"),
            "time_zone": nightly.get("timeZone"),
            "state": nightly.get("state"),
        },
        "acquisition_job": {
            "generation": (acquire.get("metadata") or {}).get("generation"),
            "image": container.get("image"),
            "differ_mode": env.get("TYCHO_DIFFER_MODE"),
        },
        "cloud_run_services": sorted(
            (service.get("metadata") or {}).get("name") for service in services or []
        ),
        "agent_runtimes": dict(sorted(runtimes.items())),
    }


def bigquery_snapshot(project: str) -> dict[str, Any]:
    from google.cloud import bigquery

    bq = bigquery.Client(project=project)
    query = f"""
        SELECT
          (SELECT COUNT(*) FROM `{project}.tycho.deltas`) AS canonical_rows,
          (SELECT COUNTIF(schema_version = 'delta@2') FROM `{project}.tycho.deltas`)
            AS canonical_v2_rows,
          (SELECT COUNT(*) FROM `{project}.tycho.delta_audit_log_20260826`) AS audit_rows,
          (SELECT COUNT(*) FROM `{project}.tycho.observations`) AS observations,
          (SELECT TO_HEX(SHA256(COALESCE(STRING_AGG(delta_id, ',' ORDER BY delta_id), '')))
           FROM `{project}.tycho.deltas`) AS canonical_delta_id_hash
    """
    row = next(iter(bq.query(query).result()))
    return {key: row[key] for key in row.keys()}


def firestore_snapshot(project: str) -> dict[str, Any]:
    from google.cloud import firestore

    db = firestore.Client(project=project)
    claims = list(db.collection("claims").stream())
    statuses: dict[str, int] = {}
    for snapshot in claims:
        status = (snapshot.to_dict() or {}).get("status", "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "claim_documents": len(claims),
        "claims_by_status": dict(sorted(statuses.items())),
        "analyst_runs": sum(1 for _ in db.collection("analyst_runs").stream()),
        "alerts": sum(1 for _ in db.collection("alerts").stream()),
        "strategy_sessions": sum(1 for _ in db.collection("strategy_sessions").stream()),
        "strategy_leases": sum(1 for _ in db.collection("strategy_leases").stream()),
        "briefs": sum(1 for _ in db.collection("briefs").stream()),
    }


def full_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    return {
        **untouched_snapshot(args),
        "bigquery": bigquery_snapshot(args.project),
        "firestore": firestore_snapshot(args.project),
    }


# --- Actions ----------------------------------------------------------------


def plan(args: argparse.Namespace) -> None:
    period_from, period_to = previous_complete_week(datetime.now(UTC))
    print(
        json.dumps(
            {
                "project": args.project,
                "location": args.location,
                "runtime_display_name": DISPLAY_NAME,
                "runtime_roles": list(REQUIRED_ROLES),
                "dispatcher_service": DISPATCHER_SERVICE,
                "dispatcher_service_account": (
                    f"{DISPATCHER_SERVICE_ACCOUNT}@{args.project}.iam.gserviceaccount.com"
                ),
                "dispatcher_roles": list(DISPATCHER_ROLES),
                "scheduler_job": SCHEDULER_JOB,
                "scheduler_schedule": SCHEDULER_CRON,
                "scheduler_time_zone": SCHEDULER_TIMEZONE,
                "scheduler_body": json.loads(scheduler_body()),
                "protected_resources": sorted(PROTECTED_RESOURCES),
                "steps": [name for name, _, _ in STEPS],
                "period_if_triggered_now": {
                    "from": period_from.isoformat(),
                    "to": period_to.isoformat(),
                },
                "state_path": str(args.state),
            },
            indent=2,
            sort_keys=True,
        )
    )


def deploy(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state is None:
        state = new_state(args)
    elif not args.resume:
        raise SystemExit(
            f"{args.state} already exists; rerun with --resume to continue or read it back"
        )
    if state["project"] != args.project or state["location"] != args.location:
        raise AmbiguousStateError(
            f"recorded deployment targets {state['project']}/{state['location']}"
        )
    if args.resource_name:
        recorded = step_record(state, "runtime").get("resource_name")
        if recorded and recorded != args.resource_name:
            raise AmbiguousStateError("--resource-name does not match the recorded Runtime")
        state.setdefault("steps", {}).setdefault("runtime", {})[
            "resource_name"
        ] = args.resource_name

    if "untouched_before" not in state:
        state["untouched_before"] = full_snapshot(args)
        write_state(args.state, state)

    for name, apply_step, readback_step in STEPS:
        if completed(state, name):
            record = readback_step(args, state)
            state["steps"][name] = {**state["steps"][name], **record, "done": True}
            write_state(args.state, state)
            print(f"= {name} (already deployed; read back)")
            continue
        print(f"+ {name}")
        record = apply_step(args, state)
        # Persist the moment the durable resource exists, before the next step
        # can fail: a retry must never create a second one.
        state["steps"][name] = {**step_record(state, name), **record, "done": True}
        write_state(args.state, state)
        verified = readback_step(args, state)
        state["steps"][name] = {**state["steps"][name], **verified, "done": True}
        write_state(args.state, state)

    state["untouched_after"] = full_snapshot(args)
    state["deployed_at"] = datetime.now(UTC).isoformat()
    write_state(args.state, state)
    print(json.dumps(state["steps"], indent=2, sort_keys=True))


def update(args: argparse.Namespace) -> None:
    """Redeploy the council package onto the Runtime already recorded here."""
    state = load_state(args.state)
    if state is None:
        raise SystemExit(f"missing {args.state}; run deploy first")
    if not completed(state, "runtime"):
        raise AmbiguousStateError("no Strategy Council Runtime is recorded to update")
    record = update_runtime(args, state)
    state["steps"]["runtime"] = {**state["steps"]["runtime"], **record, "done": True}
    write_state(args.state, state)
    verified = readback_runtime(args, state)
    state["steps"]["runtime"] = {**state["steps"]["runtime"], **verified, "done": True}
    # The managed identity survives an update, but re-read the grants so a
    # widened role can never go unnoticed.
    state["steps"]["runtime_roles"] = {
        **state["steps"].get("runtime_roles", {}),
        **readback_runtime_roles(args, state),
        "done": True,
    }
    state["last_runtime_update_at"] = datetime.now(UTC).isoformat()
    write_state(args.state, state)
    print(json.dumps(state["steps"]["runtime"], indent=2, sort_keys=True))


def readback(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state is None:
        raise SystemExit(f"missing {args.state}; run deploy first")
    verified: dict[str, Any] = {}
    for name, _, readback_step in STEPS:
        if not completed(state, name):
            raise AmbiguousStateError(f"step {name} was never completed")
        verified[name] = readback_step(args, state)
        state["steps"][name] = {**state["steps"][name], **verified[name], "done": True}
    state["last_readback_at"] = datetime.now(UTC).isoformat()
    write_state(args.state, state)
    print(json.dumps(verified, indent=2, sort_keys=True))


def snapshot(args: argparse.Namespace) -> None:
    record = full_snapshot(args)
    if args.state.exists():
        state = load_state(args.state) or {}
        state[args.snapshot_key] = record
        write_state(args.state, state)
    print(json.dumps(record, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["plan", "deploy", "update", "readback", "snapshot"]
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--resource-name")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--snapshot-key", default="untouched_after")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    {
        "plan": plan,
        "deploy": deploy,
        "update": update,
        "readback": readback,
        "snapshot": snapshot,
    }[args.action](args)


if __name__ == "__main__":
    main()
