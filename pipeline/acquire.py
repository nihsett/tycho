"""Cloud Run acquisition job: fetch, store, hash gate, diff, and publish."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from adapters.github import GithubAdapter, GithubReleasesAdapter
from pipeline.cloud import CloudBackend, CloudSettings
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
from schemas.config import EntityConfig, load_config
from schemas.delta import Delta
from schemas.observation import Observation, ObservationKind, ObservationStatus

_SOURCE = "github_releases"
_ROUTED_SCOPES = ["product/capabilities", "product/roadmap"]
LOGGER = logging.getLogger(__name__)
_VALID_DIFFER_MODES = {"semantic"}


def configured_differ_mode(value: str | None = None) -> str:
    mode = value or os.getenv("TYCHO_DIFFER_MODE", "semantic")
    if mode not in _VALID_DIFFER_MODES:
        raise ValueError(
            "TYCHO_DIFFER_MODE must be semantic; legacy and shadow are retired "
            "from acquisition"
        )
    return mode


class AcquisitionBackend(Protocol):
    def latest_observation(self, entity: str, source: str) -> Observation | None: ...

    def put_raw(
        self,
        entity: str,
        source: str,
        obs_id: str,
        payload: bytes,
        *,
        suffix: str = ".json",
    ) -> str: ...

    def get_raw(self, content_ref: str) -> bytes: ...

    def insert_observation(self, observation: Observation) -> None: ...

    def insert_delta(self, delta: Delta, *, enqueue: bool = True) -> None: ...

    def publish_delta(self, delta: Delta) -> str: ...

    def bump_verified(self, entity: str, scopes: list[str], verified_at: datetime) -> int: ...


@dataclass(frozen=True)
class AcquisitionResult:
    entity: str
    observation_id: str
    outcome: str
    delta_id: str | None = None


def _hash(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def acquire_github_releases(
    entity_key: str,
    entity: EntityConfig,
    backend: AcquisitionBackend,
    adapter: GithubAdapter,
    *,
    now: datetime | None = None,
    differ: SemanticDiffer | None = None,
    mode: str | None = None,
    retry_pending: bool = True,
) -> AcquisitionResult:
    fetched_at = now or datetime.now(UTC)
    obs_id = new_prefixed_id("obs")
    github_source = entity.sources.github_releases
    if github_source is None:
        raise ValueError(f"{entity_key} has no github_releases source")
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
        fetched = adapter.fetch_releases(github_source.repo)
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
        kind=ObservationKind.STRUCTURED,
        fetched_at=fetched_at,
        content_ref=content_ref,
        content_hash=_hash(payload),
        adapter_ver="github@1",
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


def main() -> None:
    # Imported here to avoid a circular import: the webpage acquisition module
    # shares AcquisitionBackend and AcquisitionResult from this module.
    from adapters.webpage import WebpageAdapter
    from pipeline.acquire_webpage import acquire_website_changelog

    settings = CloudSettings.from_env()
    config = load_config(settings.config_path)
    backend = CloudBackend(settings)
    differ_mode = configured_differ_mode()
    semantic_differ = SemanticDiffer(project=settings.project)
    retry_results = retry_incomplete_generation_pairs(
        backend,
        semantic_differ,
        mode="semantic",
    )
    github_adapter = GithubReleasesAdapter()
    webpage_adapter = WebpageAdapter()
    results = []
    for entity_key, entity in config.entities.items():
        if entity.sources.github_releases is not None:
            result = acquire_github_releases(
                entity_key,
                entity,
                backend,
                github_adapter,
                differ=semantic_differ,
                mode=differ_mode,
                retry_pending=False,
            )
            results.append({"source": "github_releases", **result.__dict__})
        if entity.sources.website_changelog is not None:
            result = acquire_website_changelog(
                entity_key,
                entity,
                backend,
                webpage_adapter,
                differ=semantic_differ,
                mode=differ_mode,
                retry_pending=False,
            )
            results.append({"source": "website_changelog", **result.__dict__})
    print(
        json.dumps(
            {
                "differ_mode": differ_mode,
                "retried_generation_pairs": [
                    {
                        "state": item.state,
                        "outcome": item.outcome,
                        "run_id": item.run_id,
                        "delta_id": item.delta.delta_id if item.delta else None,
                    }
                    for item in retry_results
                ],
                "watchers": results,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
