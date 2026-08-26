"""Deterministic or Gemini analyst behind a minimal Pub/Sub push HTTP service."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google.api_core.exceptions import AlreadyExists

from pipeline.claims import ClaimStore, validate_evidence_context
from pipeline.cloud import CloudBackend, CloudSettings
from schemas.claim import (
    Claim,
    ClaimClass,
    ClaimStatus,
    Confidence,
    Evidence,
    Severity,
)
from schemas.config import TychoConfig, load_config
from schemas.delta import Delta, DeltaSchemaVersion


def build_stub_claim(delta: Delta, config: TychoConfig) -> Claim:
    if delta.schema_version is not DeltaSchemaVersion.V2:
        raise ValueError("analyst accepts only canonical delta@2")
    entity = config.entities[delta.entity]
    semantic_changes = [
        change
        for change in delta.changes
        if delta.schema_version is DeltaSchemaVersion.V2 and change.statement
    ]
    added_releases = [
        change.after
        for change in delta.changes
        if change.before is None
        and isinstance(change.after, dict)
        and change.after.get("tag_name")
    ]
    created_at = delta.computed_at
    evidence_note = (
        "Change in the entity's official GitHub Releases feed."
        if delta.source == "github_releases"
        else "Change in the entity's official changelog."
    )
    if semantic_changes:
        statement = (
            f"{entity.name}: "
            + " ".join(change.statement.rstrip(".") for change in semantic_changes)
            + f" (observed on {created_at.date().isoformat()})."
        )
        evidence_note = "Grounded semantic change in the supplied observation."
    elif added_releases:
        release_details = ", ".join(
            f"{release['tag_name']} on {str(release.get('published_at') or 'an unknown date')[:10]}"
            for release in added_releases
        )
        statement = (
            f"{entity.name} published {release_details}; Tycho observed the official "
            f"feed change on {created_at.date().isoformat()}."
        )
    else:
        statement = (
            f"Tycho observed this official {entity.name} release-feed change on "
            f"{created_at.date().isoformat()}: {delta.summary.rstrip('.')}.")
    claim = Claim(
        claim_id=delta.delta_id.replace("dlt_", "clm_", 1),
        entity=delta.entity,
        scope=(
            semantic_changes[0].scope.value
            if semantic_changes and semantic_changes[0].scope
            else "product/capabilities"
        ),
        class_=ClaimClass.FACT,
        statement=statement,
        rationale=(
            "Official coding-agent releases can change capabilities, compatibility, "
            "and developer workflows immediately."
        ),
        confidence=Confidence.CONFIRMED,
        severity=Severity.NOTABLE,
        evidence=[
            Evidence(
                delta_id=delta.delta_id,
                source=delta.source,
                note=evidence_note,
            )
        ],
        status=ClaimStatus.ACTIVE,
        superseded_by=None,
        supersedes=None,
        version=1,
        created_at=created_at,
        last_verified_at=created_at,
        created_by="stub-analyst@1",
        history=[],
    )
    validate_evidence_context(claim, primary_sources={delta.source})
    return claim


def process_delta(
    delta: Delta,
    config: TychoConfig,
    store: ClaimStore,
) -> Claim:
    claim = build_stub_claim(delta, config)
    store.create_claim(claim)
    return claim


class AnalystHandler(BaseHTTPRequestHandler):
    config: TychoConfig
    mode: str
    store: CloudBackend

    def do_GET(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            envelope = json.loads(self.rfile.read(length))
            encoded = envelope["message"]["data"]
            delta = Delta.model_validate_json(base64.b64decode(encoded))
            if delta.schema_version is not DeltaSchemaVersion.V2:
                raise ValueError("delivery accepts only canonical delta@2")
            if self.mode in {"shadow", "live"}:
                from pipeline.gemini_analyst import run_analyst

                result = asyncio.run(
                    run_analyst(delta, self.config, self.store, mode=self.mode)
                )
                print(
                    json.dumps(
                        {
                            "delta_id": result.delta_id,
                            "mode": result.mode,
                            "model": result.model,
                            "action_names": [
                                action.get("action")
                                for action in result.actions
                                if isinstance(action, dict)
                            ],
                            "skipped": result.skipped,
                        },
                        sort_keys=True,
                    )
                )
            else:
                process_delta(delta, self.config, self.store)
        except AlreadyExists:
            pass  # Deterministic claim ID makes Pub/Sub redelivery idempotent.
        except Exception as exc:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            self.wfile.write(str(exc).encode())
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"analyst: {format % args}")


def main() -> None:
    config_path = os.getenv("TYCHO_CONFIG", "tycho.yaml")
    mode = os.getenv("TYCHO_ANALYST_MODE", "stub")
    if mode not in {"stub", "shadow", "live"}:
        raise RuntimeError("TYCHO_ANALYST_MODE must be stub, shadow, or live")
    AnalystHandler.config = load_config(config_path)
    AnalystHandler.mode = mode
    AnalystHandler.store = CloudBackend(CloudSettings.from_env())
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), AnalystHandler)
    print(
        f"analyst listening on {port} mode={mode} at "
        f"{datetime.now(UTC).isoformat()}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
