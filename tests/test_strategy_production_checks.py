"""The read-only checks that decide whether production evidence may be recorded.

These run entirely on synthetic payloads: no BigQuery, Firestore, Cloud Trace,
Cloud Logging, or model call.
"""

import json
from pathlib import Path

import pytest

from infra import verify_strategy_production as verify
from infra.verify_strategy_production import (
    Corpus,
    VerificationError,
    premise_evidence,
    session_evidence,
    unsafe_fields,
)

CLAIM_TEXT = "Claude Code now sandboxes every tool call it executes on Windows."
QUOTE_TEXT = "Added an optional `powershell` tool for Windows execution paths."


def corpus() -> Corpus:
    return Corpus(
        snippets=(
            ("claim_statement:clm_a", verify._normalize(CLAIM_TEXT)[: verify.LEAK_WINDOW]),
            ("evidence_quote:dlt_a", verify._normalize(QUOTE_TEXT)[: verify.LEAK_WINDOW]),
        )
    )


# --- Forbidden field names --------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"spans": [{"labels": {"gcp.vertex.agent.llm_request": "..."}}]},
        {"spans": [{"labels": {"gcp.vertex.agent.llm_response": "..."}}]},
        {"attributes": {"gen_ai.system_instructions": "You are Tycho's strategist"}},
        {"attributes": {"gen_ai.user_messages": "..."}},
        {"attributes": {"gen_ai.tool_definitions": "..."}},
        {"jsonPayload": {"input_value": "..."}},
        {"jsonPayload": {"card": {"statement": "..."}}},
        {"jsonPayload": {"rationale": "..."}},
        {"jsonPayload": {"evidence_after": {"quote": "..."}}},
        {"jsonPayload": {"rendered_md": "..."}},
        {"jsonPayload": {"traceloop.entity.input.value": "..."}},
    ],
)
def test_a_content_bearing_field_name_is_reported(payload):
    assert unsafe_fields(payload)


def test_a_structural_span_graph_is_clean():
    trace = {
        "traceId": "6df87613b2397a8802335346b07aaa8d",
        "spans": [
            {
                "name": "invocation",
                "labels": {
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": "tycho_strategist",
                    "gen_ai.request.model": "gemini-3.7-flash",
                    "gen_ai.usage.input_tokens": "1341",
                    "gen_ai.usage.output_tokens": "402",
                    "gcp.vertex.agent.invocation_id": "e-123",
                },
            },
            {"name": "agent_run [tycho_challenger]", "labels": {}},
        ],
    }

    assert unsafe_fields(trace) == []


def test_the_dispatcher_log_line_shape_is_clean():
    from pipeline.strategy_dispatcher import SAFE_LOG_FIELDS

    entry = {
        "severity": "INFO",
        "textPayload": json.dumps({field: "x" for field in sorted(SAFE_LOG_FIELDS)}),
        "resource": {"labels": {"service_name": "tycho-strategy-dispatcher"}},
    }

    assert unsafe_fields(entry) == []


# --- Governed prose never appears -------------------------------------------


def test_governed_prose_in_a_trace_is_caught_even_when_the_key_looks_innocent():
    leaky = {"spans": [{"name": "generate_content", "labels": {"note": CLAIM_TEXT}}]}

    assert corpus().leaks_in(leaky) == ["claim_statement:clm_a"]
    # The key name alone would not have caught this one.
    assert unsafe_fields(leaky) == []


def test_a_grounded_quote_is_caught_after_whitespace_and_case_changes():
    leaky = {"payload": QUOTE_TEXT.upper().replace(" ", "   ")}

    assert corpus().leaks_in(leaky) == ["evidence_quote:dlt_a"]


def test_structural_telemetry_leaks_nothing():
    safe = {
        "traceId": "abc",
        "spans": [{"name": "invocation", "labels": {"gen_ai.agent.name": "tycho_brief_writer"}}],
    }

    assert corpus().leaks_in(safe) == []


def test_short_text_is_never_used_as_a_leak_probe():
    """A tiny snippet would match everything; the window has a floor."""
    assert verify._windows("short") == []
    assert verify._windows(None) == []
    assert len(verify._windows("x" * 64)[0]) == verify.LEAK_WINDOW


# --- Session evidence is recomputed, never trusted --------------------------


class FakeStore:
    """The two read methods the verifier is allowed to use, and nothing else."""

    def __init__(self, claims=None, deltas=None):
        self.claims = {claim.claim_id: claim for claim in claims or []}
        self.deltas = {delta.delta_id: delta for delta in deltas or []}

    def get_claim(self, claim_id):
        return self.claims.get(claim_id)

    def get_delta(self, delta_id):
        return self.deltas.get(delta_id)


def synthetic_store() -> FakeStore:
    from strategy_agent.synthetic import SYNTHETIC_NOW, build_synthetic_market

    market = build_synthetic_market(SYNTHETIC_NOW)
    return FakeStore(claims=market.claims, deltas=market.deltas)


def first_claim():
    from strategy_agent.synthetic import SYNTHETIC_NOW, build_synthetic_market

    return build_synthetic_market(SYNTHETIC_NOW).claims[0]


def zero_card_session():
    from schemas.strategy import SessionMetrics, StrategySession, manifest_hash

    session = StrategySession.model_validate(
        json.loads(Path("schemas/fixtures/strategy.session.example.json").read_text())
    )
    return session.model_copy(
        update={
            "cards": [],
            "challenges": [],
            "brief_id": None,
            "input_manifest": [],
            "manifest_hash": manifest_hash([]),
            "metrics": SessionMetrics(cards_proposed=0, input_tokens=300, output_tokens=150),
        }
    )


def test_a_premise_whose_version_moved_on_fails_verification():
    claim = first_claim().model_copy(update={"version": 4})
    store = FakeStore(claims=[claim])

    with pytest.raises(VerificationError, match="version 4, not 3"):
        premise_evidence(store, claim.claim_id, 3)


def test_a_retired_premise_fails_verification():
    from schemas.claim import ClaimStatus

    claim = first_claim().model_copy(update={"status": ClaimStatus.RETIRED})
    store = FakeStore(claims=[claim])

    with pytest.raises(VerificationError, match="not active"):
        premise_evidence(store, claim.claim_id, claim.version)


def test_a_missing_premise_fails_verification():
    with pytest.raises(VerificationError, match="no longer exists"):
        premise_evidence(FakeStore(), "clm_01M0YHH4S8VQKPZ4T0V2GJ7XQ2", 1)


def test_a_premise_resting_on_canonical_meaningful_evidence_verifies():
    store = synthetic_store()
    claim = first_claim()

    record = premise_evidence(store, claim.claim_id, claim.version)

    assert record["claim_id"] == claim.claim_id
    assert record["delta_ids"]
    assert record["source_families"]


def test_a_zero_card_session_is_a_valid_verified_result():
    record = session_evidence(FakeStore(), zero_card_session(), None)

    assert record["counts"]["zero_card_result"] is True
    assert record["counts"]["passed"] == 0
    assert record["cards"] == []
    assert record["brief"] is None
    assert record["manifest_hash"] == record["manifest_hash_recomputed"]


def test_the_recorded_cost_is_labelled_as_an_upper_bound():
    record = session_evidence(FakeStore(), zero_card_session(), None)

    assert "upper bound" in record["usage"]["cost_basis"]
    assert record["usage"]["estimated_cost_usd_upper_bound"] > 0


def test_the_session_evidence_record_carries_no_card_prose():
    from schemas.strategy import StrategySession

    session = StrategySession.model_validate(
        json.loads(Path("schemas/fixtures/strategy.session.example.json").read_text())
    )
    rejected_only = session.model_copy(
        update={
            "cards": [card for card in session.cards if card.status.value == "rejected"],
            "challenges": [],
            "brief_id": None,
        }
    )
    record = session_evidence(FakeStore(), rejected_only, None)

    serialized = json.dumps(record)
    for card in rejected_only.cards:
        assert card.statement not in serialized
        assert card.rationale not in serialized
        assert card.falsifier not in serialized


# --- The trace inspection must actually see every trace ---------------------


class FakeTraceApi:
    """Cloud Trace v1 pages: pageSize is a scan budget, not a result count."""

    def __init__(self, pages):
        self.pages = pages
        self.requested_tokens = []

    def get(self, url, params=None, timeout=None):
        token = (params or {}).get("pageToken")
        self.requested_tokens.append(token)
        index = 0 if token is None else int(token)
        body = self.pages[index]

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return body

        return Response()


def _install_fake_trace_api(monkeypatch, pages):
    api = FakeTraceApi(pages)
    monkeypatch.setattr(
        verify,
        "fetch_traces",
        verify.fetch_traces,  # keep the real implementation under test
    )
    import google.auth
    from google.auth.transport import requests as auth_requests

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (object(), "p"))
    monkeypatch.setattr(auth_requests, "AuthorizedSession", lambda credentials: api)
    return api


def test_every_page_of_traces_is_followed(monkeypatch):
    from datetime import UTC, datetime

    api = _install_fake_trace_api(
        monkeypatch,
        [
            {"traces": [{"traceId": "a"}], "nextPageToken": "1"},
            {"traces": [{"traceId": "b"}], "nextPageToken": "2"},
            {"traces": [{"traceId": "c"}]},
        ],
    )

    traces = verify.fetch_traces("p", since=datetime(2026, 8, 26, tzinfo=UTC))

    assert sorted(trace["traceId"] for trace in traces) == ["a", "b", "c"]
    assert api.requested_tokens == [None, "1", "2"]


def test_a_repeated_trace_across_pages_is_counted_once(monkeypatch):
    from datetime import UTC, datetime

    _install_fake_trace_api(
        monkeypatch,
        [
            {"traces": [{"traceId": "a"}], "nextPageToken": "1"},
            {"traces": [{"traceId": "a"}, {"traceId": "b"}]},
        ],
    )

    traces = verify.fetch_traces("p", since=datetime(2026, 8, 26, tzinfo=UTC))

    assert sorted(trace["traceId"] for trace in traces) == ["a", "b"]


def test_paging_that_never_terminates_fails_closed(monkeypatch):
    from datetime import UTC, datetime

    _install_fake_trace_api(monkeypatch, [{"traces": [{"traceId": "a"}], "nextPageToken": "0"}])

    with pytest.raises(VerificationError, match="incomplete"):
        verify.fetch_traces("p", since=datetime(2026, 8, 26, tzinfo=UTC), max_pages=3)


def test_a_dispatcher_result_line_is_read_from_either_payload_shape():
    line = {"request_id": "scheduler-2026w34", "state": "completed", "skipped": True}

    assert verify._dispatcher_result_line({"jsonPayload": line}) == line
    assert verify._dispatcher_result_line({"textPayload": json.dumps(line)}) == line
    assert verify._dispatcher_result_line({"textPayload": "Starting new instance."}) is None
    assert verify._dispatcher_result_line({"jsonPayload": {"port": 8080}}) is None
