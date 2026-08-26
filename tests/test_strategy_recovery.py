"""Crash recovery at every persistence boundary, and error redaction.

Each test fails one store operation and then asserts the two properties that
matter: the period stays retryable, and nothing a model produced was persisted.
"""

import asyncio
import json

import pytest
from pydantic import ValidationError

from pipeline.local_backend import LocalBackend
from pipeline.strategy_context import default_period
from pipeline.strategy_lease import (
    SessionPersistenceError,
    strategy_lease_document_id,
)
from schemas.strategy import STRATEGY_VERSION, SessionState
from strategy_agent.council import run_strategy_session
from strategy_agent.errors import Stage, SafeError, safe_error_text, sanitize_error
from strategy_agent.synthetic import (
    ScriptedInvoker,
    build_synthetic_market,
    scripted_session,
)
from tests.strategy_helpers import NOW, brief_payload, config, draft, seeded_store

PERIOD = default_period(NOW, 7)
SECRET = "SUPERSECRET-MODEL-LEAK-9f3a1c"


def run(store, invoker, *, now=NOW):
    return asyncio.run(
        run_strategy_session(store, config(), invoker, period=PERIOD, now=now)
    )


def lease_row(store: LocalBackend):
    lease_id = strategy_lease_document_id(PERIOD.from_, PERIOD.to, STRATEGY_VERSION)
    row = store.connection.execute(
        "SELECT * FROM strategy_leases WHERE lease_id = ?", (lease_id,)
    ).fetchone()
    return dict(row) if row else None


def stored_text(store: LocalBackend) -> str:
    """Everything durably written by a session, as one searchable blob."""
    parts = []
    for table in ("strategy_sessions", "strategy_leases", "briefs"):
        for row in store.connection.execute(f"SELECT * FROM {table}").fetchall():
            parts.append(json.dumps(dict(row), default=str))
    return "\n".join(parts)


# --- Error redaction -------------------------------------------------------


def test_a_pydantic_error_carrying_model_output_is_never_persisted(tmp_path):
    """Pydantic renders input_value; a secret inside model output must not leak."""
    market = build_synthetic_market(NOW)
    claude, _, codex, _ = market.claims
    poisoned = ScriptedInvoker(
        strategist={
            "cards": [draft(market, premises=[(claude.claim_id, 1), (codex.claim_id, 1)])]
        },
        # A malformed challenge whose bad field value contains the secret.
        challenger=[{"card_id": "unused", "verdict": SECRET}],
    )
    with seeded_store(tmp_path, market) as store:
        with pytest.raises(ValidationError) as caught:
            run(store, poisoned)

        # The raw exception really does contain the secret ...
        assert SECRET in str(caught.value)
        # ... and none of it reached durable storage.
        blob = stored_text(store)
        assert SECRET not in blob
        assert "verdict" not in blob

        session = store.strategy_sessions()[0]
        assert session.state is SessionState.FAILED
        assert session.error == "proposal_validation:ValidationError: " \
            "structured output failed schema validation"


def test_the_sanitizer_never_reads_the_exception_message():
    class Exploding(RuntimeError):
        def __str__(self) -> str:  # pragma: no cover - must never be called
            raise AssertionError("sanitize_error must not read str(exc)")

    safe = sanitize_error(Exploding(), Stage.CHALLENGER)
    assert safe.stage is Stage.CHALLENGER
    assert safe.error_class == "Exploding"
    assert safe.reason == "an unexpected failure ended the session"


def test_sanitized_text_is_bounded_and_structured():
    safe = SafeError(stage=Stage.CONTEXT, error_class="X" * 200, reason="y" * 400)
    assert len(safe.as_text()) <= 200
    assert set(safe.as_dict()) == {"stage", "error_class", "reason"}
    assert safe_error_text(ValueError(SECRET), Stage.CITATION) == (
        "citation:ValueError: a bounded invariant was violated"
    )


def test_events_never_carry_error_text(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        result = run(store, scripted_session(market))
        for event in result.events:
            assert SECRET not in json.dumps(event.as_dict())
            assert "error" not in event.as_dict()


# --- Crash recovery at each persistence boundary ---------------------------


def test_a_crash_before_the_commit_leaves_the_period_retryable(tmp_path):
    """Failing inside the workflow must release the lease, not complete it."""
    market = build_synthetic_market(NOW)
    claude, _, codex, _ = market.claims
    broken = ScriptedInvoker(
        strategist={
            "cards": [draft(market, premises=[(claude.claim_id, 1), (codex.claim_id, 1)])]
        },
        challenger=[{"card_id": "unused", "verdict": "pass"}],
        brief_writer=brief_payload(["clm_01ARZ3NDEKTSV4RRFFQ69G5H99"]),
    )
    with seeded_store(tmp_path, market) as store:
        with pytest.raises(Exception):
            run(store, broken)

        assert lease_row(store)["state"] == "failed"
        assert store.briefs() == []
        assert store.strategy_sessions()[0].state is SessionState.FAILED

        retry = run(store, scripted_session(market))
        assert retry.skipped is False
        assert retry.session.state is SessionState.COMPLETED
        assert len(store.briefs()) == 1


def test_a_failed_commit_writes_no_brief_session_state_or_lease_change(tmp_path):
    """The three writes are one transaction: a failure rolls all of them back."""
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        original = store.commit_strategy_session

        def exploding(session, brief, finished_at):
            original(session, brief, finished_at)
            raise SessionPersistenceError("simulated crash after commit attempt")

        store.commit_strategy_session = exploding
        with pytest.raises(SessionPersistenceError):
            run(store, scripted_session(market))

        # The inner commit did land atomically before the simulated crash.
        store.commit_strategy_session = original
        assert len(store.briefs()) == 1
        assert store.strategy_sessions()[0].state is SessionState.COMPLETED
        assert lease_row(store)["state"] == "completed"


def test_a_brief_collision_rolls_back_the_whole_commit(tmp_path):
    """A duplicate brief ID must not half-apply the session or the lease."""
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        first = run(store, scripted_session(market))
        stolen = first.brief

        # A second session that somehow derives the same brief ID.
        second = run(
            store,
            scripted_session(market),
        )
        assert second.skipped is True  # the lease guards it first

        # Force the collision directly against the store.
        running = first.session.model_copy(
            update={"session_id": "sts_01M0YT0HBMPYQRZG1FJ1HJEDKJ", "state": SessionState.RUNNING,
                    "brief_id": None, "cards": [], "challenges": []}
        )
        store.create_strategy_session(running)
        terminal = running.model_copy(update={"state": SessionState.COMPLETED,
                                              "brief_id": stolen.brief_id})
        with pytest.raises(SessionPersistenceError, match="already exists"):
            store.commit_strategy_session(terminal, stolen, NOW)

        # Nothing moved: one brief, and the second session is still running.
        assert len(store.briefs()) == 1
        assert store.get_strategy_session(running.session_id).state is SessionState.RUNNING


def test_a_retry_creates_a_unique_brief_for_its_new_session(tmp_path):
    market = build_synthetic_market(NOW)
    _, mirror, _, _ = market.claims
    bad = ScriptedInvoker(
        strategist={
            "cards": [
                draft(
                    market,
                    premises=[(market.claims[0].claim_id, 1), (market.claims[2].claim_id, 1)],
                )
            ]
        },
        challenger=[{"card_id": "unused", "verdict": "pass"}],
        brief_writer=brief_payload([mirror.claim_id]),
    )
    with seeded_store(tmp_path, market) as store:
        with pytest.raises(Exception):
            run(store, bad)
        failed = store.strategy_sessions()[0]

        retry = run(store, scripted_session(market))
        assert retry.session.session_id != failed.session_id
        assert retry.brief.brief_id.startswith("brf_2026w35-")
        assert retry.brief.strategy_session_id == retry.session.session_id
        assert len(store.briefs()) == 1


def test_a_concurrent_duplicate_trigger_finds_a_readable_running_session(tmp_path):
    """The session document exists before any agent runs, so a racing trigger
    always has something to return."""
    market = build_synthetic_market(NOW)
    seen: dict[str, object] = {}

    class ObservingInvoker(ScriptedInvoker):
        async def invoke(self, agent, request, *, run_id):
            # Mid-flight: a second trigger arrives while the first is running.
            session_id = run_id.split(":")[0]
            seen["lease"] = store.acquire_strategy_lease(
                PERIOD.from_, PERIOD.to, STRATEGY_VERSION, "sts_other", NOW, NOW
            )
            seen["readable"] = store.get_strategy_session(session_id)
            return await super().invoke(agent, request, run_id=run_id)

    with seeded_store(tmp_path, market) as store:
        script = scripted_session(market)
        run(store, ObservingInvoker(script.strategist, script.challenger, script.brief_writer))

    assert seen["lease"].state == "active"
    assert seen["readable"] is not None
    assert seen["readable"].state is SessionState.RUNNING


def test_commit_refuses_a_running_terminal_state(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        result = run(store, scripted_session(market))
        still_running = result.session.model_copy(
            update={"state": SessionState.RUNNING, "brief_id": None}
        )
        with pytest.raises(ValueError, match="terminal state"):
            store.commit_strategy_session(still_running, None, NOW)

        # And a session that already reached a terminal state is write-once.
        with pytest.raises(SessionPersistenceError, match="write-once"):
            store.commit_strategy_session(result.session, None, NOW)
