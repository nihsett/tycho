"""Deploy and verify Tycho's isolated Enterprise Agent Platform spike.

This script never modifies the live Cloud Run analyst, Pub/Sub subscription,
scheduler, databases, or claim store. It creates a separate Agent Runtime with a
managed Agent Identity and relies on automatic Agent Registry registration.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import agentplatform
from google.cloud import storage
from google.genai import types

from platform_spike.agent import SPIKE_VERSION, app

DEFAULT_PROJECT = "gen-lang-client-0110801105"
DEFAULT_LOCATION = "us-central1"
DEFAULT_BUCKET = f"{DEFAULT_PROJECT}-tycho-agent-staging"
STATE_PATH = Path("data/platform_spike.json")
DISPLAY_NAME = "Tycho Platform Probe"

DEPLOYMENT_REQUIREMENTS = [
    "google-cloud-aiplatform[agent_engines,adk]==1.165.1",
    "google-adk==2.7.1",
    "google-genai==2.19.0",
    "cloudpickle==3.1.2",
    "pydantic==2.13.4",
]


def client(project: str, location: str) -> agentplatform.Client:
    """Return the current client-based Agent Platform SDK interface."""
    return agentplatform.Client(
        project=project,
        location=location,
        http_options=types.HttpOptions(api_version="v1beta1"),
    )


def ensure_staging_bucket(project: str, location: str, bucket_name: str) -> None:
    """Create the isolated staging bucket only when it does not exist."""
    storage_client = storage.Client(project=project)
    bucket = storage_client.bucket(bucket_name)
    if bucket.exists():
        return
    bucket.storage_class = "STANDARD"
    bucket.labels = {
        "app": "tycho",
        "purpose": "agent-runtime-spike",
        "environment": "non-production",
    }
    storage_client.create_bucket(bucket, location=location)


def resource_record(remote: Any, project: str, location: str) -> dict[str, Any]:
    """Serialize only safe deployment metadata for later verification."""
    resource = remote.api_resource
    return {
        "project": project,
        "location": location,
        "resource_name": resource.name,
        "display_name": getattr(resource, "display_name", DISPLAY_NAME),
        "effective_identity": getattr(getattr(resource, "spec", None), "effective_identity", None),
        "version": SPIKE_VERSION,
        "recorded_at": datetime.now(UTC).isoformat(),
    }


async def invoke(remote: Any) -> dict[str, Any]:
    """Invoke the remote probe and return bounded verification evidence."""
    session = await remote.async_create_session(user_id="tycho-platform-verifier")
    session_id = session.get("id") if isinstance(session, dict) else session.id
    events: list[dict[str, Any]] = []
    async for event in remote.async_stream_query(
        user_id="tycho-platform-verifier",
        session_id=session_id,
        message="Validate the managed platform path.",
    ):
        events.append(event)
    final_text = ""
    for event in reversed(events):
        parts = event.get("content", {}).get("parts", [])
        texts = [part.get("text", "") for part in parts if isinstance(part, dict)]
        if any(texts):
            final_text = "\n".join(text for text in texts if text).strip()
            break
    return {
        "session_id": session_id,
        "event_count": len(events),
        "final_text": final_text,
    }


def deploy(args: argparse.Namespace) -> None:
    if STATE_PATH.exists():
        record = json.loads(STATE_PATH.read_text())
        raise SystemExit(
            "platform spike state already exists for "
            f"{record.get('resource_name', 'an unknown runtime')}; use verify instead"
        )
    ensure_staging_bucket(args.project, args.location, args.bucket)
    platform_client = client(args.project, args.location)
    remote = platform_client.agent_engines.create(
        agent=app,
        config={
            "display_name": DISPLAY_NAME,
            "description": (
                "Non-production ADK probe for Agent Runtime, Agent Registry, "
                "Agent Identity, and OpenTelemetry validation."
            ),
            "labels": {
                "app": "tycho",
                "environment": "non-production",
                "version": "platform-spike-1",
            },
            "requirements": DEPLOYMENT_REQUIREMENTS,
            "extra_packages": ["platform_spike"],
            "staging_bucket": f"gs://{args.bucket}",
            "gcs_dir_name": "platform-spike-v1",
            "identity_type": "AGENT_IDENTITY",
            "agent_framework": "google-adk",
            "min_instances": 0,
            "max_instances": 1,
            "env_vars": {
                "TYCHO_PLATFORM_RUNTIME_LOCATION": args.location,
                "GOOGLE_GENAI_USE_ENTERPRISE": "true",
                "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
                "OTEL_SERVICE_NAME": "tycho-platform-spike",
                "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
                "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false",
            },
        },
    )
    record = resource_record(remote, args.project, args.location)
    verification = asyncio.run(invoke(remote))
    record["verification"] = verification
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))


def verify(args: argparse.Namespace) -> None:
    if not STATE_PATH.exists():
        raise SystemExit(f"missing {STATE_PATH}; deploy the spike first")
    record = json.loads(STATE_PATH.read_text())
    remote = client(args.project, record["location"]).agent_engines.get(
        name=record["resource_name"]
    )
    record["verification"] = asyncio.run(invoke(remote))
    record["verified_at"] = datetime.now(UTC).isoformat()
    STATE_PATH.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["deploy", "verify"])
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    args = parser.parse_args()
    if args.action == "deploy":
        deploy(args)
    else:
        verify(args)


if __name__ == "__main__":
    main()
