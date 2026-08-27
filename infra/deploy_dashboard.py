"""Deploy the Tycho Intelligence Dashboard, and nothing else.

    uv run python -m infra.deploy_dashboard plan
    uv run python -m infra.deploy_dashboard deploy --resume
    uv run python -m infra.deploy_dashboard readback
    uv run python -m infra.deploy_dashboard grant-viewer --member user:someone@example.com

This module creates one Cloud Run service, one service account, that account's
read-only project roles, and exactly one binding on the existing private
Strategy dispatcher so the dashboard can invoke it.

It is deliberately incapable of touching anything else in production.  Every
shell-out goes through :func:`run`, which reuses the analyst-path guard from
``infra.deploy_strategy_council`` and adds the strategy resources to it.  The
single exception is narrow and explicit: granting ``roles/run.invoker`` on
``tycho-strategy-dispatcher`` to this dashboard's own service account.  Any
other write naming a protected resource is refused, including a wider role, a
different member, or a different verb.

Deployment is resumable and idempotent: every durable resource is recorded the
moment it exists, every step is read back from the API rather than trusted from
the state file, and nothing is ever deleted or replaced.

Nothing here records a claim statement, a grounded quote, brief prose, or model
output: the state file holds resource names, identities, roles, digests, and
counts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from infra.deploy_strategy_council import (
    AmbiguousStateError,
    ProtectedResourceError,
    READ_ONLY_VERBS,
)
from infra.deploy_strategy_council import (
    PROTECTED_RESOURCES as ANALYST_PROTECTED_RESOURCES,
)

DEFAULT_PROJECT = "gen-lang-client-0110801105"
DEFAULT_REGION = "us-central1"
DEFAULT_STATE_PATH = Path("data/dashboard_production.json")

SERVICE = "tycho-dashboard"
SERVICE_ACCOUNT = "tycho-dashboard"
STRATEGY_DISPATCHER_SERVICE = "tycho-strategy-dispatcher"
ENTRYPOINT = "python -m dashboard.api.serve"
STATIC_DIR = "dashboard/frontend/dist"
CLOUD_RUN_TIMEOUT = "900"
MIN_INSTANCES = "0"
MAX_INSTANCES = "1"
CACHE_SECONDS = "60"
SERVICE_ACCOUNT_READBACK_ATTEMPTS = 10
SERVICE_ACCOUNT_READBACK_DELAY_SECONDS = 6.0
DISPATCH_TIMEOUT_SECONDS = "870"

#: The dashboard identity is read-only on data.  ``datastore.viewer`` cannot
#: write a claim; ``bigquery.dataViewer`` cannot append a Delta; neither can
#: read Cloud Storage or publish to Pub/Sub.
DASHBOARD_ROLES = (
    "roles/bigquery.dataViewer",
    "roles/bigquery.jobUser",
    "roles/datastore.viewer",
    "roles/logging.logWriter",
)

#: Roles this identity must never hold.  The readback fails closed on any of
#: them, so a widened grant is a deployment failure rather than a surprise.
FORBIDDEN_ROLE_PREFIXES = (
    "roles/storage",
    "roles/pubsub",
    "roles/aiplatform.memoryBank",
    "roles/cloudtrace",
    "roles/datastore.user",
    "roles/datastore.owner",
    "roles/bigquery.dataEditor",
    "roles/bigquery.dataOwner",
    "roles/bigquery.admin",
    "roles/editor",
    "roles/owner",
)

#: Everything the analyst deployment protects, plus the strategy council's own
#: resources: the dashboard reuses the dispatcher, it does not redeploy it.
PROTECTED_RESOURCES = frozenset(
    {
        *ANALYST_PROTECTED_RESOURCES,
        "tycho-strategy-dispatcher",
        "tycho-strategy-weekly",
        "Tycho Strategy Council",
        "reasoningEngines",
    }
)


def dashboard_member(project: str) -> str:
    return f"serviceAccount:{SERVICE_ACCOUNT}@{project}.iam.gserviceaccount.com"


def is_dispatcher_invoker_grant(tokens: tuple[str, ...]) -> bool:
    """The one write this module may make against a protected resource.

    It must be exactly: grant ``roles/run.invoker`` on the strategy dispatcher
    service to this dashboard's own service account.  A different verb, role,
    member, or resource is not this command and is refused.
    """
    if tokens[:4] != ("gcloud", "run", "services", "add-iam-policy-binding"):
        return False
    if len(tokens) < 5 or tokens[4] != STRATEGY_DISPATCHER_SERVICE:
        return False
    roles = [token for token in tokens if token.startswith("--role")]
    members = [token for token in tokens if token.startswith("--member")]
    if roles != ["--role=roles/run.invoker"] or len(members) != 1:
        return False
    member = members[0].removeprefix("--member=")
    return member.startswith("serviceAccount:") and member.split("@")[0] == (
        f"serviceAccount:{SERVICE_ACCOUNT}"
    )


def names_protected_resource(tokens: tuple[str, ...]) -> list[str]:
    """Which protected resources this command actually *targets*.

    A gcloud target is a positional argument.  A flag value is not a target:
    the dashboard legitimately passes the strategy dispatcher's URL as an
    environment variable, and that must not read as an attempt to modify it.
    A flag whose value is exactly a protected resource name is still treated as
    a target, so the exemption cannot be widened by moving the name into a flag.
    """
    mentioned: set[str] = set()
    for token in tokens:
        if token.startswith("-"):
            _, separator, value = token.partition("=")
            if separator and value in PROTECTED_RESOURCES:
                mentioned.add(value)
            continue
        mentioned.update(name for name in PROTECTED_RESOURCES if name in token)
    return sorted(mentioned)


def guard_command(args: tuple[str, ...]) -> None:
    """Refuse any non-read-only command that targets a protected resource."""
    tokens = tuple(str(arg) for arg in args)
    mentioned = names_protected_resource(tokens)
    if not mentioned:
        return
    if set(tokens) & READ_ONLY_VERBS:
        return
    if is_dispatcher_invoker_grant(tokens):
        return
    raise ProtectedResourceError(
        "the dashboard deployment may not modify existing production resources: "
        f"{mentioned}"
    )


def run(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    guard_command(args)
    print("+", " ".join(args))
    return subprocess.run(args, check=True, text=True, capture_output=capture)


def gcloud_json(*args: str) -> Any:
    result = run("gcloud", *args, "--format=json", capture=True)
    return json.loads(result.stdout or "null")


def gcloud_ok(*args: str) -> bool:
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


# --- State ------------------------------------------------------------------


def new_state(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "state_version": "dashboard-deployment@1",
        "project": args.project,
        "region": args.region,
        "service": SERVICE,
        "steps": {},
        "created_at": datetime.now(UTC).isoformat(),
    }


def load_state(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text()) if path.exists() else None


def write_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(UTC).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def step_record(state: dict[str, Any], name: str) -> dict[str, Any]:
    return state.setdefault("steps", {}).get(name, {})


def completed(state: dict[str, Any], name: str) -> bool:
    return bool(step_record(state, name).get("done"))


# --- Steps ------------------------------------------------------------------


def apply_service_account(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    del state
    email = f"{SERVICE_ACCOUNT}@{args.project}.iam.gserviceaccount.com"
    if not gcloud_ok("iam", "service-accounts", "describe", email, "--project", args.project):
        run(
            "gcloud",
            "iam",
            "service-accounts",
            "create",
            SERVICE_ACCOUNT,
            "--display-name=Tycho Intelligence Dashboard",
            "--project",
            args.project,
            "--quiet",
        )
    return {"email": email}


def readback_service_account(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    email = step_record(state, "service_account")["email"]
    # A freshly created service account is not immediately readable; IAM
    # propagation is eventually consistent. Retry rather than fail the run and
    # risk a second create on the next attempt.
    account: Any = None
    for attempt in range(SERVICE_ACCOUNT_READBACK_ATTEMPTS):
        try:
            account = gcloud_json(
                "iam", "service-accounts", "describe", email, "--project", args.project
            )
            break
        except subprocess.CalledProcessError:
            if attempt == SERVICE_ACCOUNT_READBACK_ATTEMPTS - 1:
                raise
            time.sleep(SERVICE_ACCOUNT_READBACK_DELAY_SECONDS)
    if not account or account.get("email") != email:
        raise AmbiguousStateError(f"service account readback returned {account.get('email')}")
    return {"email": email, "unique_id": account.get("uniqueId"), "disabled": account.get("disabled", False)}


def project_roles_for(project: str, member: str) -> list[str]:
    policy = gcloud_json("projects", "get-iam-policy", project)
    return sorted(
        binding["role"]
        for binding in policy.get("bindings", [])
        if member in binding.get("members", [])
    )


def apply_roles(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    member = f"serviceAccount:{step_record(state, 'service_account')['email']}"
    for role in DASHBOARD_ROLES:
        run(
            "gcloud",
            "projects",
            "add-iam-policy-binding",
            args.project,
            "--member",
            member,
            "--role",
            role,
            "--condition=None",
            "--quiet",
        )
    return {"member": member, "roles": list(DASHBOARD_ROLES)}


def readback_roles(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    member = f"serviceAccount:{step_record(state, 'service_account')['email']}"
    granted = project_roles_for(args.project, member)
    missing = sorted(set(DASHBOARD_ROLES) - set(granted))
    if missing:
        raise AmbiguousStateError(f"the dashboard identity is missing {missing}")
    extra = sorted(set(granted) - set(DASHBOARD_ROLES))
    if extra:
        raise AmbiguousStateError(f"the dashboard identity holds unexpected roles: {extra}")
    forbidden = sorted(
        role for role in granted if role.startswith(FORBIDDEN_ROLE_PREFIXES)
    )
    if forbidden:
        raise AmbiguousStateError(f"the dashboard identity holds forbidden roles: {forbidden}")
    return {"member": member, "roles": granted, "forbidden": []}


def dispatcher_url(args: argparse.Namespace) -> str:
    service = gcloud_json(
        "run",
        "services",
        "describe",
        STRATEGY_DISPATCHER_SERVICE,
        f"--region={args.region}",
        "--project",
        args.project,
    )
    url = (service.get("status") or {}).get("url")
    if not url:
        raise AmbiguousStateError("the strategy dispatcher has no Cloud Run URL")
    return str(url)


def apply_dispatcher_invoker(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    """The dashboard's only write against the existing strategy path."""
    member = f"serviceAccount:{step_record(state, 'service_account')['email']}"
    run(
        "gcloud",
        "run",
        "services",
        "add-iam-policy-binding",
        STRATEGY_DISPATCHER_SERVICE,
        f"--region={args.region}",
        f"--member={member}",
        "--role=roles/run.invoker",
        "--project",
        args.project,
        "--quiet",
    )
    return {"member": member, "target": STRATEGY_DISPATCHER_SERVICE}


def service_invokers(args: argparse.Namespace, service: str) -> list[str]:
    policy = gcloud_json(
        "run",
        "services",
        "get-iam-policy",
        service,
        f"--region={args.region}",
        "--project",
        args.project,
    )
    return sorted(
        {
            member
            for binding in (policy.get("bindings") or [])
            if binding.get("role") == "roles/run.invoker"
            for member in binding.get("members", [])
        }
    )


def readback_dispatcher_invoker(
    args: argparse.Namespace, state: dict[str, Any]
) -> dict[str, Any]:
    member = f"serviceAccount:{step_record(state, 'service_account')['email']}"
    invokers = service_invokers(args, STRATEGY_DISPATCHER_SERVICE)
    if member not in invokers:
        raise AmbiguousStateError(f"{member} cannot invoke the strategy dispatcher")
    public = sorted(set(invokers) & {"allUsers", "allAuthenticatedUsers"})
    if public:
        raise AmbiguousStateError(f"the strategy dispatcher is public: {public}")
    return {
        "member": member,
        "target": STRATEGY_DISPATCHER_SERVICE,
        "invokers": invokers,
        "url": dispatcher_url(args),
    }


def apply_service(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    email = step_record(state, "service_account")["email"]
    url = dispatcher_url(args)
    static = Path(STATIC_DIR)
    if not (static / "index.html").is_file():
        raise AmbiguousStateError(
            f"{STATIC_DIR}/index.html is missing; build the frontend before deploying"
        )
    env = ",".join(
        [
            f"TYCHO_PROJECT={args.project}",
            "TYCHO_DATASET=tycho",
            f"TYCHO_DASHBOARD_STATIC={STATIC_DIR}",
            f"TYCHO_STRATEGY_DISPATCHER_URL={url}",
            f"TYCHO_DASHBOARD_CACHE_SECONDS={CACHE_SECONDS}",
            f"TYCHO_DASHBOARD_DISPATCH_TIMEOUT={DISPATCH_TIMEOUT_SECONDS}",
        ]
    )
    run(
        "gcloud",
        "run",
        "deploy",
        SERVICE,
        "--source=.",
        f"--region={args.region}",
        f"--service-account={email}",
        f"--set-env-vars={env}",
        f"--set-build-env-vars=GOOGLE_ENTRYPOINT={ENTRYPOINT}",
        "--command=",
        "--args=",
        f"--timeout={CLOUD_RUN_TIMEOUT}",
        "--no-allow-unauthenticated",
        "--ingress=all",
        f"--min-instances={MIN_INSTANCES}",
        f"--max-instances={MAX_INSTANCES}",
        "--cpu=1",
        "--memory=1Gi",
        "--project",
        args.project,
        "--quiet",
    )
    return {"service": SERVICE, "strategy_dispatcher_url": url}


def readback_service(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    expected = step_record(state, "service_account").get("email")
    service = gcloud_json(
        "run",
        "services",
        "describe",
        SERVICE,
        f"--region={args.region}",
        "--project",
        args.project,
    )
    status = service.get("status") or {}
    template = ((service.get("spec") or {}).get("template") or {})
    template_spec = template.get("spec") or {}
    container = (template_spec.get("containers") or [{}])[0]
    annotations = (template.get("metadata") or {}).get("annotations", {})
    url = status.get("url")
    account = template_spec.get("serviceAccountName")
    if not url:
        raise AmbiguousStateError("the dashboard has no Cloud Run URL")
    if expected and account != expected:
        raise AmbiguousStateError(f"the dashboard runs as {account}, expected {expected}")
    invokers = service_invokers(args, SERVICE)
    public = sorted(set(invokers) & {"allUsers", "allAuthenticatedUsers"})
    if public:
        raise AmbiguousStateError(f"the dashboard is public: {public}")
    ingress = (service.get("metadata") or {}).get("annotations", {}).get(
        "run.googleapis.com/ingress"
    )
    env = {
        item.get("name"): item.get("value")
        for item in container.get("env", [])
        if item.get("name")
    }
    # Cloud Run omits the minScale annotation when it is the default of zero.
    min_instances = annotations.get("autoscaling.knative.dev/minScale") or "0"
    max_instances = annotations.get("autoscaling.knative.dev/maxScale")
    if min_instances != MIN_INSTANCES or max_instances != MAX_INSTANCES:
        raise AmbiguousStateError(
            f"the dashboard scales {min_instances}/{max_instances}, "
            f"expected {MIN_INSTANCES}/{MAX_INSTANCES}"
        )
    ready = next(
        (
            condition
            for condition in status.get("conditions", [])
            if condition.get("type") == "Ready"
        ),
        {},
    )
    return {
        "service": SERVICE,
        "url": url,
        "service_account": account,
        "image": container.get("image"),
        "revision": status.get("latestReadyRevisionName"),
        "ready": ready.get("status"),
        "ingress": ingress,
        "min_instances": min_instances,
        "max_instances": max_instances,
        "timeout_seconds": template_spec.get("timeoutSeconds"),
        "invokers": invokers,
        "public_invokers": public,
        "env": env,
    }


STEPS: tuple[tuple[str, Any, Any], ...] = (
    ("service_account", apply_service_account, readback_service_account),
    ("roles", apply_roles, readback_roles),
    ("dispatcher_invoker", apply_dispatcher_invoker, readback_dispatcher_invoker),
    ("service", apply_service, readback_service),
)


# --- Evidence ---------------------------------------------------------------


def image_digest(args: argparse.Namespace, image: str | None) -> str | None:
    if not image:
        return None
    if "@sha256:" in image:
        return image.split("@", 1)[1]
    try:
        described = gcloud_json(
            "artifacts", "docker", "images", "describe", image, "--project", args.project
        )
    except subprocess.CalledProcessError:
        return None
    return (described or {}).get("image_summary", {}).get("digest")


def untouched_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    """Read-only evidence that the strategy and analyst paths are unchanged."""
    dispatcher = gcloud_json(
        "run",
        "services",
        "describe",
        STRATEGY_DISPATCHER_SERVICE,
        f"--region={args.region}",
        "--project",
        args.project,
    )
    scheduler = gcloud_json(
        "scheduler",
        "jobs",
        "describe",
        "tycho-strategy-weekly",
        f"--location={args.region}",
        "--project",
        args.project,
    )
    nightly = gcloud_json(
        "scheduler",
        "jobs",
        "describe",
        "tycho-nightly",
        f"--location={args.region}",
        "--project",
        args.project,
    )
    subscription = gcloud_json(
        "pubsub", "subscriptions", "describe", "tycho-analyst-push", "--project", args.project
    )
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "strategy_dispatcher": {
            "url": (dispatcher.get("status") or {}).get("url"),
            "service_account": (
                ((dispatcher.get("spec") or {}).get("template") or {}).get("spec") or {}
            ).get("serviceAccountName"),
            "latest_ready_revision": (dispatcher.get("status") or {}).get(
                "latestReadyRevisionName"
            ),
            "invokers": service_invokers(args, STRATEGY_DISPATCHER_SERVICE),
        },
        "tycho_strategy_weekly": {
            "schedule": scheduler.get("schedule"),
            "time_zone": scheduler.get("timeZone"),
            "state": scheduler.get("state"),
            "uri": (scheduler.get("httpTarget") or {}).get("uri"),
        },
        "tycho_nightly": {
            "schedule": nightly.get("schedule"),
            "time_zone": nightly.get("timeZone"),
            "state": nightly.get("state"),
        },
        "tycho_analyst_push": {
            "push_endpoint": (subscription.get("pushConfig") or {}).get("pushEndpoint"),
            "ack_deadline_seconds": subscription.get("ackDeadlineSeconds"),
            "topic": subscription.get("topic"),
        },
    }


def data_snapshot(project: str) -> dict[str, Any]:
    """Counts and a content hash, so a dashboard read can be shown to mutate nothing."""
    from google.cloud import bigquery, firestore

    bq = bigquery.Client(project=project)
    query = f"""
        SELECT
          (SELECT COUNT(*) FROM `{project}.tycho.deltas`) AS canonical_rows,
          (SELECT COUNTIF(schema_version = 'delta@2') FROM `{project}.tycho.deltas`)
            AS canonical_v2_rows,
          (SELECT COUNT(*) FROM `{project}.tycho.observations`) AS observations,
          (SELECT TO_HEX(SHA256(COALESCE(STRING_AGG(delta_id, ',' ORDER BY delta_id), '')))
           FROM `{project}.tycho.deltas`) AS canonical_delta_id_hash
    """
    row = next(iter(bq.query(query).result()))
    db = firestore.Client(project=project)
    claims = list(db.collection("claims").stream())
    statuses: dict[str, int] = {}
    for snapshot in claims:
        status = (snapshot.to_dict() or {}).get("status", "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "bigquery": {key: row[key] for key in row.keys()},
        "firestore": {
            "claim_documents": len(claims),
            "claims_by_status": dict(sorted(statuses.items())),
            "strategy_sessions": sum(1 for _ in db.collection("strategy_sessions").stream()),
            "strategy_leases": sum(1 for _ in db.collection("strategy_leases").stream()),
            "briefs": sum(1 for _ in db.collection("briefs").stream()),
            "analyst_runs": sum(1 for _ in db.collection("analyst_runs").stream()),
            "alerts": sum(1 for _ in db.collection("alerts").stream()),
        },
    }


# --- Actions ----------------------------------------------------------------


def plan(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            {
                "project": args.project,
                "region": args.region,
                "service": SERVICE,
                "service_account": f"{SERVICE_ACCOUNT}@{args.project}.iam.gserviceaccount.com",
                "roles": list(DASHBOARD_ROLES),
                "forbidden_role_prefixes": list(FORBIDDEN_ROLE_PREFIXES),
                "entrypoint": ENTRYPOINT,
                "static_dir": STATIC_DIR,
                "min_instances": MIN_INSTANCES,
                "max_instances": MAX_INSTANCES,
                "authenticated": True,
                "strategy_dispatcher_binding": {
                    "service": STRATEGY_DISPATCHER_SERVICE,
                    "role": "roles/run.invoker",
                },
                "protected_resources": sorted(PROTECTED_RESOURCES),
                "steps": [name for name, _, _ in STEPS],
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
    if state["project"] != args.project or state["region"] != args.region:
        raise AmbiguousStateError(
            f"recorded deployment targets {state['project']}/{state['region']}"
        )
    if "untouched_before" not in state:
        state["untouched_before"] = untouched_snapshot(args)
        state["data_before"] = data_snapshot(args.project)
        write_state(args.state, state)

    for name, apply_step, readback_step in STEPS:
        if completed(state, name) and args.force_step != name:
            record = readback_step(args, state)
            state["steps"][name] = {**state["steps"][name], **record, "done": True}
            write_state(args.state, state)
            print(f"= {name} (already applied; read back)")
            continue
        print(f"+ {name}")
        record = apply_step(args, state)
        state["steps"][name] = {**step_record(state, name), **record, "done": True}
        write_state(args.state, state)
        verified = readback_step(args, state)
        state["steps"][name] = {**state["steps"][name], **verified, "done": True}
        write_state(args.state, state)

    service = state["steps"]["service"]
    service["image_digest"] = image_digest(args, service.get("image"))
    state["untouched_after"] = untouched_snapshot(args)
    state["data_after"] = data_snapshot(args.project)
    state["deployed_at"] = datetime.now(UTC).isoformat()
    write_state(args.state, state)
    print(json.dumps(state["steps"], indent=2, sort_keys=True))


def readback(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state is None:
        raise SystemExit(f"missing {args.state}; run deploy first")
    verified = {
        name: readback_step(args, state) for name, _, readback_step in STEPS
    }
    verified["service"]["image_digest"] = image_digest(
        args, verified["service"].get("image")
    )
    print(json.dumps(verified, indent=2, sort_keys=True))


def grant_viewer(args: argparse.Namespace) -> None:
    """Give one named identity permission to open the private dashboard."""
    if not args.member or ":" not in args.member:
        raise SystemExit("--member must look like user:someone@example.com")
    if args.member in {"allUsers", "allAuthenticatedUsers"}:
        raise SystemExit("the dashboard is private; refusing a public binding")
    run(
        "gcloud",
        "run",
        "services",
        "add-iam-policy-binding",
        SERVICE,
        f"--region={args.region}",
        f"--member={args.member}",
        "--role=roles/run.invoker",
        "--project",
        args.project,
        "--quiet",
    )
    invokers = service_invokers(args, SERVICE)
    public = sorted(set(invokers) & {"allUsers", "allAuthenticatedUsers"})
    if public:
        raise AmbiguousStateError(f"the dashboard is public: {public}")
    print(json.dumps({"service": SERVICE, "invokers": invokers}, indent=2, sort_keys=True))


def snapshot(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            {
                "untouched": untouched_snapshot(args),
                "data": data_snapshot(args.project),
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["plan", "deploy", "readback", "grant-viewer", "snapshot"]
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force-step", default=None)
    parser.add_argument("--member", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    {
        "plan": plan,
        "deploy": deploy,
        "readback": readback,
        "grant-viewer": grant_viewer,
        "snapshot": snapshot,
    }[args.action](args)


if __name__ == "__main__":
    main()
