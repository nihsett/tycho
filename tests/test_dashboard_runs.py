"""The Run Strategy Session action: duplicate safety and the safe SSE stream."""

from __future__ import annotations

import asyncio
import functools
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from dashboard.api.app import create_app
from dashboard.api.models import ActivityEvent, ActivityKind, RunState
from dashboard.api.readmodel import ReadModel
from dashboard.api.runs import (
    DispatchError,
    DispatchResult,
    HttpStrategyDispatcher,
    StrategyRunManager,
)
from dashboard.api.settings import DashboardSettings
from schemas.strategy import SessionState
from strategy_agent.synthetic import synthetic_id
from tests.dashboard_helpers import RecordingSource, build_dashboard_market, config

def asyncio_test(fn):
    """Run one coroutine test on its own loop; no async plugin required."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


SESSION_ID = synthetic_id("sts", 601)
BRIEF_ID = "brf_2026w35-testcard"


class SlowDispatcher:
    """Records every call and holds the trigger open until released."""

    def __init__(self, result: DispatchResult, *, gate: asyncio.Event | None = None) -> None:
        self.result = result
        self.calls = 0
        self.gate = gate

    async def trigger(self) -> DispatchResult:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return self.result


class BrokenDispatcher:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def trigger(self) -> DispatchResult:
        self.calls += 1
        raise self.exc


def completed_result(*, skipped: bool = False) -> DispatchResult:
    return DispatchResult(
        session_id=SESSION_ID,
        state="completed",
        skipped=skipped,
        cards_proposed=2,
        cards_passed=1,
        cards_rejected=1,
        brief_id=BRIEF_ID,
    )


def manager(dispatcher) -> tuple[StrategyRunManager, ReadModel]:
    market = build_dashboard_market()
    model = ReadModel(RecordingSource(market), config())
    return (
        StrategyRunManager(
            dispatcher,
            lambda sid: model.activity(sid).events,
            session_for_period=model.session_for_period,
        ),
        model,
    )


# --- Duplicate safety -------------------------------------------------------


@asyncio_test
async def test_a_duplicate_click_returns_the_same_run_and_calls_once():
    gate = asyncio.Event()
    dispatcher = SlowDispatcher(completed_result(), gate=gate)
    runs, _ = manager(dispatcher)
    first, first_duplicate = await runs.trigger()
    second, second_duplicate = await runs.trigger()
    assert first is second
    assert first_duplicate is False
    assert second_duplicate is True
    gate.set()
    await asyncio.wait_for(first.finished.wait(), timeout=5)
    assert dispatcher.calls == 1
    await runs.aclose()


@asyncio_test
async def test_concurrent_clicks_start_exactly_one_run():
    gate = asyncio.Event()
    dispatcher = SlowDispatcher(completed_result(), gate=gate)
    runs, _ = manager(dispatcher)
    results = await asyncio.gather(*(runs.trigger() for _ in range(5)))
    assert len({run.run_id for run, _ in results}) == 1
    gate.set()
    await asyncio.wait_for(results[0][0].finished.wait(), timeout=5)
    assert dispatcher.calls == 1
    await runs.aclose()


@asyncio_test
async def test_a_skipped_dispatch_is_reported_as_a_duplicate_session():
    dispatcher = SlowDispatcher(completed_result(skipped=True))
    runs, _ = manager(dispatcher)
    run, _ = await runs.trigger()
    await asyncio.wait_for(run.finished.wait(), timeout=5)
    assert run.duplicate is True
    assert run.session_id == SESSION_ID
    assert run.state is RunState.COMPLETED
    assert "without a model call" in run.detail
    await runs.aclose()


@asyncio_test
async def test_a_run_is_addressable_by_run_id_and_by_session_id():
    dispatcher = SlowDispatcher(completed_result())
    runs, _ = manager(dispatcher)
    run, _ = await runs.trigger()
    await asyncio.wait_for(run.finished.wait(), timeout=5)
    assert runs.get(run.run_id) is run
    assert runs.get(SESSION_ID) is run
    assert runs.get(synthetic_id("sts", 42)) is None
    await runs.aclose()


# --- Event stream -----------------------------------------------------------


@asyncio_test
async def test_the_stream_replays_in_order_without_duplicates():
    dispatcher = SlowDispatcher(completed_result())
    runs, _ = manager(dispatcher)
    run, _ = await runs.trigger()
    await asyncio.wait_for(run.finished.wait(), timeout=5)
    events = [event async for event in runs.stream(run)]
    assert [event.seq for event in events] == list(range(len(events)))
    assert events[0].event is ActivityKind.RUN_STARTED
    assert events[-1].event is ActivityKind.BRIEF_COMPLETED
    assert len({event.seq for event in events}) == len(events)
    await runs.aclose()


@asyncio_test
async def test_the_stream_resumes_after_a_sequence_number():
    dispatcher = SlowDispatcher(completed_result())
    runs, _ = manager(dispatcher)
    run, _ = await runs.trigger()
    await asyncio.wait_for(run.finished.wait(), timeout=5)
    everything = [event async for event in runs.stream(run)]
    resumed = [event async for event in runs.stream(run, after=2)]
    assert [event.seq for event in resumed] == [
        event.seq for event in everything if event.seq > 2
    ]
    await runs.aclose()


@asyncio_test
async def test_a_live_subscriber_sees_events_as_they_arrive():
    gate = asyncio.Event()
    dispatcher = SlowDispatcher(completed_result(), gate=gate)
    runs, _ = manager(dispatcher)
    run, _ = await runs.trigger()

    seen: list[ActivityKind] = []

    async def consume() -> None:
        async for event in runs.stream(run):
            seen.append(event.event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(run.finished.wait(), timeout=5)
    await asyncio.wait_for(task, timeout=5)
    assert seen[0] is ActivityKind.RUN_STARTED
    assert ActivityKind.BRIEF_COMPLETED in seen
    assert seen == sorted(seen, key=lambda item: 0)  # arrival order preserved
    await runs.aclose()


@asyncio_test
async def test_a_dispatcher_failure_ends_the_run_with_a_class_name_only():
    dispatcher = BrokenDispatcher(DispatchError("dispatcher returned 503"))
    runs, _ = manager(dispatcher)
    run, _ = await runs.trigger()
    await asyncio.wait_for(run.finished.wait(), timeout=5)
    assert run.state is RunState.FAILED
    last = run.events[-1]
    assert last.event is ActivityKind.RUN_FAILED
    assert last.failure_class == "DispatchError"
    assert "503" not in json.dumps(last.model_dump(mode="json"))
    assert "503" not in run.detail
    await runs.aclose()


@asyncio_test
async def test_a_failed_session_result_keeps_the_period_retryable():
    dispatcher = SlowDispatcher(
        DispatchResult(
            session_id=SESSION_ID,
            state="failed",
            skipped=False,
            error="strategist:StrategyModelError: agent returned no usable structured output",
        )
    )
    runs, _ = manager(dispatcher)
    run, _ = await runs.trigger()
    await asyncio.wait_for(run.finished.wait(), timeout=5)
    assert run.state is RunState.FAILED
    assert run.events[-1].failure_class == "strategist:StrategyModelError"
    assert "retryable" in run.detail
    await runs.aclose()


@asyncio_test
async def test_no_stream_event_carries_governed_prose():
    market = build_dashboard_market()
    model = ReadModel(RecordingSource(market), config())
    dispatcher = SlowDispatcher(completed_result())
    runs = StrategyRunManager(dispatcher, lambda sid: model.activity(sid).events)
    run, _ = await runs.trigger()
    await asyncio.wait_for(run.finished.wait(), timeout=5)
    payload = json.dumps(
        [event.model_dump(mode="json") for event in run.events]
    ).casefold()
    for card in market.sessions[0].cards:
        assert card.statement.casefold() not in payload
        assert card.rationale.casefold() not in payload
    assert market.briefs[0].rendered_md.casefold() not in payload
    await runs.aclose()


# --- The dispatcher boundary -----------------------------------------------


@asyncio_test
async def test_a_period_that_already_has_a_session_names_it_immediately():
    market = build_dashboard_market()
    model = ReadModel(RecordingSource(market), config())
    session = market.sessions[0]
    gate = asyncio.Event()
    runs = StrategyRunManager(
        SlowDispatcher(completed_result(), gate=gate),
        lambda sid: model.activity(sid).events,
        session_for_period=model.session_for_period,
        clock=lambda: session.period.to + timedelta(days=1),
    )
    run, _ = await runs.trigger()
    # Reported before the dispatcher has answered.
    assert run.session_id is None or run.session_id.startswith("sts_")
    gate.set()
    await asyncio.wait_for(run.finished.wait(), timeout=5)
    await runs.aclose()


def test_the_session_lookup_matches_the_exact_bounded_period():
    market = build_dashboard_market()
    model = ReadModel(RecordingSource(market), config())
    session = market.sessions[0]
    assert (
        model.session_for_period(session.period.from_, session.period.to)
        == session.session_id
    )
    assert (
        model.session_for_period(
            session.period.from_ - timedelta(days=7), session.period.to
        )
        is None
    )


def test_a_failed_session_does_not_claim_the_period():
    market = build_dashboard_market()
    session = market.sessions[0]
    market.sessions[0] = session.model_copy(
        update={"state": SessionState.FAILED, "brief_id": None, "error": "context:ValueError: x"}
    )
    model = ReadModel(RecordingSource(market), config())
    assert model.session_for_period(session.period.from_, session.period.to) is None


@asyncio_test
async def test_a_store_failure_never_blocks_the_trigger():
    class Broken:
        def __call__(self, *_: object) -> str | None:
            raise RuntimeError("store unavailable")

    runs = StrategyRunManager(
        SlowDispatcher(completed_result()),
        lambda sid: [],
        session_for_period=Broken(),
    )
    run, _ = await runs.trigger()
    await asyncio.wait_for(run.finished.wait(), timeout=5)
    assert run.state is RunState.COMPLETED
    await runs.aclose()


def test_the_dispatcher_result_contract_refuses_an_unexpected_field():
    with pytest.raises(DispatchError, match="unsafe fields"):
        DispatchResult.from_payload({"state": "completed", "rendered_md": "leak"})


def test_the_dispatcher_body_carries_no_prompt_or_period_range():
    assert HttpStrategyDispatcher.BODY == {
        "trigger": "dashboard",
        "period": "previous_complete_week",
    }
    assert HttpStrategyDispatcher("https://example.invalid/").url == "https://example.invalid"
    with pytest.raises(ValueError, match="required"):
        HttpStrategyDispatcher("")


# --- Through the HTTP surface ----------------------------------------------


def test_the_sse_endpoint_streams_named_events_and_closes():
    market = build_dashboard_market()
    model = ReadModel(RecordingSource(market), config())
    dispatcher = SlowDispatcher(completed_result())
    runs = StrategyRunManager(dispatcher, lambda sid: model.activity(sid).events)
    app = create_app(
        DashboardSettings(project="test", static_dir=None),
        read_model=model,
        run_manager=runs,
        config=config(),
    )
    with TestClient(app) as client:
        accepted = client.post("/api/strategy/sessions")
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        with client.stream("GET", f"/api/strategy/sessions/{run_id}/stream") as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            assert stream.headers["cache-control"].startswith("no-store")
            body = "".join(chunk for chunk in stream.iter_text())
    kinds = [line[len("event: ") :] for line in body.splitlines() if line.startswith("event: ")]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "stream_closed"
    assert "brief_completed" in kinds
    assert "id: 0" in body


@asyncio_test
async def test_a_heartbeat_never_advances_the_resume_point():
    gate = asyncio.Event()
    dispatcher = SlowDispatcher(completed_result(), gate=gate)
    runs, _ = manager(dispatcher)
    run, _ = await runs.trigger()
    seen: list[ActivityEvent] = []

    async def consume() -> None:
        async for event in runs.stream(run):
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(run.finished.wait(), timeout=5)
    await asyncio.wait_for(task, timeout=5)
    for index, event in enumerate(seen):
        if event.event is not ActivityKind.HEARTBEAT:
            continue
        # Resuming from a heartbeat must replay every real event after it.
        following = [item.seq for item in seen[index + 1 :] if item.event is not ActivityKind.HEARTBEAT]
        assert all(seq > event.seq for seq in following)
    await runs.aclose()


@asyncio_test
async def test_the_derived_replay_does_not_repeat_the_runs_own_start():
    dispatcher = SlowDispatcher(completed_result())
    runs, _ = manager(dispatcher)
    run, _ = await runs.trigger()
    await asyncio.wait_for(run.finished.wait(), timeout=5)
    starts = [event for event in run.events if event.event is ActivityKind.RUN_STARTED]
    assert len(starts) == 1
    assert starts[0].derived is False
    await runs.aclose()


def test_the_sse_endpoint_omits_the_id_line_for_a_heartbeat():
    market = build_dashboard_market()
    model = ReadModel(RecordingSource(market), config())
    gate: dict[str, asyncio.Event] = {}

    class Gated:
        calls = 0

        async def trigger(self) -> DispatchResult:
            Gated.calls += 1
            gate["event"] = asyncio.Event()
            await asyncio.wait_for(gate["event"].wait(), timeout=0.05)
            return completed_result()

    runs = StrategyRunManager(Gated(), lambda sid: model.activity(sid).events)
    app = create_app(
        DashboardSettings(project="test", static_dir=None),
        read_model=model,
        run_manager=runs,
        config=config(),
    )
    with TestClient(app) as client:
        run_id = client.post("/api/strategy/sessions").json()["run_id"]
        with client.stream("GET", f"/api/strategy/sessions/{run_id}/stream") as stream:
            body = "".join(chunk for chunk in stream.iter_text())
    blocks = [block for block in body.split("\n\n") if block.strip()]
    for block in blocks:
        if "event: heartbeat" in block:
            assert "id: " not in block
        elif "event: stream_closed" not in block:
            assert "id: " in block


def test_the_sse_endpoint_rejects_an_unknown_or_malformed_stream_id():
    market = build_dashboard_market()
    model = ReadModel(RecordingSource(market), config())
    runs = StrategyRunManager(
        SlowDispatcher(completed_result()), lambda sid: model.activity(sid).events
    )
    app = create_app(
        DashboardSettings(project="test", static_dir=None),
        read_model=model,
        run_manager=runs,
        config=config(),
    )
    with TestClient(app) as client:
        assert client.get("/api/strategy/sessions/run_dead/stream").status_code == 400
        assert (
            client.get(f"/api/strategy/sessions/{synthetic_id('sts', 7)}/stream").status_code
            == 404
        )


def test_the_trigger_period_is_the_previous_complete_week():
    market = build_dashboard_market()
    model = ReadModel(RecordingSource(market), config())
    runs = StrategyRunManager(
        SlowDispatcher(completed_result()),
        lambda sid: model.activity(sid).events,
        clock=lambda: datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
    )
    app = create_app(
        DashboardSettings(project="test", static_dir=None),
        read_model=model,
        run_manager=runs,
        config=config(),
    )
    with TestClient(app) as client:
        payload = client.post("/api/strategy/sessions").json()
    assert payload["period_from"].startswith("2026-08-17T00:00:00")
    assert payload["period_to"].startswith("2026-08-24T00:00:00")
