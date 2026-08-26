"""Acquire official changelog webpages through the shared pipeline contracts."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from adapters.webpage import WebpageAdapter, WebpageFetcher
from pipeline.acquire import (
    AcquisitionBackend,
    AcquisitionResult,
    configured_differ_mode,
)
from pipeline.quarantine import contains_llm_instructions
from pipeline.semantic_differ import (
    GENERATED_BY,
    PROMPT_VERSION,
    GenerationPair,
    SemanticDiffer,
    retry_incomplete_generation_pairs,
    run_semantic_generation,
)
from schemas.common import new_prefixed_id
from schemas.config import EntityConfig
from schemas.observation import Observation, ObservationKind, ObservationStatus

_SOURCE = "website_changelog"
_ROUTED_SCOPES = ["product/capabilities", "product/roadmap"]
LOGGER = logging.getLogger(__name__)


def _hash(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def acquire_website_changelog(
    entity_key: str,
    entity: EntityConfig,
    backend: AcquisitionBackend,
    adapter: WebpageFetcher,
    *,
    now: datetime | None = None,
    differ: SemanticDiffer | None = None,
    mode: str | None = None,
    retry_pending: bool = True,
) -> AcquisitionResult:
    fetched_at = now or datetime.now(UTC)
    obs_id = new_prefixed_id("obs")
    webpage_source = entity.sources.website_changelog
    if webpage_source is None:
        raise ValueError(f"{entity_key} has no website_changelog source")
    configured_differ_mode(mode)
    semantic_differ = differ or SemanticDiffer()
    if retry_pending:
        retry_incomplete_generation_pairs(
            backend,  # type: ignore[arg-type]
            semantic_differ,
            mode="semantic",
            now=fetched_at,
        )
    previous = backend.latest_observation(entity_key, _SOURCE)

    try:
        fetched = adapter.fetch(str(webpage_source.url), webpage_source.extract_hint)
        payload = fetched.payload
        status = (
            ObservationStatus.QUARANTINED
            if contains_llm_instructions(payload)
            else ObservationStatus.OK
        )
        suffix = ".json"
    except Exception as exc:
        payload = json.dumps(
            {"error": type(exc).__name__, "message": str(exc)},
            sort_keys=True,
        ).encode()
        status = ObservationStatus.FETCH_FAILED
        suffix = ".error.json"

    content_ref = backend.put_raw(
        entity_key, _SOURCE, obs_id, payload, suffix=suffix
    )
    observation = Observation(
        obs_id=obs_id,
        entity=entity_key,
        source=_SOURCE,
        kind=ObservationKind.TEXT,
        fetched_at=fetched_at,
        content_ref=content_ref,
        content_hash=_hash(payload),
        adapter_ver=WebpageAdapter.adapter_version,
        status=status,
    )
    backend.insert_observation(observation)

    if status is ObservationStatus.FETCH_FAILED:
        return AcquisitionResult(entity_key, obs_id, "fetch_failed")
    if status is ObservationStatus.QUARANTINED:
        return AcquisitionResult(entity_key, obs_id, "quarantined")
    if previous is None:
        return AcquisitionResult(entity_key, obs_id, "bootstrapped")
    if previous.content_hash == observation.content_hash:
        backend.bump_verified(entity_key, _ROUTED_SCOPES, fetched_at)
        return AcquisitionResult(entity_key, obs_id, "unchanged")

    generation = run_semantic_generation(
        GenerationPair(
            previous,
            observation,
            generated_by=GENERATED_BY,
            prompt_version=PROMPT_VERSION,
        ),
        backend=backend,  # type: ignore[arg-type]
        differ=semantic_differ,
        mode="semantic",
        now=fetched_at,
    )
    if generation.delta is not None and generation.state == "completed":
        return AcquisitionResult(
            entity_key,
            obs_id,
            generation.outcome or "generation_failed",
            generation.delta.delta_id,
        )
    return AcquisitionResult(
        entity_key,
        obs_id,
        "generation_failed"
        if generation.state == "failed"
        else "generation_active",
    )
