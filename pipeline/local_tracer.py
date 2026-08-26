"""Exercise the full logical tracer with live GitHub data and in-memory cloud stores."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from adapters.github import GithubFetch, GithubReleasesAdapter
from pipeline.acquire import acquire_github_releases
from pipeline.analyst import build_stub_claim
from pipeline.semantic_differ import DeltaGenerationLeaseDecision
from schemas.common import new_prefixed_id
from schemas.config import TychoConfig, load_config
from schemas.delta import Delta
from schemas.observation import Observation, ObservationKind, ObservationStatus


class StaticAdapter:
    def __init__(self, fetched: GithubFetch) -> None:
        self.fetched = fetched

    def fetch_releases(self, repository: str) -> GithubFetch:
        if repository != self.fetched.repository:
            raise ValueError(f"unexpected repository: {repository}")
        return self.fetched


class MemoryBackend:
    def __init__(self, config: TychoConfig) -> None:
        self.config = config
        self.raw: dict[str, bytes] = {}
        self.latest: dict[tuple[str, str], Observation] = {}
        self.observations: list[Observation] = []
        self.deltas: list[Delta] = []
        self.claims = []
        self.generation_leases: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def latest_observation(self, entity: str, source: str) -> Observation | None:
        return self.latest.get((entity, source))

    def put_raw(
        self,
        entity: str,
        source: str,
        obs_id: str,
        payload: bytes,
        *,
        suffix: str = ".json",
    ) -> str:
        ref = f"memory://{entity}/{source}/{obs_id}{suffix}"
        self.raw[ref] = payload
        return ref

    def get_raw(self, content_ref: str) -> bytes:
        return self.raw[content_ref]

    def insert_observation(self, observation: Observation) -> None:
        self.observations.append(observation)
        if observation.status is ObservationStatus.OK:
            self.latest[(observation.entity, observation.source)] = observation

    def insert_delta(self, delta: Delta, *, enqueue: bool = True) -> None:
        del enqueue
        self.deltas.append(delta)

    def find_delta_by_comparison_id(self, comparison_id: str) -> Delta | None:
        return next(
            (delta for delta in self.deltas if delta.comparison_id == comparison_id),
            None,
        )

    @staticmethod
    def _generation_key(obs_before: str, obs_after: str, generated_by: str, prompt_version: str):
        return obs_before, obs_after, generated_by, prompt_version

    def acquire_delta_generation_lease(
        self,
        obs_before,
        obs_after,
        generated_by,
        prompt_version,
        run_id,
        started_at,
        lease_expires_at,
        traffic_type="semantic",
    ):
        del started_at, lease_expires_at, traffic_type
        key = self._generation_key(obs_before, obs_after, generated_by, prompt_version)
        record = self.generation_leases.get(key)
        if record and record["state"] == "completed":
            return DeltaGenerationLeaseDecision(
                "completed", record["run_id"], record["attempt"], record.get("delta_id"), record.get("outcome")
            )
        if record and record["state"] == "active":
            return DeltaGenerationLeaseDecision("active", record["run_id"], record["attempt"])
        attempt = (record["attempt"] + 1) if record else 1
        self.generation_leases[key] = {"state": "active", "run_id": run_id, "attempt": attempt}
        return DeltaGenerationLeaseDecision("acquired", run_id, attempt)

    def start_delta_generation_run(self, *args, **kwargs) -> None:
        del args, kwargs

    def finish_delta_generation_run(self, *args, **kwargs) -> None:
        del args, kwargs

    def complete_delta_generation_lease(
        self,
        obs_before,
        obs_after,
        generated_by,
        prompt_version,
        run_id,
        finished_at,
        *,
        delta_id,
        outcome,
    ) -> None:
        del finished_at
        key = self._generation_key(obs_before, obs_after, generated_by, prompt_version)
        record = self.generation_leases[key]
        record.update(state="completed", run_id=run_id, delta_id=delta_id, outcome=outcome)

    def fail_delta_generation_lease(
        self,
        obs_before,
        obs_after,
        generated_by,
        prompt_version,
        run_id,
        finished_at,
        error,
    ) -> None:
        del finished_at, error
        key = self._generation_key(obs_before, obs_after, generated_by, prompt_version)
        if key in self.generation_leases and self.generation_leases[key]["run_id"] == run_id:
            self.generation_leases[key]["state"] = "failed"

    def publish_delta(self, delta: Delta) -> str:
        self.claims.append(build_stub_claim(delta, self.config))
        return f"memory-message-{len(self.claims)}"

    def bump_verified(self, entity: str, scopes: list[str], verified_at: datetime) -> int:
        return 0

    def preload_previous(self, entity: str, source: str, payload: bytes, at: datetime) -> None:
        obs_id = new_prefixed_id("obs")
        content_ref = self.put_raw(entity, source, obs_id, payload)
        self.insert_observation(
            Observation(
                obs_id=obs_id,
                entity=entity,
                source=source,
                kind=ObservationKind.STRUCTURED,
                fetched_at=at,
                content_ref=content_ref,
                content_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
                adapter_ver="github@1",
                status=ObservationStatus.OK,
            )
        )


def main() -> None:
    config = load_config("tycho.yaml")
    live_adapter = GithubReleasesAdapter()
    backend = MemoryBackend(config)
    now = datetime.now(UTC)
    output: list[dict[str, Any]] = []

    for entity_key, entity in config.entities.items():
        source = entity.sources.github_releases
        if source is None:
            continue
        fetched = live_adapter.fetch_releases(source.repo)
        if len(fetched.releases) < 2:
            output.append({"entity": entity_key, "outcome": "needs_two_releases"})
            continue

        # Both snapshots are real source records: the prior snapshot omits only
        # the newest release to deterministically replay its arrival.
        prior_payload = json.dumps(
            fetched.releases[1:], sort_keys=True, separators=(",", ":")
        ).encode()
        backend.preload_previous(
            entity_key,
            "github_releases",
            prior_payload,
            now - timedelta(minutes=5),
        )
        result = acquire_github_releases(
            entity_key,
            entity,
            backend,
            StaticAdapter(fetched),
            now=now,
        )
        claim = backend.claims[-1] if result.delta_id and backend.claims else None
        output.append(
            {
                "entity": entity_key,
                "repository": source.repo,
                "outcome": result.outcome,
                "delta_id": result.delta_id,
                "claim": claim.statement if claim else None,
            }
        )

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
