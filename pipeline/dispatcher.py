"""Authenticated Pub/Sub dispatcher for Tycho's Agent Runtime analyst."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import agentplatform
from google.genai import types

from schemas.delta import Delta, DeltaSchemaVersion

MAX_PUBSUB_BODY_BYTES = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 540


@dataclass(frozen=True)
class PubSubDeltaMessage:
    delta: Delta
    message_id: str


class DispatcherRequestError(ValueError):
    """The request is not a valid authenticated Pub/Sub Delta envelope."""


def parse_pubsub_delta(body: bytes) -> PubSubDeltaMessage:
    if len(body) > MAX_PUBSUB_BODY_BYTES:
        raise DispatcherRequestError("request body is too large")
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DispatcherRequestError("request body is not valid JSON") from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("message"), dict):
        raise DispatcherRequestError("request is missing the Pub/Sub message")
    message = envelope["message"]
    encoded = message.get("data")
    if not isinstance(encoded, str) or not encoded:
        raise DispatcherRequestError("Pub/Sub message data is missing")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise DispatcherRequestError("Pub/Sub message data is not valid base64") from exc
    try:
        delta = Delta.model_validate_json(payload)
    except ValueError as exc:
        raise DispatcherRequestError("Pub/Sub data is not a valid Tycho delta") from exc
    if delta.schema_version is not DeltaSchemaVersion.V2:
        raise DispatcherRequestError("Pub/Sub delivery accepts only canonical delta@2")
    return PubSubDeltaMessage(delta, str(message.get("messageId") or "unknown"))


def require_authenticated_push(headers: Any) -> None:
    """Rely on Cloud Run IAM, with an application-level defense-in-depth check."""
    authorization = headers.get("Authorization", "") or headers.get(
        "X-Serverless-Authorization", ""
    )
    if not authorization.startswith("Bearer "):
        raise DispatcherRequestError("authenticated Pub/Sub push is required")


class RuntimeInvoker:
    """Small client boundary that sends only a delta ID to Agent Runtime."""

    def __init__(
        self,
        resource_name: str,
        *,
        project: str,
        location: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not resource_name:
            raise ValueError("TYCHO_AGENT_RUNTIME_RESOURCE is required")
        self.resource_name = resource_name
        self.timeout_seconds = timeout_seconds
        self.client = agentplatform.Client(
            project=project,
            location=location,
            http_options=types.HttpOptions(api_version="v1beta1"),
        )
        self.remote = self.client.agent_engines.get(name=resource_name)

    async def _collect(self, delta_id: str, message_id: str) -> dict[str, Any]:
        events = []
        async for event in self.remote.async_stream_query(
            user_id="tycho-dispatcher",
            message=json.dumps({"delta_id": delta_id}, separators=(",", ":")),
        ):
            events.append(event)
        return extract_runtime_result(events)

    async def invoke(self, delta_id: str, message_id: str) -> dict[str, Any]:
        return await asyncio.wait_for(
            self._collect(delta_id, message_id), timeout=self.timeout_seconds
        )


def extract_runtime_result(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract the bounded JSON result from the final runtime event."""
    for event in reversed(events):
        output = event.get("output")
        if isinstance(output, dict) and output.get("state") in {"completed", "skipped"}:
            return output
        content = event.get("content")
        if not isinstance(content, dict):
            continue
        for part in reversed(content.get("parts") or []):
            text = part.get("text") if isinstance(part, dict) else None
            if not text:
                continue
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(result, dict) and result.get("state") in {"completed", "skipped"}:
                return result
    raise RuntimeError("Agent Runtime returned no bounded result")


class DispatcherHandler(BaseHTTPRequestHandler):
    invoker: RuntimeInvoker

    def do_GET(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self) -> None:
        try:
            # Cloud Run IAM is the authentication boundary: the platform
            # consumes the verified token before invoking this container. Do
            # not require the Authorization header to survive that hop.
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_PUBSUB_BODY_BYTES:
                raise DispatcherRequestError("invalid request size")
            message = parse_pubsub_delta(self.rfile.read(length))
            result = asyncio.run(
                self.invoker.invoke(message.delta.delta_id, message.message_id)
            )
            if result.get("state") not in {"completed", "skipped"}:
                raise RuntimeError("Agent Runtime returned an invalid state")
            print(
                json.dumps(
                    {
                        "delta_id": message.delta.delta_id,
                        "message_id": message.message_id,
                        "runtime": self.invoker.resource_name,
                        "run_id": result.get("run_id"),
                        "state": result.get("state"),
                        "action": result.get("action"),
                    },
                    sort_keys=True,
                )
            )
        except DispatcherRequestError as exc:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            self.wfile.write(str(exc).encode("utf-8"))
            return
        except asyncio.TimeoutError:
            print("dispatcher: Agent Runtime timed out; requesting Pub/Sub retry")
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.end_headers()
            return
        except Exception as exc:
            print(f"dispatcher: runtime invocation failed: {type(exc).__name__}")
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.end_headers()
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"dispatcher: {format % args}")


def main() -> None:
    project = os.getenv("TYCHO_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("TYCHO_PROJECT or GOOGLE_CLOUD_PROJECT is required")
    location = os.getenv("TYCHO_RUNTIME_LOCATION", "us-central1")
    timeout = float(os.getenv("TYCHO_DISPATCHER_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    DispatcherHandler.invoker = RuntimeInvoker(
        os.getenv("TYCHO_AGENT_RUNTIME_RESOURCE", ""),
        project=project,
        location=location,
        timeout_seconds=timeout,
    )
    port = int(os.getenv("PORT", "8080"))
    print(
        json.dumps(
            {
                "service": "tycho-analyst-dispatcher",
                "port": port,
                "runtime": DispatcherHandler.invoker.resource_name,
                "timeout_seconds": timeout,
            },
            sort_keys=True,
        )
    )
    ThreadingHTTPServer(("0.0.0.0", port), DispatcherHandler).serve_forever()


if __name__ == "__main__":
    main()
