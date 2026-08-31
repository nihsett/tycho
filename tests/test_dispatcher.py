import base64
import json
from pathlib import Path

import pytest

from pipeline.dispatcher import (
    DispatcherRequestError,
    extract_runtime_result,
    parse_pubsub_delta,
    require_authenticated_push,
)
from schemas.delta import Delta

FIXTURE = Path("schemas/fixtures/delta.example.json")


def fixture_delta() -> Delta:
    return Delta.model_validate_json(FIXTURE.read_text())


def pubsub_body(delta: Delta) -> bytes:
    encoded = base64.b64encode(delta.model_dump_json().encode()).decode()
    return json.dumps(
        {"message": {"data": encoded, "messageId": "msg-123"}, "subscription": "shadow"}
    ).encode()


def test_dispatcher_decodes_and_validates_current_pubsub_delta():
    parsed = parse_pubsub_delta(pubsub_body(fixture_delta()))
    assert parsed.delta.delta_id == fixture_delta().delta_id
    assert parsed.message_id == "msg-123"


def test_dispatcher_rejects_malformed_envelopes():
    with pytest.raises(DispatcherRequestError):
        parse_pubsub_delta(b"not-json")
    with pytest.raises(DispatcherRequestError):
        parse_pubsub_delta(json.dumps({"message": {}}).encode())


def test_dispatcher_requires_cloud_run_authenticated_push():
    require_authenticated_push({"Authorization": "Bearer signed-by-cloud-run"})
    require_authenticated_push({"X-Serverless-Authorization": "Bearer verified-by-cloud-run"})
    with pytest.raises(DispatcherRequestError, match="authenticated"):
        require_authenticated_push({})


def test_dispatcher_extracts_only_bounded_runtime_result():
    result = extract_runtime_result(
        [
            {"content": {"parts": [{"text": "intermediate"}]}},
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "delta_id": fixture_delta().delta_id,
                                    "run_id": "run_shadow",
                                    "state": "completed",
                                    "action": "no_action",
                                    "analyst_version": "gemini-analyst@1",
                                    "model": "gemini-3.7-flash",
                                }
                            )
                        }
                    ]
                }
            },
        ]
    )
    assert result["state"] == "completed"
    assert result["action"] == "no_action"

    with pytest.raises(RuntimeError, match="no bounded result"):
        extract_runtime_result([{"content": {"parts": [{"text": "not-json"}]}}])
