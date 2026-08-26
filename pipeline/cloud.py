"""Google Cloud persistence boundary for the tracer bullet."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud import bigquery, firestore, pubsub_v1, storage

from pipeline.analyst_lease import (
    AnalystLeaseDecision,
    lease_document_id,
    lease_is_active,
)
from pipeline.semantic_differ import (
    DeltaGenerationLeaseDecision,
    GenerationPair,
)
from pipeline.strategy_lease import (
    StrategyLeaseDecision,
    strategy_lease_document_id,
    strategy_lease_is_active,
)
from google.cloud.firestore_v1.base_query import FieldFilter

from schemas.brief import Brief
from schemas.claim import Claim
from schemas.delta import (
    CANONICAL_GENERATED_BY,
    CANONICAL_PROMPT_VERSION,
    Delta,
    DeltaSchemaVersion,
    Triage,
)
from schemas.observation import Observation
from schemas.receipt import DeliveryReceipt
from schemas.strategy import SessionState, StrategySession


@dataclass(frozen=True)
class CloudSettings:
    project: str
    bucket: str
    dataset: str = "tycho"
    topic: str = "tycho-deltas"
    config_path: str = "tycho.yaml"

    @classmethod
    def from_env(cls) -> "CloudSettings":
        project = os.getenv("TYCHO_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
        if not project:
            raise RuntimeError("TYCHO_PROJECT or GOOGLE_CLOUD_PROJECT is required")
        return cls(
            project=project,
            bucket=os.getenv("TYCHO_BUCKET", f"{project}-tycho-raw"),
            dataset=os.getenv("TYCHO_DATASET", "tycho"),
            topic=os.getenv("TYCHO_TOPIC", "tycho-deltas"),
            config_path=os.getenv("TYCHO_CONFIG", "tycho.yaml"),
        )


def bigquery_json_value(value: Any) -> str | None:
    """Encode a value for a BigQuery JSON field in streaming inserts."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def delta_row_for_bigquery(delta: Delta) -> dict[str, Any]:
    """Encode a Delta for compatibility/audit decoding.

    Canonical writes use ``canonical_delta_row_for_bigquery`` below. Keeping
    this compatibility helper permissive lets migration code preserve the
    immutable v1 physical row without making it writable through the normal
    backend boundary.
    """
    row = delta.model_dump(mode="json")
    for change in row["changes"]:
        for field in ("before", "after"):
            value = change[field]
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    parsed = None
                change[field] = value if isinstance(parsed, (dict, list)) else bigquery_json_value(value)
            else:
                change[field] = bigquery_json_value(value)
    return row


def canonical_delta_row_for_bigquery(delta: Delta) -> dict[str, Any]:
    """Encode one strict v2 row for ``tycho.deltas``."""
    if delta.schema_version is not DeltaSchemaVersion.V2:
        raise ValueError("canonical BigQuery writes accept only delta@2")
    if delta.generated_by != CANONICAL_GENERATED_BY:
        raise ValueError("canonical Delta has an invalid generator")
    if delta.prompt_version != CANONICAL_PROMPT_VERSION:
        raise ValueError("canonical Delta has an invalid prompt version")
    row = delta.model_dump(mode="json")
    for change in row["changes"]:
        change.pop("path", None)
        change["before"] = bigquery_json_value(change["before"])
        change["after"] = bigquery_json_value(change["after"])
    return row


def _firestore_client(project: str) -> firestore.Client:
    """Use the Agent Identity mTLS endpoint when the runtime requires it.

    The high-level Firestore client in the pinned dependency does not propagate
    its mTLS environment choice into the custom gRPC channel it creates. Agent
    Identity tokens are certificate-bound, so the managed runtime must use the
    mTLS endpoint and certificate explicitly. Local and legacy Cloud Run paths
    retain the standard client behavior.
    """
    if os.getenv("GOOGLE_API_USE_MTLS_ENDPOINT", "").lower() != "always":
        return firestore.Client(project=project)

    from google.auth import default
    from google.auth.transport import mtls
    from google.cloud.firestore_v1.services.firestore import FirestoreClient
    from google.cloud.firestore_v1.services.firestore.transports.grpc import (
        FirestoreGrpcTransport,
    )

    credentials, _ = default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    cert_source = mtls.default_client_cert_source()
    if cert_source is None:
        raise RuntimeError("Agent Identity mTLS certificate is unavailable")
    transport = FirestoreGrpcTransport(
        host="firestore.mtls.googleapis.com",
        credentials=credentials,
        client_cert_source_for_mtls=cert_source,
    )
    api = FirestoreClient(transport=transport)
    client = firestore.Client(project=project, credentials=credentials)
    client._firestore_api_internal = api  # noqa: SLF001 - pinned client seam
    return client


class CloudBackend:
    def __init__(self, settings: CloudSettings) -> None:
        self.settings = settings
        self.bigquery = bigquery.Client(project=settings.project)
        self.storage = storage.Client(project=settings.project)
        self.firestore = _firestore_client(settings.project)
        self.publisher = pubsub_v1.PublisherClient()

    @property
    def observations_table(self) -> str:
        return f"{self.settings.project}.{self.settings.dataset}.observations"

    @property
    def deltas_table(self) -> str:
        return f"{self.settings.project}.{self.settings.dataset}.deltas"

    @property
    def audit_deltas_table(self) -> str:
        return (
            f"{self.settings.project}.{self.settings.dataset}."
            "delta_audit_log_20260826"
        )

    def latest_observation(self, entity: str, source: str) -> Observation | None:
        query = f"""
            SELECT obs_id, entity, source, kind, fetched_at, content_ref,
                   content_hash, adapter_ver, status
            FROM `{self.observations_table}`
            WHERE entity = @entity AND source = @source AND status = 'ok'
            ORDER BY fetched_at DESC
            LIMIT 1
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("entity", "STRING", entity),
                bigquery.ScalarQueryParameter("source", "STRING", source),
            ]
        )
        rows = list(self.bigquery.query(query, job_config=config).result())
        return Observation.model_validate(dict(rows[0])) if rows else None

    def put_raw(
        self,
        entity: str,
        source: str,
        obs_id: str,
        payload: bytes,
        *,
        suffix: str = ".json",
    ) -> str:
        object_name = f"{entity}/{source}/{obs_id}{suffix}"
        blob = self.storage.bucket(self.settings.bucket).blob(object_name)
        blob.upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=0,
        )
        return f"gs://{self.settings.bucket}/{object_name}"

    def get_raw(self, content_ref: str) -> bytes:
        if not content_ref.startswith("gs://"):
            raise ValueError(f"unsupported content reference: {content_ref}")
        bucket_name, _, object_name = content_ref[5:].partition("/")
        if not bucket_name or not object_name:
            raise ValueError(f"invalid GCS content reference: {content_ref}")
        return self.storage.bucket(bucket_name).blob(object_name).download_as_bytes()

    def insert_observation(self, observation: Observation) -> None:
        errors = self.bigquery.insert_rows_json(
            self.observations_table,
            [observation.model_dump(mode="json")],
        )
        if errors:
            raise RuntimeError(f"BigQuery observation insert failed: {errors}")

    def insert_delta(self, delta: Delta, *, enqueue: bool = True) -> None:
        del enqueue  # Pub/Sub publication is a separate explicit operation in GCP.
        errors = self.bigquery.insert_rows_json(
            self.deltas_table,
            [canonical_delta_row_for_bigquery(delta)],
        )
        if errors:
            raise RuntimeError(f"BigQuery canonical delta insert failed: {errors}")

    def create_claim(self, claim: Claim) -> None:
        self.firestore.collection("claims").document(claim.claim_id).create(
            claim.model_dump(mode="json", by_alias=True)
        )

    def update_claim(self, claim_id: str, fields: dict[str, Any]) -> None:
        self.firestore.collection("claims").document(claim_id).update(fields)

    def get_claim(self, claim_id: str) -> Claim | None:
        snapshot = self.firestore.collection("claims").document(claim_id).get()
        return Claim.model_validate(snapshot.to_dict()) if snapshot.exists else None

    def list_claims(self) -> list[Claim]:
        return [
            Claim.model_validate(snapshot.to_dict())
            for snapshot in self.firestore.collection("claims").stream()
        ]

    def active_claims(self, entity: str, scopes: list[str]) -> list[Claim]:
        if not scopes:
            return []
        scope_set = set(scopes)
        snapshots = self.firestore.collection("claims").where(
            filter=FieldFilter("entity", "==", entity)
        ).stream()
        return [
            claim
            for snapshot in snapshots
            if (claim := Claim.model_validate(snapshot.to_dict())).status.value == "active"
            and claim.scope in scope_set
        ]

    def get_observation(self, obs_id: str) -> Observation | None:
        query = f"""
            SELECT obs_id, entity, source, kind, fetched_at, content_ref,
                   content_hash, adapter_ver, status
            FROM `{self.observations_table}`
            WHERE obs_id = @obs_id
            LIMIT 1
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("obs_id", "STRING", obs_id)]
        )
        rows = list(self.bigquery.query(query, job_config=config).result())
        return Observation.model_validate(dict(rows[0])) if rows else None

    @staticmethod
    def _decode_delta_row(row: Any) -> Delta:
        data = dict(row)
        if not data.get("schema_version"):
            data.pop("schema_version", None)
        raw_changes = data.get("changes") or []
        data["changes"] = []
        for raw_change in raw_changes:
            change = dict(raw_change)
            for field in ("before", "after"):
                value = change.get(field)
                if isinstance(value, str):
                    try:
                        change[field] = json.loads(value)
                    except json.JSONDecodeError:
                        # A legacy JSON field may already be a plain string.
                        pass
            data["changes"].append(change)
        return Delta.model_validate(data)

    def get_delta(self, delta_id: str) -> Delta | None:
        query = f"""
            SELECT schema_version, delta_id, comparison_id, entity, source,
                   obs_before, obs_after, computed_at, diff_kind, generated_by,
                   prompt_version, changes, summary, triage, triage_reason,
                   triage_by, routed_to
            FROM `{self.deltas_table}`
            WHERE delta_id = @delta_id AND schema_version = 'delta@2'
            LIMIT 1
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("delta_id", "STRING", delta_id)
            ]
        )
        rows = list(self.bigquery.query(query, job_config=config).result())
        if not rows:
            return None
        return self._decode_delta_row(rows[0])

    def get_audit_delta(self, delta_id: str) -> Delta | None:
        """Read one immutable migration/audit row; never used by normal reads."""
        query = f"""
            SELECT schema_version, delta_id, comparison_id, entity, source,
                   obs_before, obs_after, computed_at, diff_kind, generated_by,
                   prompt_version, changes, summary, triage, triage_reason,
                   triage_by, routed_to
            FROM `{self.audit_deltas_table}`
            WHERE delta_id = @delta_id
            LIMIT 1
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("delta_id", "STRING", delta_id)
            ]
        )
        rows = list(self.bigquery.query(query, job_config=config).result())
        return self._decode_delta_row(rows[0]) if rows else None

    def list_audit_deltas(self, *, legacy_only: bool = False) -> list[Delta]:
        """List archive rows explicitly for migration/audit tooling only."""
        where = "WHERE schema_version IS NULL OR schema_version = 'delta@1'" if legacy_only else ""
        query = f"""
            SELECT schema_version, delta_id, comparison_id, entity, source,
                   obs_before, obs_after, computed_at, diff_kind, generated_by,
                   prompt_version, changes, summary, triage, triage_reason,
                   triage_by, routed_to
            FROM `{self.audit_deltas_table}`
            {where}
            ORDER BY computed_at, delta_id
        """
        return [
            self._decode_delta_row(row)
            for row in self.bigquery.query(query).result()
        ]

    def find_audit_delta_by_comparison_id(self, comparison_id: str) -> Delta | None:
        """Find an archived row by comparison identity for repair tooling."""
        query = f"""
            SELECT schema_version, delta_id, comparison_id, entity, source,
                   obs_before, obs_after, computed_at, diff_kind, generated_by,
                   prompt_version, changes, summary, triage, triage_reason,
                   triage_by, routed_to
            FROM `{self.audit_deltas_table}`
            WHERE comparison_id = @comparison_id
            ORDER BY computed_at, delta_id
            LIMIT 1
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("comparison_id", "STRING", comparison_id)
            ]
        )
        rows = list(self.bigquery.query(query, job_config=config).result())
        return self._decode_delta_row(rows[0]) if rows else None

    def find_delta_by_comparison_id(self, comparison_id: str) -> Delta | None:
        query = f"""
            SELECT delta_id
            FROM `{self.deltas_table}`
            WHERE comparison_id = @comparison_id AND schema_version = 'delta@2'
            ORDER BY computed_at
            LIMIT 1
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("comparison_id", "STRING", comparison_id)
            ]
        )
        rows = list(self.bigquery.query(query, job_config=config).result())
        return self.get_delta(rows[0].delta_id) if rows else None

    @staticmethod
    def _generation_lease_id(
        obs_before: str, obs_after: str, generated_by: str, prompt_version: str
    ) -> str:
        return lease_document_id(
            f"{obs_before}:{obs_after}", generated_by, prompt_version
        )

    @staticmethod
    def _generation_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return None

    def acquire_delta_generation_lease(
        self,
        obs_before: str,
        obs_after: str,
        generated_by: str,
        prompt_version: str,
        run_id: str,
        started_at: datetime,
        lease_expires_at: datetime,
        traffic_type: str = "semantic",
    ) -> DeltaGenerationLeaseDecision:
        lease_ref = self.firestore.collection("delta_generation_leases").document(
            self._generation_lease_id(obs_before, obs_after, generated_by, prompt_version)
        )
        transaction = self.firestore.transaction()

        @firestore.transactional
        def commit(txn: firestore.Transaction) -> DeltaGenerationLeaseDecision:
            snapshot = lease_ref.get(transaction=txn)
            if not snapshot.exists:
                txn.create(
                    lease_ref,
                    {
                        "obs_before": obs_before,
                        "obs_after": obs_after,
                        "generated_by": generated_by,
                        "prompt_version": prompt_version,
                        "state": "active",
                        "run_id": run_id,
                        "attempt": 1,
                        "started_at": started_at,
                        "lease_expires_at": lease_expires_at,
                        "traffic_type": traffic_type,
                    },
                )
                return DeltaGenerationLeaseDecision("acquired", run_id, 1)
            record = snapshot.to_dict() or {}
            if record.get("state") == "completed" and not (
                traffic_type in {"semantic", "historical_backfill"}
                and record.get("traffic_type") == "shadow"
            ):
                return DeltaGenerationLeaseDecision(
                    "completed",
                    record.get("run_id"),
                    int(record.get("attempt", 0)),
                    record.get("delta_id"),
                    record.get("outcome"),
                )
            expires_at = self._generation_datetime(record.get("lease_expires_at"))
            if record.get("state") == "active" and expires_at and expires_at > started_at:
                return DeltaGenerationLeaseDecision(
                    "active", record.get("run_id"), int(record.get("attempt", 0))
                )
            attempt = int(record.get("attempt", 0)) + 1
            txn.update(
                lease_ref,
                {
                    "state": "active",
                    "run_id": run_id,
                    "attempt": attempt,
                    "started_at": started_at,
                    "lease_expires_at": lease_expires_at,
                    "traffic_type": traffic_type,
                    "finished_at": None,
                    "delta_id": None,
                    "outcome": None,
                    "error": None,
                },
            )
            return DeltaGenerationLeaseDecision("acquired", run_id, attempt)

        return commit(transaction)

    def start_delta_generation_run(
        self,
        run_id: str,
        obs_before: str,
        obs_after: str,
        generated_by: str,
        prompt_version: str,
        model: str,
        attempt: int,
        started_at: datetime,
        input_bytes: int,
        estimated_input_tokens: int,
        traffic_type: str = "semantic",
        obs_before_hash: str | None = None,
        obs_after_hash: str | None = None,
    ) -> None:
        self.firestore.collection("delta_generation_runs").document(run_id).create(
            {
                "run_id": run_id,
                "obs_before": obs_before,
                "obs_after": obs_after,
                "obs_before_hash": obs_before_hash,
                "obs_after_hash": obs_after_hash,
                "generated_by": generated_by,
                "prompt_version": prompt_version,
                "model": model,
                "attempt": attempt,
                "state": "running",
                "started_at": started_at,
                "input_bytes": input_bytes,
                "estimated_input_tokens": estimated_input_tokens,
                "traffic_type": traffic_type,
            }
        )

    def finish_delta_generation_run(
        self,
        run_id: str,
        finished_at: datetime,
        *,
        outcome: str | None,
        validation: str,
        delta_id: str | None = None,
        usage: dict[str, int | float] | None = None,
        latency_ms: int | None = None,
        error_class: str | None = None,
        error_message: str | None = None,
    ) -> None:
        usage = usage or {}
        self.firestore.collection("delta_generation_runs").document(run_id).update(
            {
                "state": "failed" if error_class else "completed",
                "finished_at": finished_at,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "thinking_tokens": usage.get("thinking_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "estimated_cost_usd": usage.get("estimated_cost_usd"),
                "latency_ms": latency_ms,
                "outcome": outcome,
                "validation": validation,
                "delta_id": delta_id,
                "error_class": error_class,
                "error_message": error_message[:500] if error_message else None,
            }
        )

    def complete_delta_generation_lease(
        self,
        obs_before: str,
        obs_after: str,
        generated_by: str,
        prompt_version: str,
        run_id: str,
        finished_at: datetime,
        *,
        delta_id: str | None,
        outcome: str,
    ) -> None:
        lease_ref = self.firestore.collection("delta_generation_leases").document(
            self._generation_lease_id(obs_before, obs_after, generated_by, prompt_version)
        )
        transaction = self.firestore.transaction()

        @firestore.transactional
        def commit(txn: firestore.Transaction) -> None:
            snapshot = lease_ref.get(transaction=txn)
            if not snapshot.exists:
                raise KeyError(f"unknown delta generation lease: {lease_ref.id}")
            record = snapshot.to_dict() or {}
            if record.get("state") == "completed" and record.get("run_id") == run_id:
                return
            if record.get("state") != "active" or record.get("run_id") != run_id:
                raise RuntimeError("delta generation lease is no longer owned by this run")
            txn.update(
                lease_ref,
                {
                    "state": "completed",
                    "finished_at": finished_at,
                    "delta_id": delta_id,
                    "outcome": outcome,
                    "error": None,
                },
            )

        commit(transaction)

    def fail_delta_generation_lease(
        self,
        obs_before: str,
        obs_after: str,
        generated_by: str,
        prompt_version: str,
        run_id: str,
        finished_at: datetime,
        error: str,
    ) -> None:
        lease_ref = self.firestore.collection("delta_generation_leases").document(
            self._generation_lease_id(obs_before, obs_after, generated_by, prompt_version)
        )
        transaction = self.firestore.transaction()

        @firestore.transactional
        def commit(txn: firestore.Transaction) -> None:
            snapshot = lease_ref.get(transaction=txn)
            if not snapshot.exists:
                return
            record = snapshot.to_dict() or {}
            if record.get("state") != "active" or record.get("run_id") != run_id:
                return
            txn.update(
                lease_ref,
                {
                    "state": "failed",
                    "finished_at": finished_at,
                    "error": error[:500],
                },
            )

        commit(transaction)

    def retryable_delta_generation_pairs(self, now: datetime) -> list[GenerationPair]:
        pairs: list[GenerationPair] = []
        for snapshot in self.firestore.collection("delta_generation_leases").stream():
            record = snapshot.to_dict() or {}
            if record.get("state") == "failed":
                retry = True
            elif record.get("state") == "active":
                expires_at = self._generation_datetime(record.get("lease_expires_at"))
                retry = expires_at is not None and expires_at <= now
            else:
                retry = False
            if not retry:
                continue
            before = self.get_observation(record.get("obs_before", ""))
            after = self.get_observation(record.get("obs_after", ""))
            if before is not None and after is not None:
                pairs.append(
                    GenerationPair(
                        before,
                        after,
                        generated_by=record["generated_by"],
                        prompt_version=record["prompt_version"],
                    )
                )
        return pairs

    def record_historical_delta_import(
        self,
        *,
        delta: Delta,
        legacy_delta_id: str,
        usage: dict[str, int | float],
        input_bytes: int,
        estimated_input_tokens: int,
        source: str,
    ) -> None:
        """Persist safe replay provenance without model/source payloads."""
        if delta.schema_version is not DeltaSchemaVersion.V2:
            raise ValueError("historical imports must reference canonical delta@2")
        comparison_id = delta.comparison_id or ""
        document_id = hashlib.sha256(comparison_id.encode()).hexdigest()
        self.firestore.collection("historical_delta_imports").document(document_id).set(
            {
                "comparison_id": comparison_id,
                "legacy_delta_id": legacy_delta_id,
                "delta_id": delta.delta_id,
                "obs_before": delta.obs_before,
                "obs_after": delta.obs_after,
                "entity": delta.entity,
                "source": delta.source,
                "generated_by": delta.generated_by,
                "prompt_version": delta.prompt_version,
                "traffic_type": "historical_replay_import",
                "source_kind": source,
                "historical_replay_import": True,
                "input_bytes": input_bytes,
                "estimated_input_tokens": estimated_input_tokens,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "thinking_tokens": usage.get("thinking_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "estimated_cost_usd": usage.get("estimated_cost_usd"),
                "outcome": delta.triage.value,
            },
            merge=True,
        )

    def start_analyst_run(
        self,
        run_id: str,
        delta_id: str,
        mode: str,
        analyst_version: str,
        model: str,
        input_document: str,
        started_at: datetime,
    ) -> None:
        self.firestore.collection("analyst_runs").document(run_id).create(
            {
                "run_id": run_id,
                "delta_id": delta_id,
                "mode": mode,
                "analyst_version": analyst_version,
                "model": model,
                "state": "running",
                "started_at": started_at.isoformat(),
                "input_document": input_document,
            }
        )

    def finish_analyst_run(
        self,
        run_id: str,
        *,
        actions: list[dict[str, Any]],
        final_text: str,
        finished_at: datetime,
        error: str | None = None,
    ) -> None:
        self.firestore.collection("analyst_runs").document(run_id).update(
            {
                "state": "failed" if error else "completed",
                "finished_at": finished_at.isoformat(),
                "actions": actions,
                "final_text": final_text,
                "error": error,
            }
        )

    def acquire_analyst_lease(
        self,
        delta_id: str,
        mode: str,
        analyst_version: str,
        run_id: str,
        started_at: datetime,
        lease_expires_at: datetime,
        *,
        force: bool = False,
    ) -> AnalystLeaseDecision:
        lease_ref = self.firestore.collection("analyst_run_leases").document(
            lease_document_id(delta_id, mode, analyst_version)
        )
        transaction = self.firestore.transaction()

        @firestore.transactional
        def commit(txn: firestore.Transaction) -> AnalystLeaseDecision:
            snapshot = lease_ref.get(transaction=txn)
            if not snapshot.exists:
                txn.create(
                    lease_ref,
                    {
                        "delta_id": delta_id,
                        "mode": mode,
                        "analyst_version": analyst_version,
                        "state": "active",
                        "run_id": run_id,
                        "attempt": 1,
                        "started_at": started_at,
                        "lease_expires_at": lease_expires_at,
                    },
                )
                return AnalystLeaseDecision("acquired", run_id, 1)

            record = snapshot.to_dict() or {}
            if record.get("state") == "completed" and not force:
                return AnalystLeaseDecision(
                    "completed", record.get("run_id"), int(record.get("attempt", 0))
                )

            expires_at = record.get("lease_expires_at")
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if record.get("state") == "active" and lease_is_active(
                expires_at, started_at
            ):
                return AnalystLeaseDecision(
                    "active", record.get("run_id"), int(record.get("attempt", 0))
                )

            attempt = int(record.get("attempt", 0)) + 1
            txn.update(
                lease_ref,
                {
                    "delta_id": delta_id,
                    "mode": mode,
                    "analyst_version": analyst_version,
                    "state": "active",
                    "run_id": run_id,
                    "attempt": attempt,
                    "started_at": started_at,
                    "lease_expires_at": lease_expires_at,
                    "finished_at": None,
                    "error": None,
                },
            )
            return AnalystLeaseDecision("acquired", run_id, attempt)

        return commit(transaction)

    def complete_analyst_lease(
        self,
        delta_id: str,
        mode: str,
        analyst_version: str,
        run_id: str,
        finished_at: datetime,
    ) -> None:
        lease_ref = self.firestore.collection("analyst_run_leases").document(
            lease_document_id(delta_id, mode, analyst_version)
        )
        transaction = self.firestore.transaction()

        @firestore.transactional
        def commit(txn: firestore.Transaction) -> None:
            snapshot = lease_ref.get(transaction=txn)
            if not snapshot.exists:
                raise KeyError(f"unknown analyst lease: {lease_ref.id}")
            record = snapshot.to_dict() or {}
            if record.get("state") == "completed" and record.get("run_id") == run_id:
                return
            if record.get("state") != "active" or record.get("run_id") != run_id:
                raise RuntimeError("analyst lease is no longer owned by this run")
            txn.update(
                lease_ref,
                {"state": "completed", "finished_at": finished_at, "error": None},
            )

        commit(transaction)

    def fail_analyst_lease(
        self,
        delta_id: str,
        mode: str,
        analyst_version: str,
        run_id: str,
        finished_at: datetime,
        error: str,
    ) -> None:
        lease_ref = self.firestore.collection("analyst_run_leases").document(
            lease_document_id(delta_id, mode, analyst_version)
        )
        transaction = self.firestore.transaction()

        @firestore.transactional
        def commit(txn: firestore.Transaction) -> None:
            snapshot = lease_ref.get(transaction=txn)
            if not snapshot.exists:
                return
            record = snapshot.to_dict() or {}
            if record.get("state") != "active" or record.get("run_id") != run_id:
                return
            txn.update(
                lease_ref,
                {"state": "failed", "finished_at": finished_at, "error": error},
            )

        commit(transaction)

    def has_completed_analyst_run(
        self, delta_id: str, mode: str, analyst_version: str
    ) -> bool:
        for snapshot in self.firestore.collection("analyst_runs").stream():
            run = snapshot.to_dict()
            if (
                run.get("delta_id") == delta_id
                and run.get("mode") == mode
                and run.get("analyst_version") == analyst_version
                and run.get("state") == "completed"
            ):
                return True
        return False

    def record_alert(
        self,
        alert_id: str,
        claim_id: str,
        delta_id: str,
        severity: str,
        kind: str,
        message: str,
        created_at: datetime,
    ) -> bool:
        try:
            self.firestore.collection("alerts").document(alert_id).create(
                {
                    "alert_id": alert_id,
                    "claim_id": claim_id,
                    "delta_id": delta_id,
                    "severity": severity,
                    "kind": kind,
                    "message": message,
                    "created_at": created_at.isoformat(),
                }
            )
        except AlreadyExists:
            return False
        return True

    def create_receipt_once(self, dedup_key: str, receipt: DeliveryReceipt) -> bool:
        dedup_ref = self.firestore.collection("delivery_dedup").document(dedup_key)
        receipt_ref = self.firestore.collection("receipts").document(receipt.receipt_id)
        transaction = self.firestore.transaction()

        @firestore.transactional
        def commit(txn: firestore.Transaction) -> bool:
            if dedup_ref.get(transaction=txn).exists:
                return False
            txn.create(receipt_ref, receipt.model_dump(mode="json"))
            txn.create(
                dedup_ref,
                {
                    "claim_id": receipt.claim_id,
                    "claim_version": receipt.claim_version,
                    "context_key": receipt.context_key,
                    "receipt_id": receipt.receipt_id,
                },
            )
            return True

        return commit(transaction)

    # --- Strategy sessions, leases, and briefs ------------------------------
    #
    # These mirror LocalBackend exactly so a strategy session behaves the same
    # locally and in Firestore.  Nothing here touches BigQuery Delta writes,
    # Pub/Sub, or GCS.

    def list_canonical_deltas(self) -> list[Delta]:
        """List canonical delta@2 rows; the archive table is never in scope."""
        query = f"""
            SELECT * FROM `{self.deltas_table}`
            WHERE schema_version = 'delta@2'
            ORDER BY computed_at
        """
        return [self._decode_delta_row(row) for row in self.bigquery.query(query).result()]

    def acquire_strategy_lease(
        self,
        period_from: datetime,
        period_to: datetime,
        strategy_version: str,
        session_id: str,
        started_at: datetime,
        lease_expires_at: datetime,
    ) -> StrategyLeaseDecision:
        lease_ref = self.firestore.collection("strategy_leases").document(
            strategy_lease_document_id(period_from, period_to, strategy_version)
        )
        transaction = self.firestore.transaction()

        @firestore.transactional
        def commit(txn: firestore.Transaction) -> StrategyLeaseDecision:
            snapshot = lease_ref.get(transaction=txn)
            if not snapshot.exists:
                txn.create(
                    lease_ref,
                    {
                        "period_from": period_from,
                        "period_to": period_to,
                        "strategy_version": strategy_version,
                        "state": "active",
                        "session_id": session_id,
                        "attempt": 1,
                        "started_at": started_at,
                        "lease_expires_at": lease_expires_at,
                    },
                )
                return StrategyLeaseDecision("acquired", session_id, 1)

            record = snapshot.to_dict() or {}
            if record.get("state") == "completed":
                return StrategyLeaseDecision(
                    "completed", record.get("session_id"), int(record.get("attempt", 0))
                )
            expires_at = record.get("lease_expires_at")
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if record.get("state") == "active" and strategy_lease_is_active(
                expires_at, started_at
            ):
                return StrategyLeaseDecision(
                    "active", record.get("session_id"), int(record.get("attempt", 0))
                )

            attempt = int(record.get("attempt", 0)) + 1
            txn.update(
                lease_ref,
                {
                    "period_from": period_from,
                    "period_to": period_to,
                    "strategy_version": strategy_version,
                    "state": "active",
                    "session_id": session_id,
                    "attempt": attempt,
                    "started_at": started_at,
                    "lease_expires_at": lease_expires_at,
                    "finished_at": None,
                    "error": None,
                },
            )
            return StrategyLeaseDecision("acquired", session_id, attempt)

        return commit(transaction)

    def complete_strategy_lease(
        self,
        period_from: datetime,
        period_to: datetime,
        strategy_version: str,
        session_id: str,
        finished_at: datetime,
    ) -> None:
        lease_ref = self.firestore.collection("strategy_leases").document(
            strategy_lease_document_id(period_from, period_to, strategy_version)
        )
        transaction = self.firestore.transaction()

        @firestore.transactional
        def commit(txn: firestore.Transaction) -> None:
            snapshot = lease_ref.get(transaction=txn)
            record = snapshot.to_dict() or {}
            if not snapshot.exists or record.get("session_id") != session_id:
                return
            txn.update(
                lease_ref,
                {"state": "completed", "finished_at": finished_at, "error": None},
            )

        commit(transaction)

    def fail_strategy_lease(
        self,
        period_from: datetime,
        period_to: datetime,
        strategy_version: str,
        session_id: str,
        finished_at: datetime,
        error: str,
    ) -> None:
        lease_ref = self.firestore.collection("strategy_leases").document(
            strategy_lease_document_id(period_from, period_to, strategy_version)
        )
        transaction = self.firestore.transaction()

        @firestore.transactional
        def commit(txn: firestore.Transaction) -> None:
            snapshot = lease_ref.get(transaction=txn)
            record = snapshot.to_dict() or {}
            if not snapshot.exists or record.get("session_id") != session_id:
                return
            txn.update(
                lease_ref,
                {"state": "failed", "finished_at": finished_at, "error": error[:500]},
            )

        commit(transaction)

    def create_strategy_session(self, session: StrategySession) -> None:
        self.firestore.collection("strategy_sessions").document(session.session_id).create(
            session.model_dump(mode="json", by_alias=True)
        )

    def finalize_strategy_session(self, session: StrategySession) -> None:
        if session.state is SessionState.RUNNING:
            raise ValueError("finalize_strategy_session requires a terminal state")
        session_ref = self.firestore.collection("strategy_sessions").document(
            session.session_id
        )
        transaction = self.firestore.transaction()

        @firestore.transactional
        def commit(txn: firestore.Transaction) -> None:
            snapshot = session_ref.get(transaction=txn)
            record = snapshot.to_dict() or {}
            if not snapshot.exists or record.get("state") != SessionState.RUNNING.value:
                raise ValueError(
                    f"strategy session {session.session_id} is not running; it is write-once"
                )
            txn.update(session_ref, session.model_dump(mode="json", by_alias=True))

        commit(transaction)

    def get_strategy_session(self, session_id: str) -> StrategySession | None:
        snapshot = self.firestore.collection("strategy_sessions").document(session_id).get()
        return StrategySession.model_validate(snapshot.to_dict()) if snapshot.exists else None

    def strategy_sessions(self) -> list[StrategySession]:
        return [
            StrategySession.model_validate(snapshot.to_dict())
            for snapshot in self.firestore.collection("strategy_sessions").stream()
        ]

    def create_brief_once(self, brief: Brief) -> bool:
        try:
            self.firestore.collection("briefs").document(brief.brief_id).create(
                brief.model_dump(mode="json", by_alias=True)
            )
        except AlreadyExists:
            return False
        return True

    def get_brief(self, brief_id: str) -> Brief | None:
        snapshot = self.firestore.collection("briefs").document(brief_id).get()
        return Brief.model_validate(snapshot.to_dict()) if snapshot.exists else None

    def briefs(self) -> list[Brief]:
        return [
            Brief.model_validate(snapshot.to_dict())
            for snapshot in self.firestore.collection("briefs").stream()
        ]

    def publish_delta(self, delta: Delta) -> str:
        if delta.schema_version is not DeltaSchemaVersion.V2:
            raise ValueError("only canonical delta@2 rows may be published")
        if delta.triage is not Triage.MEANINGFUL:
            raise ValueError("only meaningful Deltas may be published")
        topic_path = self.publisher.topic_path(self.settings.project, self.settings.topic)
        future = self.publisher.publish(
            topic_path,
            delta.model_dump_json(by_alias=True).encode(),
            entity=delta.entity,
            source=delta.source,
        )
        return future.result(timeout=30)

    def bump_verified(self, entity: str, scopes: list[str], verified_at: datetime) -> int:
        query = self.firestore.collection("claims").where(
            filter=FieldFilter("entity", "==", entity)
        )
        updated = 0
        batch = self.firestore.batch()
        for snapshot in query.stream():
            claim = snapshot.to_dict()
            if claim.get("status") == "active" and claim.get("scope") in scopes:
                batch.update(snapshot.reference, {"last_verified_at": verified_at})
                updated += 1
        if updated:
            batch.commit()
        return updated
