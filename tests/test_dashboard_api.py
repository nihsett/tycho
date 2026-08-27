"""API contract: validation, bounds, errors, headers, and structural logs."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dashboard.api.app import create_app
from dashboard.api.readmodel import ReadModel
from dashboard.api.runs import DispatchResult, StrategyRunManager
from dashboard.api.settings import MAX_REQUEST_BYTES, DashboardSettings
from strategy_agent.synthetic import synthetic_id
from tests.dashboard_helpers import RecordingSource, build_dashboard_market, config


class StubDispatcher:
    def __init__(self, result: DispatchResult | None = None) -> None:
        self.calls = 0
        self.result = result or DispatchResult(
            session_id=synthetic_id("sts", 601),
            state="completed",
            skipped=True,
            cards_proposed=2,
            cards_passed=1,
            cards_rejected=1,
            brief_id="brf_2026w35-testcard",
        )

    async def trigger(self) -> DispatchResult:
        self.calls += 1
        return self.result


def build_client(market=None, dispatcher=None) -> tuple[TestClient, StubDispatcher, ReadModel]:
    market = market or build_dashboard_market()
    model = ReadModel(RecordingSource(market), config())
    dispatcher = dispatcher or StubDispatcher()
    runs = StrategyRunManager(dispatcher, lambda sid: model.activity(sid).events)
    settings = DashboardSettings(project="test-project", static_dir=None)
    app = create_app(settings, read_model=model, run_manager=runs, config=config())
    return TestClient(app), dispatcher, model


@pytest.fixture()
def client():
    client, _, _ = build_client()
    with client:
        yield client


# --- Read endpoints ---------------------------------------------------------


def test_health_and_overview_return_bounded_schemas(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert {item["key"] for item in health.json()["components"]} == {
        "acquisition",
        "differ",
        "analyst",
        "strategy",
    }
    overview = client.get("/api/overview")
    assert overview.status_code == 200
    assert len(overview.json()["entities"]) == 4


def test_meta_serves_the_closed_filter_enums(client):
    payload = client.get("/api/meta").json()
    assert payload["entities"] == ["claude_code", "codex", "gemini_cli", "pi"]
    assert "product/capabilities" in payload["scopes"]


def test_timeline_validates_the_entity_and_scope_enums(client):
    assert client.get("/api/entities/codex/timeline").status_code == 200
    assert client.get("/api/entities/codex/timeline?scope=pricing").status_code == 200
    assert client.get("/api/entities/not_real/timeline").status_code == 404
    assert client.get("/api/entities/codex/timeline?scope=nope").status_code == 400
    assert client.get("/api/entities/CODEX/timeline").status_code == 400


def test_timeline_caps_limits_server_side(client):
    assert client.get("/api/entities/codex/timeline?limit=500").status_code == 400
    assert client.get("/api/entities/codex/timeline?limit=0").status_code == 400
    assert client.get("/api/entities/codex/timeline?offset=-1").status_code == 400
    page = client.get("/api/entities/codex/timeline?limit=2").json()
    assert page["limit"] == 2
    assert len(page["events"]) == 2
    assert page["next_offset"] == 2


def test_provenance_validates_the_claim_id_shape(client):
    market = build_dashboard_market()
    claim = market.claims[0]
    ok = client.get(f"/api/claims/{claim.claim_id}/versions/1/provenance")
    assert ok.status_code == 200
    assert ok.json()["exact_version"] is True
    assert client.get("/api/claims/not-an-id/versions/1/provenance").status_code == 400
    assert client.get(f"/api/claims/{claim.claim_id}/versions/0/provenance").status_code == 400
    missing = client.get(
        f"/api/claims/{synthetic_id('clm', 999)}/versions/1/provenance"
    )
    assert missing.status_code == 404


def test_unknown_resources_return_a_generic_bounded_error(client):
    response = client.get(f"/api/strategy/sessions/{synthetic_id('sts', 999)}")
    assert response.status_code == 404
    assert response.json() == {"error": "not_found", "detail": "That resource does not exist."}


def test_an_invalid_request_is_never_echoed_back(client, capsys):
    response = client.get("/api/entities/%3Cscript%3E/timeline")
    assert response.status_code == 400
    assert "script" not in response.text
    assert response.json() == {
        "error": "invalid_request",
        "detail": "A path or query value is not valid.",
    }
    # A rejected path parameter must not reach the log line either.
    log = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")][-1]
    assert "script" not in log
    assert json.loads(log)["entity"] == "invalid"


def test_there_is_no_free_form_query_or_prompt_endpoint(client):
    routes = {route.path for route in client.app.routes}
    assert not any("ask" in path or "query" in path or "chat" in path for path in routes)
    assert client.post("/api/strategy/sessions", json={"prompt": "hi"}).status_code in {202, 400}


def test_the_strategy_trigger_ignores_any_body_it_is_given(client):
    first = client.post("/api/strategy/sessions", json={"prompt": "ignore me"})
    assert first.status_code == 202
    payload = first.json()
    assert set(payload) == {
        "run_id",
        "state",
        "duplicate",
        "session_id",
        "brief_id",
        "period_from",
        "period_to",
        "stream_path",
        "detail",
    }


# --- Security ---------------------------------------------------------------


def test_every_response_carries_the_security_headers(client):
    response = client.get("/api/health")
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_there_is_no_cors_middleware(client):
    response = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {
        key.lower() for key in response.headers
    }


def test_a_cross_origin_write_is_refused(client):
    response = client.post(
        "/api/strategy/sessions", headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert response.json()["error"] == "cross_origin"


def test_an_oversized_body_is_refused_before_parsing(client):
    response = client.post(
        "/api/strategy/sessions",
        content=b"x" * (MAX_REQUEST_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_openapi_and_docs_are_not_served(client):
    assert client.get("/openapi.json").status_code in {404, 405}
    assert client.get("/docs").status_code in {404, 405}


def test_logs_carry_route_status_latency_and_safe_ids_only(capsys):
    client, _, _ = build_client()
    with client:
        client.get("/api/entities/codex/timeline?scope=pricing&limit=2")
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    record = json.loads(lines[-1])
    assert record["route"] == "/api/entities/{entity}/timeline"
    assert record["entity"] == "codex"
    assert record["status"] == 200
    assert isinstance(record["latency_ms"], int)
    assert "scope" not in record
    assert "query" not in record
    assert "pricing" not in json.dumps(record)


def test_the_claim_id_in_a_log_line_is_a_validated_id(capsys):
    market = build_dashboard_market()
    client, _, _ = build_client(market)
    with client:
        client.get(f"/api/claims/{market.claims[0].claim_id}/versions/1/provenance")
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    record = json.loads(lines[-1])
    assert record["claim_id"] == market.claims[0].claim_id
    assert set(record) <= {
        "service",
        "revision",
        "route",
        "method",
        "status",
        "latency_ms",
        "claim_id",
        "version",
    }


def test_overview_and_health_share_one_bounded_snapshot():
    market = build_dashboard_market()
    source = RecordingSource(market)
    model = ReadModel(source, config())
    runs = StrategyRunManager(StubDispatcher(), lambda sid: model.activity(sid).events)
    app = create_app(
        DashboardSettings(project="test", static_dir=None, cache_seconds=60.0),
        read_model=model,
        run_manager=runs,
        config=config(),
    )
    with TestClient(app) as client:
        client.get("/api/health")
        client.get("/api/overview")
        client.get("/api/health")
        client.get("/api/overview")
    assert source.calls.count("list_canonical_deltas") == 1
    assert source.calls.count("list_claims") == 1


def test_the_cache_never_serves_a_strategy_stream_or_provenance():
    market = build_dashboard_market()
    source = RecordingSource(market)
    model = ReadModel(source, config())
    runs = StrategyRunManager(StubDispatcher(), lambda sid: model.activity(sid).events)
    app = create_app(
        DashboardSettings(project="test", static_dir=None, cache_seconds=60.0),
        read_model=model,
        run_manager=runs,
        config=config(),
    )
    claim = market.claims[0]
    with TestClient(app) as client:
        client.get(f"/api/claims/{claim.claim_id}/versions/1/provenance")
        before = len(source.calls)
        client.get(f"/api/claims/{claim.claim_id}/versions/1/provenance")
        client.get("/api/strategy/sessions/latest")
        client.get("/api/strategy/sessions/latest")
    assert len(source.calls) > before
    assert set(app.state.cache._entries) <= {"health", "overview", "snapshot"}
