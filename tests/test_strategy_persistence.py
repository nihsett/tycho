"""Leases, write-once sessions and briefs, retries, and local/cloud parity."""

import asyncio
import inspect
from datetime import timedelta

import pytest

from pipeline.cloud import CloudBackend
from pipeline.local_backend import LocalBackend
from pipeline.strategy_context import default_period
from pipeline.strategy_lease import (
    strategy_lease_document_id,
    strategy_lease_is_active,
)
from schemas.strategy import STRATEGY_VERSION, SessionState
from strategy_agent.citations import CitationError
from strategy_agent.council import StrategySessionStore, run_strategy_session
from strategy_agent.synthetic import (
    ScriptedInvoker,
    build_synthetic_market,
    scripted_session,
)
from tests.strategy_helpers import NOW, brief_payload, config, draft, seeded_store

PERIOD = default_period(NOW, 7)


def run(store, invoker, *, now=NOW):
    return asyncio.run(
        run_strategy_session(store, config(), invoker, period=PERIOD, now=now)
    )


def test_lease_identity_is_the_period_and_strategy_version():
    first = strategy_lease_document_id(PERIOD.from_, PERIOD.to, STRATEGY_VERSION)
    assert first == strategy_lease_document_id(PERIOD.from_, PERIOD.to, STRATEGY_VERSION)
    assert first != strategy_lease_document_id(PERIOD.from_, PERIOD.to, "strategy-council@2")
    assert first != strategy_lease_document_id(
        PERIOD.from_ - timedelta(days=1), PERIOD.to, STRATEGY_VERSION
    )
    assert strategy_lease_is_active(NOW + timedelta(minutes=1), NOW)
    assert not strategy_lease_is_active(NOW - timedelta(minutes=1), NOW)
    assert not strategy_lease_is_active(None, NOW)


def test_begin_creates_the_lease_and_a_readable_session_together(tmp_path):
    """The two writes a duplicate trigger depends on land in one transaction."""
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        before = len(store.strategy_sessions())
        result = run(store, scripted_session(market))
        assert len(store.strategy_sessions()) == before + 1
        assert store.get_strategy_session(result.session.session_id) is not None


def test_concurrent_trigger_sees_an_active_lease(tmp_path):
    with seeded_store(tmp_path, build_synthetic_market(NOW)) as store:
        first = store.acquire_strategy_lease(
            PERIOD.from_, PERIOD.to, STRATEGY_VERSION, "sts_a", NOW, NOW + timedelta(minutes=30)
        )
        second = store.acquire_strategy_lease(
            PERIOD.from_,
            PERIOD.to,
            STRATEGY_VERSION,
            "sts_b",
            NOW + timedelta(seconds=1),
            NOW + timedelta(minutes=30),
        )
        assert first.state == "acquired"
        assert second.state == "active"
        assert second.session_id == "sts_a"


def test_duplicate_trigger_returns_the_existing_session_without_a_model_call(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        first = run(store, scripted_session(market))
        assert first.skipped is False

        second_invoker = scripted_session(market)
        second = run(store, second_invoker)

        assert second.skipped is True
        assert second.skip_reason == "session already completed"
        assert second_invoker.calls == []
        assert second.session.session_id == first.session.session_id
        assert len(store.strategy_sessions()) == 1
        assert len(store.briefs()) == 1


def test_a_failed_session_stays_retryable(tmp_path):
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
        with pytest.raises(CitationError):
            run(store, bad)
        failed = store.strategy_sessions()[0]
        assert failed.state is SessionState.FAILED

        # The lease is failed, not completed, so the next trigger runs again.
        retry = run(store, scripted_session(market), now=NOW + timedelta(minutes=1))
        assert retry.skipped is False
        assert retry.session.state is SessionState.COMPLETED
        assert retry.session.session_id != failed.session_id
        assert len(store.strategy_sessions()) == 2


def test_an_expired_lease_is_reclaimed(tmp_path):
    with seeded_store(tmp_path, build_synthetic_market(NOW)) as store:
        store.acquire_strategy_lease(
            PERIOD.from_, PERIOD.to, STRATEGY_VERSION, "sts_a", NOW, NOW + timedelta(seconds=1)
        )
        later = store.acquire_strategy_lease(
            PERIOD.from_,
            PERIOD.to,
            STRATEGY_VERSION,
            "sts_b",
            NOW + timedelta(minutes=5),
            NOW + timedelta(minutes=35),
        )
        assert later.state == "acquired"
        assert later.attempt == 2


def test_sessions_and_briefs_are_write_once(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        result = run(store, scripted_session(market))
        session = result.session

        with pytest.raises(Exception):
            store.create_strategy_session(session)
        with pytest.raises(ValueError, match="write-once"):
            store.finalize_strategy_session(session)
        with pytest.raises(ValueError, match="terminal state"):
            store.finalize_strategy_session(
                session.model_copy(update={"state": SessionState.RUNNING, "brief_id": None})
            )
        assert store.create_brief_once(result.brief) is False
        assert len(store.briefs()) == 1


def test_session_and_brief_round_trip_through_the_store(tmp_path):
    market = build_synthetic_market(NOW)
    with seeded_store(tmp_path, market) as store:
        result = run(store, scripted_session(market))
        reread = store.get_strategy_session(result.session.session_id)
        assert reread == result.session
        assert store.get_brief(result.brief.brief_id) == result.brief
        assert store.stats()["strategy_sessions"] == 1
        assert store.stats()["briefs"] == 1


PARITY_METHODS = (
    "begin_strategy_session",
    "commit_strategy_session",
    "list_claims",
    "list_canonical_deltas",
    "get_claim",
    "get_delta",
    "acquire_strategy_lease",
    "complete_strategy_lease",
    "fail_strategy_lease",
    "create_strategy_session",
    "finalize_strategy_session",
    "get_strategy_session",
    "strategy_sessions",
    "create_brief_once",
    "get_brief",
    "briefs",
)


@pytest.mark.parametrize("name", PARITY_METHODS)
def test_local_and_cloud_strategy_surfaces_match(name):
    """The strategy store contract is identical in SQLite and Firestore."""
    local = getattr(LocalBackend, name)
    cloud = getattr(CloudBackend, name)
    local_params = list(inspect.signature(local).parameters)
    cloud_params = list(inspect.signature(cloud).parameters)
    assert local_params == cloud_params, f"{name} signature differs"


def test_both_backends_satisfy_the_strategy_session_protocol(tmp_path):
    for backend in (LocalBackend, CloudBackend):
        missing = [
            name
            for name in StrategySessionStore.__protocol_attrs__
            if not hasattr(backend, name)
        ]
        assert missing == [], f"{backend.__name__} is missing {missing}"

    with seeded_store(tmp_path, build_synthetic_market(NOW)) as store:
        assert all(
            callable(getattr(store, name, None))
            for name in StrategySessionStore.__protocol_attrs__
        )
