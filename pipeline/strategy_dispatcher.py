"""Authenticated HTTP dispatcher for Tycho's Strategy Council Agent Runtime.

This is a second, entirely separate dispatcher.  The analyst dispatcher in
``pipeline/dispatcher.py`` consumes Pub/Sub Delta deliveries; nothing here
touches it, its subscription, its service account, or its Runtime.

What this service accepts is deliberately tiny.  A trusted caller - the weekly
Cloud Scheduler job, or the authenticated dashboard "Run Strategy Session"
action - sends a trigger naming *which* bounded period to run.  It never sends
the period itself: the dispatcher derives it from its own clock, so one static
Scheduler body yields a different, deterministic week every Monday, and two
triggers landing in the same week normalize to the same
``(period_from, period_to, strategy_version)`` lease identity.

There is therefore no field in which a caller could smuggle prompt text, a
question, a claim, or an instruction - and no field in which the Runtime could
return one.  Everything crossing this boundary in either direction is an ID, a
state, a count, or a flag.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from schemas.strategy import STRATEGY_VERSION, SessionState, StrategyTrigger
from strategy_agent.request import (
    ParsedStrategyRequest,
    StrategyRequestError,
    parse_strategy_request,
)

SERVICE_NAME = "tycho-strategy-dispatcher"
MAX_TRIGGER_BODY_BYTES = 4_096
DEFAULT_TIMEOUT_SECONDS = 840
WEEK_DAYS = 7

#: The only period a v1 trigger may name.  It is a *name*, not a date range:
#: the dispatcher resolves it, so the caller cannot widen the window.
PeriodSelector = Literal["previous_complete_week"]
DEFAULT_PERIOD_SELECTOR: PeriodSelector = "previous_complete_week"

#: The bounded result shape the Runtime is allowed to hand back.
SAFE_RESULT_FIELDS = frozenset(
    {
        "session_id",
        "strategy_version",
        "state",
        "cards_proposed",
        "cards_passed",
        "cards_rejected",
        "brief_id",
        "skipped",
        "error",
    }
)

#: The bounded structural line this service prints.  A log record carries no
#: card statement, claim text, evidence quote, or brief prose by construction.
SAFE_LOG_FIELDS = frozenset(
    {
        "service",
        "request_id",
        "trigger",
        "period_from",
        "period_to",
        "strategy_version",
        "runtime",
        *SAFE_RESULT_FIELDS,
    }
)

_SessionId = Annotated[str, StringConstraints(pattern=r"^sts_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
_BriefId = Annotated[str, StringConstraints(pattern=r"^brf_[A-Za-z0-9._:-]{1,64}$")]
_Version = Annotated[str, StringConstraints(min_length=1, max_length=64)]
#: Runtime failures arrive already sanitized by ``strategy_agent.errors`` as
#: ``stage:ExceptionClass: curated reason``.  Bound it again here so a future
#: change upstream still cannot hand this service an unbounded string.
_SafeError = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class StrategyDispatcherError(ValueError):
    """The request is not a valid bounded strategy trigger."""


class StrategyRuntimeError(RuntimeError):
    """Agent Runtime returned no usable bounded result."""


class StrategyTriggerRequest(BaseModel):
    """The complete contract this dispatcher accepts from a trusted caller."""

    model_config = ConfigDict(extra="forbid")

    trigger: StrategyTrigger
    period: PeriodSelector = DEFAULT_PERIOD_SELECTOR
    strategy_version: _Version = STRATEGY_VERSION


class StrategyRuntimeResult(BaseModel):
    """The complete contract this dispatcher accepts back from the Runtime.

    ``extra="forbid"`` is the point: if the Runtime ever grew a content-bearing
    field, this service would refuse the response rather than log or return it.
    """

    model_config = ConfigDict(extra="forbid")

    state: SessionState
    session_id: _SessionId | None = None
    strategy_version: _Version | None = None
    cards_proposed: int = Field(default=0, ge=0)
    cards_passed: int = Field(default=0, ge=0)
    cards_rejected: int = Field(default=0, ge=0)
    brief_id: _BriefId | None = None
    skipped: bool = False
    error: _SafeError | None = None

    def as_bounded_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        unknown = set(payload) - SAFE_RESULT_FIELDS
        if unknown:
            raise StrategyRuntimeError(f"runtime result carries unsafe fields: {sorted(unknown)}")
        return payload


def previous_complete_week(now: datetime) -> tuple[datetime, datetime]:
    """The last calendar week that had entirely finished before ``now``.

    Weeks run Monday 00:00 UTC to the following Monday 00:00 UTC, so the
    ``0 6 * * 1`` Scheduler trigger reports on the week that ended six hours
    earlier, and every trigger inside one calendar week resolves to exactly the
    same bounded period.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise StrategyDispatcherError("the trigger clock must be timezone-aware")
    utc_now = now.astimezone(UTC)
    midnight = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
    this_week_start = midnight - timedelta(days=utc_now.weekday())
    return this_week_start - timedelta(days=WEEK_DAYS), this_week_start


def weekly_request_id(trigger: str, period_from: datetime) -> str:
    """A stable request ID for one (trigger, week) pair; never a random value."""
    year, week, _ = period_from.isocalendar()
    return f"{trigger}-{year:04d}w{week:02d}"


def parse_trigger(body: bytes | str | dict[str, Any]) -> StrategyTriggerRequest:
    """Parse the trigger envelope and refuse anything it does not name."""
    if isinstance(body, (bytes, bytearray)) and len(body) > MAX_TRIGGER_BODY_BYTES:
        raise StrategyDispatcherError("trigger body is too large")
    if isinstance(body, (bytes, bytearray, str)):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise StrategyDispatcherError("strategy trigger is not valid JSON") from exc
    else:
        payload = body
    if not isinstance(payload, dict):
        raise StrategyDispatcherError("strategy trigger must be a JSON object")
    unknown = set(payload) - set(StrategyTriggerRequest.model_fields)
    if unknown:
        # Named explicitly so "prompt", "question", "instructions", or a
        # hand-written period can never be accepted and silently ignored.
        raise StrategyDispatcherError(f"strategy trigger rejects fields: {sorted(unknown)}")
    try:
        return StrategyTriggerRequest.model_validate(payload)
    except Exception as exc:
        raise StrategyDispatcherError(f"invalid strategy trigger: {type(exc).__name__}") from exc


def normalize_trigger(
    body: bytes | str | dict[str, Any], *, now: datetime | None = None
) -> ParsedStrategyRequest:
    """Turn a trusted trigger into the existing bounded ``StrategyRequest``.

    Normalizing *through* ``parse_strategy_request`` is deliberate: this service
    is structurally incapable of sending the Runtime anything the Runtime's own
    contract would not already accept.
    """
    trigger = parse_trigger(body)
    period_from, period_to = previous_complete_week(now or datetime.now(UTC))
    try:
        return parse_strategy_request(
            {
                "request_id": weekly_request_id(trigger.trigger, period_from),
                "trigger": trigger.trigger,
                "period_from": period_from.isoformat(),
                "period_to": period_to.isoformat(),
                "strategy_version": trigger.strategy_version,
            }
        )
    except StrategyRequestError as exc:
        raise StrategyDispatcherError(str(exc)) from exc


def runtime_message(parsed: ParsedStrategyRequest) -> str:
    """Serialize the only payload that ever leaves for Agent Runtime."""
    return json.dumps(
        parsed.request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def extract_strategy_result(events: list[dict[str, Any]]) -> StrategyRuntimeResult:
    """Pull the bounded JSON result out of the final Runtime event."""
    for event in reversed(events):
        for candidate in _result_candidates(event):
            try:
                return StrategyRuntimeResult.model_validate(candidate)
            except Exception:
                continue
    raise StrategyRuntimeError("Agent Runtime returned no bounded strategy result")


def _result_candidates(event: dict[str, Any]) -> list[Any]:
    candidates: list[Any] = []
    output = event.get("output")
    if isinstance(output, dict):
        candidates.append(output)
    content = event.get("content")
    if isinstance(content, dict):
        for part in reversed(content.get("parts") or []):
            text = part.get("text") if isinstance(part, dict) else None
            if not text:
                continue
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                candidates.append(decoded)
    return candidates


def require_authenticated_request(headers: Any) -> None:
    """Cloud Run IAM is the boundary; this is defense in depth behind it.

    Both callers are OIDC identities (the Scheduler job and the dashboard), so a
    request with no bearer token never legitimately reaches this handler.
    """
    authorization = headers.get("Authorization", "") or headers.get(
        "X-Serverless-Authorization", ""
    )
    if not authorization.startswith("Bearer "):
        raise StrategyDispatcherError("an authenticated strategy trigger is required")


def bounded_log_record(
    parsed: ParsedStrategyRequest, runtime: str, result: StrategyRuntimeResult
) -> dict[str, Any]:
    """The only thing this service prints: IDs, state, counts, and flags."""
    record = {
        "service": SERVICE_NAME,
        "request_id": parsed.request.request_id,
        "trigger": parsed.request.trigger,
        "period_from": parsed.request.period_from.isoformat(),
        "period_to": parsed.request.period_to.isoformat(),
        "strategy_version": parsed.request.strategy_version,
        "runtime": runtime,
        **result.as_bounded_dict(),
    }
    unknown = set(record) - SAFE_LOG_FIELDS
    if unknown:
        raise StrategyRuntimeError(f"strategy log record carries unsafe fields: {sorted(unknown)}")
    return record


class StrategyRuntimeInvoker:
    """Client boundary that sends only a normalized bounded request."""

    def __init__(
        self,
        resource_name: str,
        *,
        project: str,
        location: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not resource_name:
            raise ValueError("TYCHO_STRATEGY_RUNTIME_RESOURCE is required")
        import agentplatform
        from google.genai import types

        self.resource_name = resource_name
        self.timeout_seconds = timeout_seconds
        self.client = agentplatform.Client(
            project=project,
            location=location,
            http_options=types.HttpOptions(api_version="v1beta1"),
        )
        self.remote = self.client.agent_engines.get(name=resource_name)

    async def _collect(self, parsed: ParsedStrategyRequest) -> StrategyRuntimeResult:
        events: list[dict[str, Any]] = []
        async for event in self.remote.async_stream_query(
            user_id="tycho-strategy-dispatcher",
            message=runtime_message(parsed),
        ):
            events.append(event)
        return extract_strategy_result(events)

    async def invoke(self, parsed: ParsedStrategyRequest) -> StrategyRuntimeResult:
        return await asyncio.wait_for(self._collect(parsed), timeout=self.timeout_seconds)


class StrategyDispatcherHandler(BaseHTTPRequestHandler):
    invoker: StrategyRuntimeInvoker

    def do_GET(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self) -> None:
        try:
            require_authenticated_request(self.headers)
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_TRIGGER_BODY_BYTES:
                raise StrategyDispatcherError("invalid trigger size")
            parsed = normalize_trigger(self.rfile.read(length))
            result = asyncio.run(self.invoker.invoke(parsed))
            record = bounded_log_record(parsed, self.invoker.resource_name, result)
            print(json.dumps(record, sort_keys=True))
        except StrategyDispatcherError as exc:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            self.wfile.write(str(exc).encode("utf-8"))
            return
        except asyncio.TimeoutError:
            print(f"{SERVICE_NAME}: Agent Runtime timed out; the period stays retryable")
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.end_headers()
            return
        except Exception as exc:
            # Never echo the exception: it may carry model output or claim text.
            print(f"{SERVICE_NAME}: runtime invocation failed: {type(exc).__name__}")
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.end_headers()
            return

        if result.state is SessionState.FAILED:
            # A durable failed session is recorded and the period stays
            # retryable; report it so Scheduler surfaces the attempt.
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.end_headers()
            return
        body = json.dumps(record, sort_keys=True).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{SERVICE_NAME}: {format % args}")


def main() -> None:
    project = os.getenv("TYCHO_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("TYCHO_PROJECT or GOOGLE_CLOUD_PROJECT is required")
    location = os.getenv("TYCHO_RUNTIME_LOCATION", "us-central1")
    timeout = float(os.getenv("TYCHO_DISPATCHER_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    StrategyDispatcherHandler.invoker = StrategyRuntimeInvoker(
        os.getenv("TYCHO_STRATEGY_RUNTIME_RESOURCE", ""),
        project=project,
        location=location,
        timeout_seconds=timeout,
    )
    port = int(os.getenv("PORT", "8080"))
    print(
        json.dumps(
            {
                "service": SERVICE_NAME,
                "port": port,
                "runtime": StrategyDispatcherHandler.invoker.resource_name,
                "timeout_seconds": timeout,
            },
            sort_keys=True,
        )
    )
    ThreadingHTTPServer(("0.0.0.0", port), StrategyDispatcherHandler).serve_forever()


if __name__ == "__main__":
    main()
