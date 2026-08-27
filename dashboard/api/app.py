"""The Tycho Intelligence Dashboard API.

Route handlers are thin: they validate enums and bounds, call one named read
model method, and return a strict response model.  Every database query lives in
:mod:`dashboard.api.readmodel`.

Security posture, in one place:

- the browser receives no Google credential and never talks to Firestore,
  BigQuery, Agent Runtime, or Pub/Sub; this service uses its own Cloud Run
  service account;
- there is no CORS middleware, so the API is same-origin only, and a
  cross-origin ``Origin`` header on a write is refused outright;
- request bodies are bounded before they are parsed;
- responses carry a strict CSP and the usual hardening headers;
- logs carry route template, method, status, latency, and validated IDs -
  never a query string, a request body, or a response body.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, TypeVar

from fastapi import FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from schemas.config import TychoConfig, load_config

from dashboard.api.models import (
    ActivityKind,
    ActivityResponse,
    ErrorResponse,
    HealthResponse,
    OverviewResponse,
    ProvenanceResponse,
    RunState,
    StrategySessionResponse,
    TimelineResponse,
    TriggerResponse,
)
from dashboard.api.readmodel import MAX_TIMELINE_LIMIT, ReadModel, UnknownResource
from dashboard.api.runs import HttpStrategyDispatcher, StrategyRunManager
from dashboard.api.settings import MAX_REQUEST_BYTES, DashboardSettings

T = TypeVar("T")

CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "manifest-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}

CLAIM_ID = r"^clm_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
SESSION_ID = r"^sts_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
STREAM_ID = r"^(?:sts_[0-7][0-9A-HJKMNP-TV-Z]{25}|run_[0-9a-f]{16})$"
ENTITY_KEY = r"^[a-z][a-z0-9_]{0,40}$"
SCOPE_KEY = r"^[a-z][a-z0-9/_]{0,40}$"


class MetaResponse(BaseModel):
    """Closed enums the UI filters by, served from tycho.yaml."""

    model_config = ConfigDict(extra="forbid")

    entities: list[Annotated[str, StringConstraints(max_length=40)]] = Field(max_length=4)
    scopes: list[Annotated[str, StringConstraints(max_length=40)]] = Field(max_length=16)
    service: Annotated[str, StringConstraints(max_length=64)]
    revision: Annotated[str, StringConstraints(max_length=120)]


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class TtlCache:
    """A bounded cache for overview and health only.

    An in-progress strategy event stream is never cached, and a session is never
    cached as completed: those paths always read the store.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, _CacheEntry] = {}

    def get_or_call(self, key: str, factory: Callable[[], T]) -> T:
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is not None and entry.expires_at > now:
            return entry.value
        value = factory()
        if len(self._entries) > 16:
            self._entries.clear()
        self._entries[key] = _CacheEntry(value=value, expires_at=now + self._ttl)
        return value

    def clear(self) -> None:
        self._entries.clear()


class BoundedBodyMiddleware(BaseHTTPMiddleware):
    """Refuse an oversized body before anything tries to parse it."""

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > MAX_REQUEST_BYTES:
                    return _error(413, "request_too_large", "The request body is too large.")
            except ValueError:
                return _error(400, "invalid_request", "The request is malformed.")
        return await call_next(request)


class SameOriginMiddleware(BaseHTTPMiddleware):
    """No CORS, and no cross-origin writes.

    There is no CORS middleware on this app at all, so a browser cannot read a
    response cross-origin.  This refuses a cross-origin *write* as well, so a
    hostile page cannot drive the Run Strategy Session action.
    """

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
                forwarded = request.headers.get("x-forwarded-proto")
                allowed = {expected, f"https://{request.headers.get('host', '')}"}
                if forwarded:
                    allowed.add(f"{forwarded}://{request.headers.get('host', '')}")
                if origin not in allowed:
                    return _error(403, "cross_origin", "Cross-origin writes are refused.")
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


#: A path parameter is logged only when it still looks like the safe ID or enum
#: the route expects.  A *rejected* request's raw parameter must never reach a
#: log line, so anything else is recorded as the literal ``invalid``.
_SAFE_LOG_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")

LOGGED_PATH_PARAMS = frozenset({"entity", "claim_id", "version", "session_id", "stream_id"})


def safe_log_value(value: object) -> str:
    text = str(value)
    return text if _SAFE_LOG_VALUE.fullmatch(text) else "invalid"


class StructuralLogMiddleware(BaseHTTPMiddleware):
    """Log route, method, status, latency, and validated IDs. Nothing else.

    The raw path and query string are deliberately not logged: a query string is
    caller-controlled text, and a persisted log line must stay structural.
    """

    def __init__(self, app: Any, service: str, revision: str) -> None:
        super().__init__(app)
        self._service = service
        self._revision = revision

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        template = getattr(route, "path", None) or "unmatched"
        params = request.scope.get("path_params") or {}
        record = {
            "service": self._service,
            "revision": self._revision,
            "route": template,
            "method": request.method,
            "status": response.status_code,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            **{
                key: safe_log_value(value)
                for key, value in params.items()
                if key in LOGGED_PATH_PARAMS
            },
        }
        print(json.dumps(record, sort_keys=True), flush=True)
        return response


def _error(status: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=ErrorResponse(error=error, detail=detail).model_dump(mode="json"),
        headers=dict(SECURITY_HEADERS),
    )


def create_app(
    settings: DashboardSettings | None = None,
    *,
    read_model: ReadModel | None = None,
    run_manager: StrategyRunManager | None = None,
    config: TychoConfig | None = None,
) -> FastAPI:
    """Build the dashboard app.  Injectable so tests never touch production."""
    settings = settings or DashboardSettings.from_env()
    config = config or load_config(settings.config_path)

    if read_model is None:
        from dashboard.api.source import CloudReadSource

        read_model = ReadModel(
            CloudReadSource(settings.project, settings.dataset), config
        )
    model = read_model

    if run_manager is None:
        dispatcher = HttpStrategyDispatcher(
            settings.strategy_dispatcher_url or "",
            timeout_seconds=settings.dispatch_timeout_seconds,
        )
        run_manager = StrategyRunManager(
            dispatcher,
            lambda sid: model.activity(sid).events,
            session_for_period=model.session_for_period,
        )
    runs = run_manager

    cache = TtlCache(settings.cache_seconds)
    entity_keys = set(config.entities)
    scope_keys = set(config.ontology)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await runs.aclose()

    app = FastAPI(
        title="Tycho Intelligence Dashboard",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(StructuralLogMiddleware, service=settings.service_name, revision=settings.revision)
    app.add_middleware(SameOriginMiddleware)
    app.add_middleware(BoundedBodyMiddleware)
    app.state.settings = settings
    app.state.read_model = model
    app.state.run_manager = runs
    app.state.cache = cache

    @app.exception_handler(UnknownResource)
    async def _unknown(_: Request, exc: UnknownResource) -> JSONResponse:
        del exc  # The message may name an ID; the response stays generic.
        return _error(404, "not_found", "That resource does not exist.")

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default 422 body echoes the caller's own input.  Reply with
        # the failing field names only, so a request can never be reflected.
        del exc
        return _error(400, "invalid_request", "A path or query value is not valid.")

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return _error(exc.status_code, "request_error", str(exc.detail)[:300])

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Never echo the exception: it can quote a store value or model output.
        print(json.dumps({"service": settings.service_name, "error_class": type(exc).__name__}))
        return _error(500, "internal_error", "The dashboard could not serve that request.")

    # --- Read endpoints ----------------------------------------------------

    def _snapshot():
        return cache.get_or_call("snapshot", model.snapshot)

    @app.get("/api/meta", response_model=MetaResponse)
    def meta() -> MetaResponse:
        return MetaResponse(
            entities=model.entities(),
            scopes=model.scopes(),
            service=settings.service_name,
            revision=settings.revision,
        )

    # Health and overview describe the same moment, so they share one bounded
    # snapshot: two panels never disagree, and a cold load runs the read set
    # once instead of twice.
    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return cache.get_or_call("health", lambda: model.health(_snapshot()))

    @app.get("/api/overview", response_model=OverviewResponse)
    def overview() -> OverviewResponse:
        return cache.get_or_call("overview", lambda: model.overview(_snapshot()))

    @app.get("/api/entities/{entity}/timeline", response_model=TimelineResponse)
    def timeline(
        entity: Annotated[str, Path(pattern=ENTITY_KEY)],
        scope: Annotated[str | None, Query(pattern=SCOPE_KEY, max_length=40)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_TIMELINE_LIMIT)] = 50,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    ) -> TimelineResponse:
        if entity not in entity_keys:
            raise UnknownResource(entity)
        if scope is not None and scope not in scope_keys:
            raise HTTPException(status_code=400, detail="Unknown ontology scope.")
        return model.timeline(entity, scope=scope, limit=limit, offset=offset)

    @app.get(
        "/api/claims/{claim_id}/versions/{version}/provenance",
        response_model=ProvenanceResponse,
    )
    def provenance(
        claim_id: Annotated[str, Path(pattern=CLAIM_ID)],
        version: Annotated[int, Path(ge=1, le=10_000)],
    ) -> ProvenanceResponse:
        return model.provenance(claim_id, version)

    @app.get("/api/strategy/sessions/latest", response_model=StrategySessionResponse)
    def latest_session() -> StrategySessionResponse:
        return model.latest_session()

    @app.get("/api/strategy/sessions/{session_id}", response_model=StrategySessionResponse)
    def session(
        session_id: Annotated[str, Path(pattern=SESSION_ID)],
    ) -> StrategySessionResponse:
        return model.session(session_id)

    @app.get("/api/strategy/sessions/{session_id}/events", response_model=ActivityResponse)
    def session_events(
        session_id: Annotated[str, Path(pattern=SESSION_ID)],
    ) -> ActivityResponse:
        return model.activity(session_id)

    # --- The one write-shaped action ---------------------------------------

    @app.post("/api/strategy/sessions", response_model=TriggerResponse, status_code=202)
    async def trigger_session() -> TriggerResponse:
        """Start the fixed bounded strategy workflow.  No inputs, by design."""
        run, duplicate = await runs.trigger()
        return TriggerResponse(
            run_id=run.run_id,
            state=run.state if isinstance(run.state, RunState) else RunState(run.state),
            duplicate=duplicate or run.duplicate,
            session_id=run.session_id,
            brief_id=run.brief_id,
            period_from=run.period_from,
            period_to=run.period_to,
            stream_path=f"/api/strategy/sessions/{run.run_id}/stream",
            detail=(
                "A run for this period is already in flight; following it."
                if duplicate
                else run.detail
            ),
        )

    @app.get("/api/strategy/sessions/{stream_id}/stream")
    async def stream(
        stream_id: Annotated[str, Path(pattern=STREAM_ID)],
        last_event_id: Annotated[str | None, Header(alias="last-event-id")] = None,
        after: Annotated[int, Query(ge=-1, le=10_000)] = -1,
    ) -> StreamingResponse:
        run = runs.get(stream_id)
        if run is None:
            raise UnknownResource(stream_id)
        resume = after
        if last_event_id is not None and last_event_id.isdigit():
            resume = max(resume, int(last_event_id))

        async def body() -> AsyncIterator[bytes]:
            async for event in runs.stream(run, after=resume):
                payload = json.dumps(event.model_dump(mode="json"), sort_keys=True)
                # Only a real event advances Last-Event-ID. A heartbeat that did
                # so would make a reconnect skip the next real event.
                identity = (
                    "" if event.event is ActivityKind.HEARTBEAT else f"id: {event.seq}\n"
                )
                yield f"{identity}event: {event.event.value}\ndata: {payload}\n\n".encode()
            closing = json.dumps(
                {
                    "state": run.state.value,
                    "session_id": run.session_id,
                    "brief_id": run.brief_id,
                    "duplicate": run.duplicate,
                },
                sort_keys=True,
            )
            yield f"event: stream_closed\ndata: {closing}\n\n".encode()

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                **SECURITY_HEADERS,
            },
        )

    # --- Static frontend ---------------------------------------------------

    static_dir = settings.static_dir
    if static_dir is not None and static_dir.is_dir():
        assets = static_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")
        index = static_dir / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> Response:
            if full_path.startswith("api/"):
                raise UnknownResource(full_path)
            if not index.is_file():
                raise UnknownResource(full_path)
            return FileResponse(
                index,
                media_type="text/html",
                headers={"Cache-Control": "no-store", **SECURITY_HEADERS},
            )

    return app


def utcnow() -> datetime:
    return datetime.now(UTC)
