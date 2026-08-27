"""Duplicate-safe Run Strategy Session action and its safe event stream.

The dashboard cannot start an arbitrary agent.  The button POSTs a fixed
trigger to the existing private Strategy dispatcher, which derives the bounded
period from its own clock and normalizes it through the same ``StrategyRequest``
the weekly Cloud Scheduler job uses.  No prompt text, model name, scope, or
evidence policy can be changed from here, because there is no field for one.

Duplicate protection has two independent layers:

1. this process refuses to start a second run for the same bounded period while
   one is in flight, and returns the first run;
2. the dispatcher and the Strategy Runtime share one transactional
   ``(period_from, period_to, strategy_version)`` lease, so even a racing second
   trigger returns the existing session with ``skipped=true`` and zero model
   calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pipeline.strategy_dispatcher import previous_complete_week

from dashboard.api.models import ActivityEvent, ActivityKind, RunState

#: What the dispatcher is allowed to hand back.  Anything else is refused
#: rather than logged or forwarded to the browser.
SAFE_DISPATCHER_FIELDS = frozenset(
    {
        "service",
        "request_id",
        "trigger",
        "period_from",
        "period_to",
        "strategy_version",
        "runtime",
        "session_id",
        "state",
        "cards_proposed",
        "cards_passed",
        "cards_rejected",
        "brief_id",
        "skipped",
        "error",
    }
)

HEARTBEAT_SECONDS = 10.0
MAX_RUN_EVENTS = 64
MAX_TRACKED_RUNS = 24
COUNCIL_AGENT = "tycho_strategy_council"


class DispatchError(RuntimeError):
    """The Strategy dispatcher could not be reached or refused the trigger."""


@dataclass(frozen=True)
class DispatchResult:
    """The bounded result of one dispatcher call."""

    session_id: str | None
    state: str
    skipped: bool
    cards_proposed: int = 0
    cards_passed: int = 0
    cards_rejected: int = 0
    brief_id: str | None = None
    error: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DispatchResult":
        unknown = set(payload) - SAFE_DISPATCHER_FIELDS
        if unknown:
            raise DispatchError(f"dispatcher result carries unsafe fields: {sorted(unknown)}")
        state = str(payload.get("state") or "unknown")
        return cls(
            session_id=payload.get("session_id"),
            state=state,
            skipped=bool(payload.get("skipped", False)),
            cards_proposed=int(payload.get("cards_proposed") or 0),
            cards_passed=int(payload.get("cards_passed") or 0),
            cards_rejected=int(payload.get("cards_rejected") or 0),
            brief_id=payload.get("brief_id"),
            error=str(payload["error"])[:200] if payload.get("error") else None,
        )


class StrategyDispatcher(Protocol):
    async def trigger(self) -> DispatchResult: ...


def _identity_token(audience: str) -> str:
    """Mint an OIDC token for the private dispatcher from the runtime identity.

    On Cloud Run this is the metadata server.  The environment override exists
    for local verification runs only; there is no credential in the browser and
    none in the served bundle.
    """
    override = os.getenv("TYCHO_DASHBOARD_ID_TOKEN")
    if override:
        return override
    import httpx

    try:
        response = httpx.get(
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            "service-accounts/default/identity",
            params={"audience": audience},
            headers={"Metadata-Flavor": "Google"},
            timeout=10.0,
        )
        response.raise_for_status()
        return response.text.strip()
    except Exception:
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        return id_token.fetch_id_token(Request(), audience)


class HttpStrategyDispatcher:
    """Authenticated client for the existing private Strategy dispatcher."""

    #: The complete body.  ``period`` is a *name*; the dispatcher resolves the
    #: week itself, so this caller cannot widen the window.
    BODY = {"trigger": "dashboard", "period": "previous_complete_week"}

    def __init__(self, url: str, *, timeout_seconds: float = 870.0) -> None:
        if not url:
            raise ValueError("TYCHO_STRATEGY_DISPATCHER_URL is required")
        self.url = url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def trigger(self) -> DispatchResult:
        import httpx

        token = await asyncio.to_thread(_identity_token, self.url)
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.url,
                json=self.BODY,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        if response.status_code >= 500:
            raise DispatchError(f"dispatcher returned {response.status_code}")
        if response.status_code >= 400:
            raise DispatchError(f"dispatcher refused the trigger ({response.status_code})")
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise DispatchError("dispatcher returned a non-JSON result") from exc
        if not isinstance(payload, dict):
            raise DispatchError("dispatcher returned a non-object result")
        return DispatchResult.from_payload(payload)


@dataclass
class RunRecord:
    """One dashboard-initiated strategy run, tracked in this process."""

    run_id: str
    period_from: datetime
    period_to: datetime
    state: RunState = RunState.DISPATCHING
    session_id: str | None = None
    brief_id: str | None = None
    detail: str = "Trigger accepted; the Strategy dispatcher is deriving the period."
    duplicate: bool = False
    events: list[ActivityEvent] = field(default_factory=list)
    subscribers: set[asyncio.Queue[ActivityEvent]] = field(default_factory=set)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def terminal(self) -> bool:
        return self.state in {RunState.COMPLETED, RunState.FAILED}

    def period_key(self) -> tuple[datetime, datetime]:
        return (self.period_from, self.period_to)


class StrategyRunManager:
    """Starts at most one strategy run per bounded period and fans out events."""

    def __init__(
        self,
        dispatcher: StrategyDispatcher,
        activity_for: Callable[[str], list[ActivityEvent]],
        *,
        session_for_period: Callable[[datetime, datetime], str | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._activity_for = activity_for
        self._session_for_period = session_for_period
        self._clock = clock or (lambda: datetime.now(UTC))
        self._runs: dict[str, RunRecord] = {}
        self._by_session: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    # --- Lookup -------------------------------------------------------------

    def get(self, identifier: str) -> RunRecord | None:
        if identifier in self._runs:
            return self._runs[identifier]
        run_id = self._by_session.get(identifier)
        return self._runs.get(run_id) if run_id else None

    def active_for_period(self, period: tuple[datetime, datetime]) -> RunRecord | None:
        for run in self._runs.values():
            if run.period_key() == period and not run.terminal:
                return run
        return None

    # --- Trigger ------------------------------------------------------------

    async def trigger(self) -> tuple[RunRecord, bool]:
        """Start one run, or return the in-flight run for the same period."""
        period_from, period_to = previous_complete_week(self._clock())
        async with self._lock:
            existing = self.active_for_period((period_from, period_to))
            if existing is not None:
                existing.duplicate = True
                return existing, True
            run = RunRecord(
                run_id=f"run_{secrets.token_hex(8)}",
                period_from=period_from,
                period_to=period_to,
            )
            # The Runtime creates the session, so a brand-new period has no ID
            # to report yet.  A period that already has one - which is the
            # common case inside a week the council has run - is named
            # immediately, because that is the session the lease will return.
            run.session_id = self._existing_session(period_from, period_to)
            if run.session_id:
                run.detail = (
                    "This period already has a session; the lease will return it."
                )
            self._runs[run.run_id] = run
            self._evict()
            self._emit(
                run,
                ActivityKind.RUN_STARTED,
                agent=COUNCIL_AGENT,
                state="dispatching",
                derived=False,
            )
            task = asyncio.create_task(self._execute(run))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return run, False

    def _existing_session(self, period_from: datetime, period_to: datetime) -> str | None:
        if self._session_for_period is None:
            return None
        try:
            return self._session_for_period(period_from, period_to)
        except Exception:
            # A read failure here must not block the trigger; the dispatcher
            # remains the authority on which session this period has.
            return None

    def _evict(self) -> None:
        if len(self._runs) <= MAX_TRACKED_RUNS:
            return
        finished = [run for run in self._runs.values() if run.terminal and not run.subscribers]
        finished.sort(key=lambda item: item.events[0].at if item.events else self._clock())
        for run in finished[: len(self._runs) - MAX_TRACKED_RUNS]:
            self._runs.pop(run.run_id, None)
            if run.session_id:
                self._by_session.pop(run.session_id, None)

    async def _execute(self, run: RunRecord) -> None:
        try:
            result = await self._dispatcher.trigger()
        except Exception as exc:
            run.state = RunState.FAILED
            # Only the class name: a dispatcher exception may quote a response.
            run.detail = "The Strategy dispatcher could not complete this trigger."
            self._emit(
                run,
                ActivityKind.RUN_FAILED,
                agent=COUNCIL_AGENT,
                state="failed",
                failure_class=type(exc).__name__[:120],
                derived=False,
            )
            run.finished.set()
            return

        run.session_id = result.session_id
        run.brief_id = result.brief_id
        run.duplicate = run.duplicate or result.skipped
        if result.session_id:
            self._by_session[result.session_id] = run.run_id
            for event in self._activity_for(result.session_id):
                # This run already emitted its own run_started when the trigger
                # was accepted; the derived replay must not repeat it.
                if event.event is not ActivityKind.RUN_STARTED:
                    self._append(run, event)
        if result.state == "completed":
            run.state = RunState.COMPLETED
            run.detail = (
                "This period already had a session; the lease returned it "
                "without a model call."
                if result.skipped
                else "The strategy session completed."
            )
            self._emit(
                run,
                ActivityKind.BRIEF_COMPLETED,
                agent=COUNCIL_AGENT,
                state="skipped" if result.skipped else "completed",
                brief_id=result.brief_id,
                card_count=result.cards_proposed,
                passed_count=result.cards_passed,
                rejected_count=result.cards_rejected,
                derived=False,
            )
        else:
            run.state = RunState.FAILED
            run.detail = "The strategy session failed; the period stays retryable."
            self._emit(
                run,
                ActivityKind.RUN_FAILED,
                agent=COUNCIL_AGENT,
                state=result.state,
                failure_class=(result.error or "session_failed").split(": ", 1)[0][:120],
                derived=False,
            )
        run.finished.set()

    # --- Events -------------------------------------------------------------

    def _emit(self, run: RunRecord, kind: ActivityKind, **fields: Any) -> None:
        event = ActivityEvent(
            seq=len(run.events),
            event=kind,
            at=self._clock(),
            run_id=run.run_id,
            session_id=run.session_id,
            **fields,
        )
        self._append(run, event)

    def _append(self, run: RunRecord, event: ActivityEvent) -> None:
        if len(run.events) >= MAX_RUN_EVENTS:
            return
        pinned = event.model_copy(update={"seq": len(run.events), "run_id": run.run_id})
        run.events.append(pinned)
        for queue in list(run.subscribers):
            queue.put_nowait(pinned)

    async def stream(self, run: RunRecord, *, after: int = -1) -> AsyncIterator[ActivityEvent]:
        """Replay from ``after``, then follow live until the run is terminal."""
        queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
        run.subscribers.add(queue)
        try:
            for event in list(run.events):
                if event.seq > after:
                    yield event
            while not (run.terminal and queue.empty()):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    if run.terminal:
                        break
                    # A heartbeat carries the LAST delivered sequence number, and
                    # the SSE layer omits its id line. If it advanced the resume
                    # point, a reconnect right after one would silently skip the
                    # next real event.
                    yield ActivityEvent(
                        seq=max(len(run.events) - 1, 0),
                        event=ActivityKind.HEARTBEAT,
                        at=self._clock(),
                        run_id=run.run_id,
                        session_id=run.session_id,
                        state=run.state.value,
                        derived=False,
                    )
                    continue
                if event.seq > after:
                    yield event
        finally:
            run.subscribers.discard(queue)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown path
                pass
