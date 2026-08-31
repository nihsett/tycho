"""Deploy Tycho's analyst to Agent Runtime without touching live Pub/Sub routing.

Commands intentionally stop before production subscription cutover:

    uv run python -m infra.deploy_agent_runtime deploy
    uv run python -m infra.deploy_agent_runtime verify-shadow --delta-id dlt_...
    uv run python -m infra.deploy_agent_runtime prepare-live

``prepare-live`` updates the Runtime and prints the complete authenticated
Pub/Sub cutover command, but does not execute that command.

The existing ``tycho-analyst-push`` endpoint is read and recorded, never
modified by this module.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import agentplatform
from google.cloud import bigquery, firestore, storage
from google.genai import types

from pipeline.dispatcher import extract_runtime_result
from runtime_agent.agent import app

DEFAULT_PROJECT = "gen-lang-client-0110801105"
DEFAULT_LOCATION = "us-central1"
DEFAULT_BUCKET = f"{DEFAULT_PROJECT}-tycho-agent-staging"
DEFAULT_STATE_PATH = Path("data/agent_runtime_production.json")
DISPLAY_NAME = "Tycho Analyst"
RUNTIME_VERSION = "tycho-analyst-runtime@1"
RUNTIME_LABEL_VERSION = "tycho-analyst-runtime-1"
DISPATCHER_SERVICE = "tycho-analyst-dispatcher"
DISPATCHER_SERVICE_ACCOUNT = "tycho-agent-dispatcher"
SHADOW_SUBSCRIPTION = "tycho-analyst-runtime-shadow"
LIVE_SUBSCRIPTION = "tycho-analyst-push"

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


def run(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args))
    return subprocess.run(args, check=True, text=True, capture_output=capture)


def gcloud_exists(*args: str) -> bool:
    return subprocess.run(
        ("gcloud", *args), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


def client(project: str, location: str) -> agentplatform.Client:
    return agentplatform.Client(
        project=project,
        location=location,
        http_options=types.HttpOptions(api_version="v1beta1"),
    )


def ensure_staging_bucket(project: str, location: str, bucket_name: str) -> None:
    storage_client = storage.Client(project=project)
    bucket = storage_client.bucket(bucket_name)
    if bucket.exists():
        return
    bucket.storage_class = "STANDARD"
    bucket.labels = {
        "app": "tycho",
        "purpose": "agent-runtime-staging",
        "environment": "production-shadow",
    }
    storage_client.create_bucket(bucket, location=location)


def resource_record(remote: Any, project: str, location: str) -> dict[str, Any]:
    resource = remote.api_resource
    spec = getattr(resource, "spec", None)
    return {
        "project": project,
        "location": location,
        "resource_name": resource.name,
        "display_name": getattr(resource, "display_name", DISPLAY_NAME),
        "effective_identity": getattr(spec, "effective_identity", None),
        "version": RUNTIME_VERSION,
        "deployed_at": datetime.now(UTC).isoformat(),
    }


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


def grant_pubsub_token_creator(project: str, dispatcher_email: str) -> str:
    """Let Pub/Sub mint OIDC tokens for only the dispatcher identity."""
    project_number = run(
        "gcloud",
        "projects",
        "describe",
        project,
        "--format=value(projectNumber)",
        capture=True,
    ).stdout.strip()
    if not project_number:
        raise RuntimeError("project number lookup returned no value")
    pubsub_service_agent = (
        f"serviceAccount:service-{project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"
    )
    run(
        "gcloud",
        "iam",
        "service-accounts",
        "add-iam-policy-binding",
        dispatcher_email,
        "--member",
        pubsub_service_agent,
        "--role=roles/iam.serviceAccountTokenCreator",
        "--project",
        project,
        "--quiet",
    )
    return pubsub_service_agent


def build_cutover_command(state: dict[str, Any]) -> list[str]:
    """Build, but never execute, the complete authenticated push cutover."""
    return [
        "gcloud",
        "pubsub",
        "subscriptions",
        "modify-push-config",
        state["live_subscription"]["name"],
        f"--push-endpoint={state['dispatcher_url']}",
        f"--push-auth-service-account={state['dispatcher_service_account']}",
        f"--push-auth-token-audience={state['dispatcher_url']}",
        f"--project={state['project']}",
    ]


def print_cutover_instructions(state: dict[str, Any]) -> None:
    print("\nRuntime is prepared for live traffic; Pub/Sub routing is unchanged.")
    print(f"old endpoint: {state['live_subscription']['recorded_old_endpoint']}")
    print(f"new endpoint: {state['dispatcher_url']}")
    print("Run only after explicit cutover approval:")
    print(shlex.join(build_cutover_command(state)))


def ensure_service_account(project: str, account_name: str, display_name: str) -> str:
    email = f"{account_name}@{project}.iam.gserviceaccount.com"
    if not gcloud_exists(
        "iam",
        "service-accounts",
        "describe",
        email,
        "--project",
        project,
    ):
        run(
            "gcloud",
            "iam",
            "service-accounts",
            "create",
            account_name,
            "--display-name",
            display_name,
            "--project",
            project,
            "--quiet",
        )
    for attempt in range(10):
        if gcloud_exists(
            "iam",
            "service-accounts",
            "describe",
            email,
            "--project",
            project,
        ):
            return email
        if attempt < 9:
            time.sleep(3)
    raise RuntimeError(f"service account did not become visible: {email}")


def grant_runtime_roles(project: str, identity: str) -> list[str]:
    if not identity:
        raise RuntimeError("Agent Runtime did not return its managed identity")
    member = identity if identity.startswith("principal:") else f"principal://{identity}"
    roles = [
        "roles/datastore.user",
        "roles/bigquery.dataViewer",
        "roles/bigquery.jobUser",
        "roles/telemetry.tracesWriter",
    ]
    for role in roles:
        project_binding(project, member, role)
    return roles


def runtime_deployment_config(args: argparse.Namespace) -> dict[str, Any]:
    """Build one deployment config so deploy and prepare-live cannot diverge."""
    environment = (
        "production-live" if args.analyst_mode == "live" else "production-shadow"
    )
    phase = (
        "prepared for production traffic"
        if args.analyst_mode == "live"
        else "in shadow validation before cutover"
    )
    return {
        "display_name": DISPLAY_NAME,
        "description": (
            "Production Tycho analyst running the existing bounded ADK claim "
            f"lifecycle in Agent Runtime; {phase}."
        ),
        "labels": {
            "app": "tycho",
            "environment": environment,
            "version": RUNTIME_LABEL_VERSION,
        },
        "requirements": DEPLOYMENT_REQUIREMENTS,
        "extra_packages": ["runtime_agent", "pipeline", "schemas", "tycho.yaml"],
        "staging_bucket": f"gs://{args.bucket}",
        "gcs_dir_name": "tycho-analyst-runtime-v1",
        "identity_type": "AGENT_IDENTITY",
        "agent_framework": "google-adk",
        "min_instances": 0,
        "max_instances": 1,
        "env_vars": {
            "TYCHO_PROJECT": args.project,
            "TYCHO_DATASET": "tycho",
            "TYCHO_CONFIG": "tycho.yaml",
            "TYCHO_ANALYST_MODE": args.analyst_mode,
            "TYCHO_ANALYST_MODEL": "gemini-3.7-flash",
            "TYCHO_RUNTIME_LOCATION": args.location,
            "GOOGLE_CLOUD_LOCATION": "global",
            "GOOGLE_API_USE_CLIENT_CERTIFICATE": "true",
            "GOOGLE_API_USE_MTLS_ENDPOINT": "always",
            "GOOGLE_GENAI_USE_ENTERPRISE": "true",
            "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
            "OTEL_SERVICE_NAME": "tycho-analyst-runtime",
            "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
            "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false",
        },
    }


def registry_entry(project: str, location: str, resource_name: str) -> dict[str, Any] | None:
    result = run(
        "gcloud",
        "agent-registry",
        "agents",
        "list",
        f"--project={project}",
        f"--location={location}",
        "--format=json",
        capture=True,
    )
    entries = json.loads(result.stdout or "[]")
    runtime_uri = f"//aiplatform.googleapis.com/{resource_name}"
    for entry in entries:
        attributes = entry.get("attributes") or {}
        reference = attributes.get(
            "agentregistry.googleapis.com/system/RuntimeReference", {}
        ).get("uri")
        if reference == runtime_uri:
            return entry
    return None


def wait_for_registry_entry(
    project: str, location: str, resource_name: str, attempts: int = 12
) -> dict[str, Any]:
    for attempt in range(attempts):
        entry = registry_entry(project, location, resource_name)
        if entry is not None:
            return entry
        if attempt + 1 < attempts:
            time.sleep(5)
    raise RuntimeError("Agent Runtime was deployed but did not appear in Agent Registry")


def live_subscription_endpoint(project: str) -> str:
    result = run(
        "gcloud",
        "pubsub",
        "subscriptions",
        "describe",
        LIVE_SUBSCRIPTION,
        f"--project={project}",
        "--format=value(pushConfig.pushEndpoint)",
        capture=True,
    )
    return result.stdout.strip()


def deploy_dispatcher(
    project: str,
    location: str,
    resource_name: str,
    dispatcher_email: str,
) -> str:
    env = ",".join(
        [
            f"TYCHO_PROJECT={project}",
            f"TYCHO_RUNTIME_LOCATION={location}",
            f"TYCHO_AGENT_RUNTIME_RESOURCE={resource_name}",
            "TYCHO_DISPATCHER_TIMEOUT_SECONDS=540",
        ]
    )
    run(
        "gcloud",
        "run",
        "deploy",
        DISPATCHER_SERVICE,
        "--source=.",
        f"--region={location}",
        f"--service-account={dispatcher_email}",
        f"--set-env-vars={env}",
        "--set-build-env-vars=GOOGLE_ENTRYPOINT=python -m pipeline.dispatcher",
        "--command=",
        "--args=",
        "--timeout=10m",
        "--no-allow-unauthenticated",
        "--project",
        project,
        "--quiet",
    )
    url = run(
        "gcloud",
        "run",
        "services",
        "describe",
        DISPATCHER_SERVICE,
        f"--region={location}",
        "--format=value(status.url)",
        "--project",
        project,
        capture=True,
    ).stdout.strip()
    if not url:
        raise RuntimeError("dispatcher deployment returned no Cloud Run URL")
    run(
        "gcloud",
        "run",
        "services",
        "add-iam-policy-binding",
        DISPATCHER_SERVICE,
        f"--region={location}",
        f"--member=serviceAccount:{dispatcher_email}",
        "--role=roles/run.invoker",
        "--project",
        project,
        "--quiet",
    )
    return url


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def finish_shadow_deployment(
    args: argparse.Namespace,
    state: dict[str, Any],
    old_endpoint: str,
) -> None:
    dispatcher_email = state.get("dispatcher_service_account") or ensure_service_account(
        args.project,
        args.dispatcher_service_account,
        "Tycho Agent Runtime Pub/Sub dispatcher",
    )
    # Viewer includes reasoningEngines.query and is narrower than the user/editor
    # roles used by the old Cloud Run analyst. It grants no data-store access.
    project_binding(args.project, f"serviceAccount:{dispatcher_email}", "roles/aiplatform.viewer")
    project_binding(args.project, f"serviceAccount:{dispatcher_email}", "roles/logging.logWriter")
    dispatcher_url = deploy_dispatcher(
        args.project, args.location, state["resource_name"], dispatcher_email
    )
    pubsub_service_agent = grant_pubsub_token_creator(args.project, dispatcher_email)
    state.update(
        {
            "analyst_mode": args.analyst_mode,
            "staging_bucket": args.bucket,
            "dispatcher_service": DISPATCHER_SERVICE,
            "dispatcher_service_account": dispatcher_email,
            "dispatcher_url": dispatcher_url,
            "pubsub_oidc_token_creator": pubsub_service_agent,
            "live_subscription": {
                "name": LIVE_SUBSCRIPTION,
                "recorded_old_endpoint": old_endpoint,
                "cutover_performed": False,
                "prepared": args.analyst_mode == "live",
            },
            "shadow_subscription": {"name": SHADOW_SUBSCRIPTION, "created": False},
        }
    )
    write_state(args.state, state)
    print(json.dumps(state, indent=2, sort_keys=True))
    if args.analyst_mode == "live":
        print_cutover_instructions(state)


def deploy(args: argparse.Namespace) -> None:
    if args.state.exists():
        raise SystemExit(
            f"{args.state} already exists; use verify-shadow instead of deploying another runtime"
        )
    ensure_staging_bucket(args.project, args.location, args.bucket)
    old_endpoint = live_subscription_endpoint(args.project)

    remote = client(args.project, args.location).agent_engines.create(
        agent=app,
        config=runtime_deployment_config(args),
    )
    state = resource_record(remote, args.project, args.location)
    state["runtime_roles"] = grant_runtime_roles(
        args.project, state["effective_identity"]
    )
    state["registry_entry"] = wait_for_registry_entry(
        args.project, args.location, state["resource_name"]
    )
    state.update(
        {
            "staging_bucket": args.bucket,
            "live_subscription": {
                "name": LIVE_SUBSCRIPTION,
                "recorded_old_endpoint": old_endpoint,
                "cutover_performed": False,
            },
            "shadow_subscription": {"name": SHADOW_SUBSCRIPTION, "created": False},
        }
    )
    # Persist the runtime before creating the dispatcher so a transient IAM or
    # build failure can be resumed without creating a second runtime.
    write_state(args.state, state)
    finish_shadow_deployment(args, state, old_endpoint)


def update_runtime(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    """Refresh the existing runtime package without creating another resource."""
    remote = client(args.project, args.location).agent_engines.update(
        name=state["resource_name"],
        agent=app,
        config=runtime_deployment_config(args),
    )
    return resource_record(remote, args.project, args.location)


def resume(args: argparse.Namespace) -> None:
    if args.state.exists():
        state = _load_state(args.state)
        if args.resource_name and state["resource_name"] != args.resource_name:
            raise RuntimeError("--resource-name does not match the recorded runtime")
        old_endpoint = state["live_subscription"]["recorded_old_endpoint"]
    else:
        if not args.resource_name:
            raise SystemExit("resume requires --resource-name when no state file exists")
        remote = client(args.project, args.location).agent_engines.get(
            name=args.resource_name
        )
        state = resource_record(remote, args.project, args.location)
        state["runtime_roles"] = grant_runtime_roles(
            args.project, state["effective_identity"]
        )
        state["registry_entry"] = wait_for_registry_entry(
            args.project, args.location, state["resource_name"]
        )
        old_endpoint = live_subscription_endpoint(args.project)
        state.update(
            {
                "staging_bucket": args.bucket,
                "live_subscription": {
                    "name": LIVE_SUBSCRIPTION,
                    "recorded_old_endpoint": old_endpoint,
                    "cutover_performed": False,
                },
                "shadow_subscription": {"name": SHADOW_SUBSCRIPTION, "created": False},
            }
        )
        write_state(args.state, state)
    state.update(update_runtime(args, state))
    state["runtime_roles"] = grant_runtime_roles(
        args.project, state["effective_identity"]
    )
    state["registry_entry"] = wait_for_registry_entry(
        args.project, args.location, state["resource_name"]
    )
    write_state(args.state, state)
    finish_shadow_deployment(args, state, old_endpoint)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"missing {path}; run deploy first")
    return json.loads(path.read_text())


async def invoke_shadow(remote: Any, delta_id: str):
    events = []
    async for event in remote.async_stream_query(
        user_id="tycho-shadow-validator",
        message=json.dumps({"delta_id": delta_id}, separators=(",", ":")),
    ):
        events.append(event)
    return extract_runtime_result(events)


def latest_meaningful_delta(project: str) -> str:
    bq = bigquery.Client(project=project)
    query = f"""
        SELECT delta_id
        FROM `{project}.tycho.deltas`
        WHERE triage = 'meaningful'
        ORDER BY computed_at DESC
        LIMIT 1
    """
    rows = list(bq.query(query).result())
    if not rows:
        raise RuntimeError("no meaningful production delta exists for shadow validation")
    return rows[0]["delta_id"]


def claim_ids(project: str) -> set[str]:
    db = firestore.Client(project=project)
    return {snapshot.id for snapshot in db.collection("claims").stream()}


def trace_contains_unsafe_content(trace: dict[str, Any]) -> list[str]:
    forbidden = {
        "gcp.vertex.agent.llm_request",
        "gcp.vertex.agent.llm_response",
        "gen_ai.system_instructions",
        "gen_ai.user_messages",
        "gen_ai.tool_definitions",
        "gen_ai.response",
        "gen_ai.prompt",
        "gen_ai.completion",
    }
    found: list[str] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                current = f"{path}.{key}" if path else key
                if key in forbidden:
                    found.append(current)
                walk(child, current)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(trace)
    return found


def find_safe_trace(project: str, *, attempts: int = 12) -> dict[str, Any]:
    """Inspect Cloud Trace v1 directly; fail if the payload is not safe."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)
    url = f"https://cloudtrace.googleapis.com/v1/projects/{quote(project)}/traces"
    params = {"filter": "span:tycho", "view": "COMPLETE", "pageSize": "20"}
    for attempt in range(attempts):
        response = session.get(url, params=params, timeout=30)
        response.raise_for_status()
        traces = response.json().get("traces", [])
        candidates = [
            trace
            for trace in traces
            if any(
                span.get("name") == "tycho.analyze_delta"
                for span in trace.get("spans", [])
            )
        ]
        if candidates:
            trace = next(
                (
                    candidate
                    for candidate in candidates
                    if any(
                        span.get("name", "").startswith("generate_content ")
                        for span in candidate.get("spans", [])
                    )
                    and any(
                        span.get("name", "").startswith("execute_tool ")
                        for span in candidate.get("spans", [])
                    )
                ),
                candidates[0],
            )
            unsafe = trace_contains_unsafe_content(trace)
            if unsafe:
                raise RuntimeError(f"persisted trace contains unsafe fields: {unsafe}")
            spans = trace.get("spans", [])
            names = [span.get("name") for span in spans]
            return {
                "trace_id": trace.get("traceId"),
                "span_names": names,
                "real_gemini_tool_flow": any(
                    name.startswith("generate_content ") for name in names
                )
                and any(name.startswith("execute_tool ") for name in names),
                "unsafe_fields": [],
                "inspected_directly": True,
            }
        if attempt + 1 < attempts:
            time.sleep(5)
    raise RuntimeError("no persisted tycho.analyze_delta trace found")


def verify_shadow(args: argparse.Namespace) -> None:
    state = _load_state(args.state)
    delta_id = args.delta_id or latest_meaningful_delta(state["project"])
    before = claim_ids(state["project"])
    remote = client(state["project"], state["location"]).agent_engines.get(
        name=state["resource_name"]
    )
    result = asyncio.run(invoke_shadow(remote, delta_id))
    after = claim_ids(state["project"])
    if before != after:
        raise RuntimeError("shadow validation changed the Firestore claim set")
    if result.get("state") not in {"completed", "skipped"}:
        raise RuntimeError("shadow validation returned an invalid state")
    trace = find_safe_trace(state["project"])
    validation = {
        "validated_at": datetime.now(UTC).isoformat(),
        "delta_id": delta_id,
        "result": result,
        "claim_count_before": len(before),
        "claim_count_after": len(after),
        "claim_set_unchanged": True,
        "trace": trace,
    }
    state["shadow_validation"] = validation
    args.state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    print(json.dumps(validation, indent=2, sort_keys=True))


def create_shadow_subscription(args: argparse.Namespace) -> None:
    state = _load_state(args.state)
    project = state["project"]
    name = state.get("shadow_subscription", {}).get("name", SHADOW_SUBSCRIPTION)
    if gcloud_exists("pubsub", "subscriptions", "describe", name, f"--project={project}"):
        endpoint = run(
            "gcloud",
            "pubsub",
            "subscriptions",
            "describe",
            name,
            f"--project={project}",
            "--format=value(pushConfig.pushEndpoint)",
            capture=True,
        ).stdout.strip()
        if endpoint != state["dispatcher_url"]:
            raise RuntimeError(f"existing shadow subscription points to {endpoint!r}")
    else:
        run(
            "gcloud",
            "pubsub",
            "subscriptions",
            "create",
            name,
            "--topic=tycho-deltas",
            f"--push-endpoint={state['dispatcher_url']}",
            f"--push-auth-service-account={state['dispatcher_service_account']}",
            f"--push-auth-token-audience={state['dispatcher_url']}",
            "--ack-deadline=600",
            f"--project={project}",
            "--quiet",
        )
    state["shadow_subscription"] = {
        "name": name,
        "created": True,
        "endpoint": state["dispatcher_url"],
    }
    args.state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    print(json.dumps(state["shadow_subscription"], indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=[
            "deploy",
            "resume",
            "prepare-live",
            "verify-shadow",
            "create-shadow-subscription",
        ],
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--dispatcher-service-account", default=DISPATCHER_SERVICE_ACCOUNT)
    parser.add_argument("--delta-id")
    parser.add_argument("--resource-name")
    parser.add_argument("--analyst-mode", choices=["shadow", "live"], default="shadow")
    args = parser.parse_args()
    if args.action == "deploy":
        deploy(args)
    elif args.action == "resume":
        resume(args)
    elif args.action == "prepare-live":
        args.analyst_mode = "live"
        resume(args)
    elif args.action == "verify-shadow":
        verify_shadow(args)
    else:
        create_shadow_subscription(args)


if __name__ == "__main__":
    main()
