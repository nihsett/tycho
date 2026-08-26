"""Idempotently deploy the tracer bullet to Google Cloud with gcloud."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from schemas.config import load_config

SERVICES = [
    "artifactregistry.googleapis.com",
    "bigquery.googleapis.com",
    "cloudbuild.googleapis.com",
    "aiplatform.googleapis.com",
    "firestore.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
]
ROLES = [
    "roles/aiplatform.user",
    "roles/bigquery.dataEditor",
    "roles/bigquery.jobUser",
    "roles/datastore.user",
    "roles/pubsub.publisher",
    "roles/storage.objectAdmin",
]


def run(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args))
    return subprocess.run(
        args,
        check=check,
        text=True,
        capture_output=capture,
    )


def gcloud_exists(*args: str) -> bool:
    return run("gcloud", *args, check=False, capture=True).returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--bucket")
    parser.add_argument("--service-account", default="tycho-runtime")
    parser.add_argument(
        "--differ-mode",
        choices=("semantic",),
        default=os.getenv("TYCHO_DIFFER_MODE", "semantic"),
        help="the only supported cloud acquisition mode",
    )
    parser.add_argument(
        "--semantic-differ-model",
        choices=("gemini-3.7-flash",),
        default=os.getenv("TYCHO_SEMANTIC_DIFFER_MODEL", "gemini-3.7-flash"),
    )
    parser.add_argument(
        "--replace-analyst-push",
        action="store_true",
        help=(
            "explicitly route the existing analyst subscription to the Cloud Run "
            "rollback service; by default an existing Runtime dispatcher route is preserved"
        ),
    )
    args = parser.parse_args()

    if shutil.which("gcloud") is None:
        raise SystemExit("gcloud is required: https://cloud.google.com/sdk/docs/install")
    if args.differ_mode != "semantic":
        raise SystemExit("cloud deployment refuses non-semantic Delta production")
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    config = load_config(root / "tycho.yaml")
    bucket = args.bucket or f"{args.project}-tycho-raw"
    account = f"{args.service_account}@{args.project}.iam.gserviceaccount.com"
    common = ["--project", args.project, "--quiet"]

    run("gcloud", "services", "enable", *SERVICES, *common)
    if not gcloud_exists(
        "iam", "service-accounts", "describe", account, "--project", args.project
    ):
        run(
            "gcloud",
            "iam",
            "service-accounts",
            "create",
            args.service_account,
            "--display-name",
            "Tycho runtime",
            *common,
        )
    for role in ROLES:
        run(
            "gcloud",
            "projects",
            "add-iam-policy-binding",
            args.project,
            "--member",
            f"serviceAccount:{account}",
            "--role",
            role,
            "--condition=None",
            "--quiet",
        )

    project_number = run(
        "gcloud",
        "projects",
        "describe",
        args.project,
        "--format=value(projectNumber)",
        capture=True,
    ).stdout.strip()
    run(
        "gcloud",
        "iam",
        "service-accounts",
        "add-iam-policy-binding",
        account,
        "--member",
        f"serviceAccount:service-{project_number}@gcp-sa-pubsub.iam.gserviceaccount.com",
        "--role=roles/iam.serviceAccountTokenCreator",
        *common,
    )

    if not gcloud_exists(
        "firestore",
        "databases",
        "describe",
        "--database=(default)",
        "--project",
        args.project,
    ):
        run(
            "gcloud",
            "firestore",
            "databases",
            "create",
            "--database=(default)",
            f"--location={args.region}",
            "--type=firestore-native",
            *common,
        )

    run(
        sys.executable,
        "-m",
        "infra.bootstrap",
        "--project",
        args.project,
        "--location",
        args.region,
        "--bucket",
        bucket,
    )
    runtime_env = (
        f"TYCHO_PROJECT={args.project},TYCHO_BUCKET={bucket},"
        "TYCHO_DATASET=tycho,TYCHO_TOPIC=tycho-deltas,TYCHO_CONFIG=tycho.yaml,"
        f"TYCHO_DIFFER_MODE={args.differ_mode},"
        f"TYCHO_SEMANTIC_DIFFER_MODEL={args.semantic_differ_model},"
        "GOOGLE_GENAI_USE_ENTERPRISE=true,GOOGLE_CLOUD_LOCATION=global,"
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false,"
        "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false"
    )
    analyst_env = (
        f"{runtime_env},TYCHO_ANALYST_MODE=live,"
        "TYCHO_ANALYST_MODEL=gemini-3.5-flash-lite,"
        "GOOGLE_GENAI_USE_ENTERPRISE=true,"
        f"GOOGLE_CLOUD_PROJECT={args.project},GOOGLE_CLOUD_LOCATION=global"
    )
    run(
        "gcloud",
        "run",
        "deploy",
        "tycho-analyst",
        "--source=.",
        f"--region={args.region}",
        f"--service-account={account}",
        f"--set-env-vars={analyst_env}",
        "--set-build-env-vars=GOOGLE_ENTRYPOINT=python -m pipeline.analyst",
        "--command=",
        "--args=",
        "--timeout=10m",
        "--no-allow-unauthenticated",
        *common,
    )
    analyst_url = run(
        "gcloud",
        "run",
        "services",
        "describe",
        "tycho-analyst",
        f"--region={args.region}",
        "--format=value(status.url)",
        "--project",
        args.project,
        capture=True,
    ).stdout.strip()
    run(
        "gcloud",
        "run",
        "services",
        "add-iam-policy-binding",
        "tycho-analyst",
        f"--region={args.region}",
        f"--member=serviceAccount:{account}",
        "--role=roles/run.invoker",
        *common,
    )

    analyst_image = run(
        "gcloud",
        "run",
        "services",
        "describe",
        "tycho-analyst",
        f"--region={args.region}",
        "--format=value(spec.template.spec.containers[0].image)",
        "--project",
        args.project,
        capture=True,
    ).stdout.strip()
    if not analyst_image:
        raise RuntimeError("healthy analyst image was not returned by Cloud Run")

    subscription = "tycho-analyst-push"
    if gcloud_exists("pubsub", "subscriptions", "describe", subscription, *common):
        if args.replace_analyst_push:
            run(
                "gcloud",
                "pubsub",
                "subscriptions",
                "modify-push-config",
                subscription,
                f"--push-endpoint={analyst_url}",
                f"--push-auth-service-account={account}",
                f"--push-auth-token-audience={analyst_url}",
                *common,
            )
        else:
            print(
                f"preserved existing {subscription} push route; "
                "pass --replace-analyst-push only for an explicit rollback"
            )
    else:
        run(
            "gcloud",
            "pubsub",
            "subscriptions",
            "create",
            subscription,
            "--topic=tycho-deltas",
            f"--push-endpoint={analyst_url}",
            f"--push-auth-service-account={account}",
            f"--push-auth-token-audience={analyst_url}",
            "--ack-deadline=600",
            *common,
        )
    run(
        "gcloud",
        "pubsub",
        "subscriptions",
        "update",
        subscription,
        "--ack-deadline=600",
        *common,
    )

    run(
        "gcloud",
        "run",
        "jobs",
        "deploy",
        "tycho-acquire",
        f"--image={analyst_image}",
        f"--region={args.region}",
        f"--service-account={account}",
        f"--set-env-vars={runtime_env}",
        "--command=/layers/google.python.uv/uv-dependencies/.venv/bin/python",
        "--args=-m,pipeline.acquire",
        "--max-retries=1",
        "--task-timeout=10m",
        *common,
    )
    run(
        "gcloud",
        "run",
        "jobs",
        "add-iam-policy-binding",
        "tycho-acquire",
        f"--region={args.region}",
        f"--member=serviceAccount:{account}",
        "--role=roles/run.invoker",
        *common,
    )

    schedule = config.schedules["github_releases"]
    scheduler_uri = (
        f"https://run.googleapis.com/v2/projects/{args.project}/locations/"
        f"{args.region}/jobs/tycho-acquire:run"
    )
    scheduler_args = [
        "--location",
        args.region,
        "--schedule",
        schedule,
        "--uri",
        scheduler_uri,
        "--http-method=POST",
        f"--oauth-service-account-email={account}",
        "--oauth-token-scope=https://www.googleapis.com/auth/cloud-platform",
        "--time-zone=Etc/UTC",
        *common,
    ]
    if gcloud_exists(
        "scheduler", "jobs", "describe", "tycho-nightly", "--location", args.region, *common
    ):
        run("gcloud", "scheduler", "jobs", "update", "http", "tycho-nightly", *scheduler_args)
    else:
        run("gcloud", "scheduler", "jobs", "create", "http", "tycho-nightly", *scheduler_args)

    print(f"deployed: {analyst_url}")
    print("verify: gcloud scheduler jobs run tycho-nightly " f"--location={args.region} --project={args.project}")


if __name__ == "__main__":
    main()
