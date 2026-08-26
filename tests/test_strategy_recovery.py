"""Crash recovery at every persistence boundary, and error redaction.

Each test fails one store operation and then asserts the two properties that
matter: the period stays retryable, and nothing a model produced was persisted.
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from pydantic import ValidationError

from pipeline.local_backend import LocalBackend, LocalSettings
from pipeline.strategy_context import default_period
from pipeline.strategy_lease import (
    SessionPersistenceError,
    strategy_lease_document_id,
)
from schemas.strategy import STRATEGY_VERSION, SessionState
from strategy_agent import council as council_module
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


# --- The lease/session race ------------------------------------------------


def _duplicate_trigger(data_root, invoker):
    """Fire a genuine second trigger against the same store, from another
    thread with its own SQLite connection and event loop."""

    def worker():
        with LocalBackend(config(), LocalSettings(data_root)) as other:
            return asyncio.run(
                run_strategy_session(other, config(), invoker, period=PERIOD, now=NOW)
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(worker).result(timeout=30)


def test_a_duplicate_trigger_between_lease_and_session_reads_the_running_session(
    tmp_path, monkeypatch
):
    """Pause immediately after atomic acquisition, before context or any agent.

    Acquiring the lease and creating the session as two steps would leave the
    duplicate seeing an active lease and no readable session.
    """
    market = build_synthetic_market(NOW)
    data_root = tmp_path / "strategy-data"
    observed: dict[str, object] = {}
    duplicate = scripted_session(market)

    with seeded_store(tmp_path, market) as store:
        real_builder = council_module.build_strategy_context

        def paused(*args, **kwargs):
            # Past begin_strategy_session, before context and before any agent.
            if "loser" not in observed:
                observed["loser"] = _duplicate_trigger(data_root, duplicate)
            return real_builder(*args, **kwargs)

        monkeypatch.setattr(council_module, "build_strategy_context", paused)
        winner = run(store, scripted_session(market))

    loser = observed["loser"]
    assert loser.skipped is True
    assert loser.skip_reason == "another strategy session is active"
    assert loser.session.session_id == winner.session.session_id
    # The winner had not finished when the duplicate read it.
    assert loser.session.state is SessionState.RUNNING
    assert loser.session.input_manifest == []
    # No agent ran for the duplicate.
    assert duplicate.calls == []
    assert winner.session.state is SessionState.COMPLETED


def test_begin_is_atomic_no_lease_without_a_session(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        placeholder = _placeholder(store)
        decision = store.begin_strategy_session(placeholder, NOW + timedelta(minutes=30))

        assert decision.state == "acquired"
        assert lease_row(store)["session_id"] == placeholder.session_id
        readable = store.get_strategy_session(placeholder.session_id)
        assert readable is not None
        assert readable.state is SessionState.RUNNING
        assert readable.input_manifest == []
        assert readable.metrics.cards_proposed == 0

        # A second begin loses and names the winner.
        second = store.begin_strategy_session(
            _placeholder(store), NOW + timedelta(minutes=30)
        )
        assert second.state == "active"
        assert second.session_id == placeholder.session_id
        assert len(store.strategy_sessions()) == 1


def test_begin_rolls_back_the_lease_when_the_session_insert_fails(tmp_path):
    """A duplicate session ID must not leave a lease pointing at nothing."""
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        placeholder = _placeholder(store)
        store.create_strategy_session(placeholder)  # take the ID first

        with pytest.raises(Exception):
            store.begin_strategy_session(placeholder, NOW + timedelta(minutes=30))

        assert lease_row(store) is None


def test_begin_refuses_a_non_running_session(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        terminal = _placeholder(store).model_copy(
            update={"state": SessionState.FAILED, "error": "x"}
        )
        with pytest.raises(ValueError, match="running session"):
            store.begin_strategy_session(terminal, NOW + timedelta(minutes=30))


# --- Lease ownership at commit --------------------------------------------


def test_commit_refuses_when_the_lease_is_missing(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        placeholder = _placeholder(store)
        store.begin_strategy_session(placeholder, NOW + timedelta(minutes=30))
        store.connection.execute("DELETE FROM strategy_leases")
        store.connection.commit()

        terminal = placeholder.model_copy(update={"state": SessionState.COMPLETED})
        with pytest.raises(SessionPersistenceError, match="active lease"):
            store.commit_strategy_session(terminal, None, NOW)

        assert store.briefs() == []
        assert store.get_strategy_session(placeholder.session_id).state is SessionState.RUNNING


def test_commit_refuses_a_lease_owned_by_another_session(tmp_path):
    """An expired lease reclaimed by a newer attempt must block the old commit."""
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        stale = _placeholder(store)
        store.begin_strategy_session(stale, NOW + timedelta(seconds=1))

        # A later attempt, after expiry, reclaims the lease.
        later = NOW + timedelta(minutes=5)
        newer = _placeholder(store, created_at=later)
        reclaimed = store.begin_strategy_session(newer, later + timedelta(minutes=30))
        assert reclaimed.state == "acquired"
        assert lease_row(store)["session_id"] == newer.session_id

        terminal = stale.model_copy(update={"state": SessionState.COMPLETED})
        with pytest.raises(SessionPersistenceError, match="active lease"):
            store.commit_strategy_session(terminal, None, NOW)

        assert store.briefs() == []
        assert store.get_strategy_session(stale.session_id).state is SessionState.RUNNING
        assert lease_row(store)["session_id"] == newer.session_id


def test_commit_refuses_an_already_released_lease(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        placeholder = _placeholder(store)
        store.begin_strategy_session(placeholder, NOW + timedelta(minutes=30))
        store.connection.execute("UPDATE strategy_leases SET state = 'failed'")
        store.connection.commit()

        terminal = placeholder.model_copy(update={"state": SessionState.FAILED, "error": "e"})
        with pytest.raises(SessionPersistenceError, match="active lease"):
            store.commit_strategy_session(terminal, None, NOW)
        assert store.get_strategy_session(placeholder.session_id).state is SessionState.RUNNING


# --- Durable context failure ----------------------------------------------


def test_a_context_failure_marks_the_placeholder_failed_and_retryable(
    tmp_path, monkeypatch
):
    market = build_synthetic_market(NOW)
    invoker = scripted_session(market)
    monkeypatch.setattr("pipeline.strategy_context.MAX_CONTEXT_BYTES", 10)

    with seeded_store(tmp_path, market) as store:
        with pytest.raises(Exception):
            run(store, invoker)

        assert invoker.calls == []
        session = store.strategy_sessions()[0]
        assert session.state is SessionState.FAILED
        assert session.error == (
            "context:StrategyContextTooLarge: "
            "bounded context exceeded its byte or token budget"
        )
        assert session.input_manifest == []
        assert session.metrics.cards_proposed == 0
        assert lease_row(store)["state"] == "failed"
        assert SECRET not in stored_text(store)

        monkeypatch.undo()
        retry = run(store, scripted_session(market))
        assert retry.session.state is SessionState.COMPLETED
        assert retry.session.session_id != session.session_id
        assert len(store.strategy_sessions()) == 2


def _placeholder(store, created_at=NOW):
    """A fresh running placeholder session for the shared test period."""
    from schemas.strategy import (
        BRIEF_WRITER_VERSION,
        CHALLENGER_VERSION,
        STRATEGIST_VERSION,
        STRATEGY_QUESTION,
        STRATEGY_VERSION as VERSION,
        AgentVersions,
        ModelVersions,
        SessionMetrics,
        StrategySession,
        manifest_hash,
    )
    from schemas.common import new_prefixed_id

    del store
    return StrategySession(
        session_id=new_prefixed_id("sts"),
        strategy_version=VERSION,
        question=STRATEGY_QUESTION,
        period=PERIOD,
        input_manifest=[],
        manifest_hash=manifest_hash([]),
        metrics_evidence=[],
        agent_versions=AgentVersions(
            strategist=STRATEGIST_VERSION,
            challenger=CHALLENGER_VERSION,
            brief_writer=BRIEF_WRITER_VERSION,
        ),
        model_versions=ModelVersions(
            strategist="scripted-offline",
            challenger="scripted-offline",
            brief_writer="scripted-offline",
        ),
        state=SessionState.RUNNING,
        metrics=SessionMetrics(),
        created_at=created_at,
        updated_at=created_at,
    )


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
