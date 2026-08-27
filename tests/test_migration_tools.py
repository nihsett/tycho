import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from google.api_core.exceptions import NotFound

from infra.backfill_semantic_deltas import (
    LegacyPair,
    _import_saved_pair,
    _load_or_create_manifest,
)
from infra.cutover_semantic_deltas import (
    CutoverError,
    rename_table,
    run_cutover,
    subscription_backlog_readback,
)
from infra.migrate_legacy_claims import _retire_claim
from pipeline.local_backend import LocalBackend, LocalSettings
from pipeline.semantic_differ import GenerationPair, run_semantic_generation
from schemas.claim import Evidence
from schemas.config import load_config
from schemas.delta import Delta
from schemas.observation import Observation, ObservationKind, ObservationStatus
from tests.semantic_test_helpers import FakeSemanticDiffer


FIXTURES = Path("schemas/fixtures")


def _observation(obs_id: str, ref: str, *, entity: str = "pi") -> Observation:
    return Observation(
        obs_id=obs_id,
        entity=entity,
        source="github_releases",
        kind=ObservationKind.STRUCTURED,
        fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
        content_ref=ref,
        content_hash="sha256:" + "a" * 64,
        adapter_ver="github@1",
        status=ObservationStatus.OK,
    )


class _Blob:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def download_as_bytes(self) -> bytes:
        return self.payload


class _Bucket:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def blob(self, name: str) -> _Blob:
        return _Blob(self.payloads[name])


class _Gcs:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.bucket_data = _Bucket(payloads)

    def bucket(self, name: str) -> _Bucket:
        assert name == "bucket"
        return self.bucket_data


class _ImportBackend:
    def __init__(self) -> None:
        self.inserted: list[tuple[Delta, bool]] = []
        self.import_metadata: list[dict] = []
        self.existing: Delta | None = None

    def find_delta_by_comparison_id(self, comparison_id: str) -> Delta | None:
        return self.existing if self.existing and self.existing.comparison_id == comparison_id else None

    def insert_delta(self, delta: Delta, *, enqueue: bool) -> None:
        self.inserted.append((delta, enqueue))
        self.existing = delta

    def record_historical_delta_import(self, **metadata) -> None:
        self.import_metadata.append(metadata)


def _legacy_pair() -> LegacyPair:
    before = _observation("obs_01ARZ3NDEKTSV4RRFFQ69G5FAV", "gs://bucket/before.json")
    after = _observation("obs_01ARZ3NDEKTSV4RRFFQ69G5FAW", "gs://bucket/after.json")
    return LegacyPair(
        legacy_delta_id="dlt_01ARZ3NDEKTSV4RRFFQ69G5FAX",
        entity="pi",
        source="github_releases",
        obs_before=before,
        obs_after=after,
        computed_at=datetime(2026, 8, 20, 2, 3, 11, tzinfo=UTC),
    )


def _saved_case() -> dict:
    return {
        "outcome": "meaningful",
        "model_output": {
            "status": "meaningful",
            "summary": "Pi added a durable capability.",
            "reason": "The source states a durable capability change.",
            "changes": [
                {
                    "category": "capability",
                    "scope": "product/capabilities",
                    "statement": "Pi added a durable capability.",
                    "before": "",
                    "after": "Added a durable capability.",
                    "evidence_before": "",
                    "evidence_after": "Added a durable capability.",
                }
            ],
        },
    }


def test_backfill_checkpoint_and_resume_are_idempotent(tmp_path):
    pair = _legacy_pair()
    gcs = _Gcs(
        {
            "before.json": b'[{"tag_name":"v1","body":"old"}]',
            "after.json": b'[{"tag_name":"v2","body":"Added a durable capability."}]',
        }
    )
    manifest = {"entries": {}}
    manifest_path = tmp_path / "manifest.json"
    backend = _ImportBackend()

    first = _import_saved_pair(
        pair,
        _saved_case(),
        backend=None,
        gcs=gcs,
        manifest=manifest,
        manifest_path=manifest_path,
        report_sha="sha256:report",
        retry_report_sha="sha256:retry",
        source="saved_replay",
        apply=False,
    )
    assert first["state"] == "validated"
    assert backend.inserted == []

    second = _import_saved_pair(
        pair,
        _saved_case(),
        backend=backend,
        gcs=gcs,
        manifest=manifest,
        manifest_path=manifest_path,
        report_sha="sha256:report",
        retry_report_sha="sha256:retry",
        source="saved_replay",
        apply=True,
    )
    assert second["state"] == "inserted"
    assert backend.inserted[0][1] is False
    assert backend.import_metadata[0]["source"] == "saved_replay"

    third = _import_saved_pair(
        pair,
        _saved_case(),
        backend=backend,
        gcs=gcs,
        manifest=manifest,
        manifest_path=manifest_path,
        report_sha="sha256:report",
        retry_report_sha="sha256:retry",
        source="saved_replay",
        apply=True,
    )
    assert third["delta_id"] == second["delta_id"]
    assert len(backend.inserted) == 1


def test_backfill_manifest_requires_resume_after_checkpoint(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    created = _load_or_create_manifest(
        manifest_path,
        project="project",
        dataset="tycho",
        report=FIXTURES / "delta.example.json",
        retry_report=FIXTURES / "delta.semantic.meaningful.example.json",
        resume=False,
    )
    manifest_path.write_text(json.dumps(created))
    with pytest.raises(Exception, match="pass --resume"):
        _load_or_create_manifest(
            manifest_path,
            project="project",
            dataset="tycho",
            report=FIXTURES / "delta.example.json",
            retry_report=FIXTURES / "delta.semantic.meaningful.example.json",
            resume=False,
        )
    resumed = _load_or_create_manifest(
        manifest_path,
        project="project",
        dataset="tycho",
        report=FIXTURES / "delta.example.json",
        retry_report=FIXTURES / "delta.semantic.meaningful.example.json",
        resume=True,
    )
    assert resumed["manifest_version"] == "semantic-backfill-manifest@1"


def test_historical_backfill_never_enqueues_or_publishes(tmp_path):
    config = load_config("tycho.yaml")
    before_payload = b'[{"tag_name":"v1","body":"old"}]'
    after_payload = b'[{"tag_name":"v2","body":"Added a durable capability."}]'
    before = _observation(
        "obs_01ARZ3NDEKTSV4RRFFQ69G5FAV", "file://before.json"
    )
    after = _observation(
        "obs_01ARZ3NDEKTSV4RRFFQ69G5FAW", "file://after.json"
    )

    with LocalBackend(config, LocalSettings(tmp_path / "data")) as backend:
        before = before.model_copy(
            update={"content_ref": backend.put_raw("pi", "github_releases", before.obs_id, before_payload)}
        )
        after = after.model_copy(
            update={"content_ref": backend.put_raw("pi", "github_releases", after.obs_id, after_payload)}
        )
        backend.insert_observation(before)
        backend.insert_observation(after)
        result = run_semantic_generation(
            GenerationPair(before, after),
            backend=backend,
            differ=FakeSemanticDiffer(),
            mode="historical_backfill",
            now=datetime(2026, 8, 20, tzinfo=UTC),
        )
        assert result.state == "completed"
        assert result.delta is not None
        assert backend.pending_count() == 0
        assert backend.process_pending() == []


def test_claim_migration_retirement_preserves_legacy_history(tmp_path):
    config = load_config("tycho.yaml")
    delta = Delta.model_validate_json((FIXTURES / "delta.example.json").read_text())
    from pipeline.analyst import build_stub_claim

    claim = build_stub_claim(delta, config).model_copy(
        update={
            "evidence": [
                Evidence(
                    delta_id="dlt_01ARZ3NDEKTSV4RRFFQ69G5FAX",
                    source="github_releases",
                    note="archived legacy evidence",
                )
            ]
        }
    )

    class Store:
        def __init__(self):
            self.updated = []

        def update_claim(self, claim_id, fields):
            self.updated.append((claim_id, fields))

    store = Store()
    _retire_claim(
        store,
        claim,
        actor="test-migration",
        reason="mapped v2 noise",
        mapped_delta_ids=[delta.delta_id],
    )
    fields = store.updated[0][1]
    assert fields["status"] == "retired"
    assert fields["history"][-1]["mapped_v2_delta_ids"] == [delta.delta_id]
    assert fields["history"][-1]["previous_state"]["evidence_delta_ids"] == [
        "dlt_01ARZ3NDEKTSV4RRFFQ69G5FAX"
    ]


class _RenameJob:
    def result(self):
        return None


class _RenameClient:
    def __init__(self, error: Exception | None = None):
        self.queries = []
        self.error = error

    def get_table(self, table):
        raise NotFound(table)

    def query(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        return _RenameJob()


def test_pubsub_backlog_readback_requires_recent_zero_metric(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "timeSeries": [
                    {
                        "points": [
                            {"value": {"int64Value": "0"}},
                            {"value": {"int64Value": "0"}},
                        ]
                    }
                ]
            }

    class Session:
        def __init__(self, credentials):
            assert credentials == "adc"

        def get(self, url, *, params, timeout):
            assert "monitoring.googleapis.com" in url
            assert params["view"] == "FULL"
            assert timeout == 60
            return Response()

    monkeypatch.setattr(
        "infra.cutover_semantic_deltas.google.auth.default",
        lambda scopes: ("adc", "project"),
    )
    monkeypatch.setattr("infra.cutover_semantic_deltas.AuthorizedSession", Session)

    result = subscription_backlog_readback("project", "subscription")

    assert result["zero_pending_messages"] is True
    assert result["observed_values"] == [0, 0]


def test_table_swap_uses_same_dataset_destination_name():
    client = _RenameClient()
    rename_table(client, "project.tycho.deltas", "delta_audit_log_20260826")
    assert client.queries == [
        "ALTER TABLE `project.tycho.deltas` RENAME TO `delta_audit_log_20260826`"
    ]


def test_table_swap_reports_streaming_blocker_without_fallback():
    client = _RenameClient(Exception("Cannot rename table because it has streaming data"))
    with pytest.raises(CutoverError, match="streaming data"):
        rename_table(client, "project.tycho.deltas", "delta_audit_log_20260826")
    assert client.queries


class _CutoverBigQueryClient:
    """Minimal BigQuery stand-in whose only state is the set of table names."""

    def __init__(self, tables: set[str]) -> None:
        self.tables = set(tables)

    def get_table(self, table):
        if table.rsplit(".", 1)[-1] not in self.tables:
            raise NotFound(table)
        return object()


class _CutoverCloud:
    """Fake cloud for `run_cutover`: table renames plus Scheduler transitions."""

    def __init__(self, tables: set[str], *, rename_failures: dict[str, str] | None = None):
        self.client = _CutoverBigQueryClient(tables)
        self.rename_failures = dict(rename_failures or {})
        self.renames: list[tuple[str, str]] = []
        self.scheduler_calls: list[str] = []

    # --- monkeypatch targets -------------------------------------------------
    def bigquery_client(self, project):
        return self.client

    def pause(self, project, region, scheduler):
        self.scheduler_calls.append("pause")

    def resume(self, project, region, scheduler):
        self.scheduler_calls.append("resume")

    def scheduler_readback(self, project, region, scheduler):
        state = "PAUSED" if self.scheduler_calls[-1:] == ["pause"] else "ENABLED"
        return {"name": scheduler, "state": state, "schedule": "0 2 * * *"}

    def rename_table(self, client, source, destination_name):
        short = source.rsplit(".", 1)[-1]
        blocker = self.rename_failures.get(short)
        if blocker is not None:
            raise CutoverError(blocker)
        self.client.tables.discard(short)
        self.client.tables.add(destination_name)
        self.renames.append((short, destination_name))

    def inventory(self, client, table):
        return {"table": table, "row_count": 50}

    def invariants(self, client, table):
        return {
            "rows": 50,
            "bad_schema_version": 0,
            "bad_diff_kind": 0,
            "bad_generator": 0,
            "bad_prompt": 0,
            "missing_comparison": 0,
            "bad_meaningful_bounds": 0,
            "meaningful_without_scope": 0,
            "bad_noise": 0,
            "duplicate_comparison_ids": 0,
        }

    def pair_invariants(self, client, *, raw_table, canonical_table, observations_table):
        return {
            "legacy_pairs": 46,
            "missing_or_wrong_replacements": 0,
            "duplicate_pairs": 0,
            "bad_observation_joins": 0,
        }

    def subscription(self, project, subscription):
        return {"name": subscription, "push_endpoint": "https://dispatcher"}

    def backlog(self, project, subscription, **kwargs):
        return {"observed_values": [0], "zero_pending_messages": True}

    def acquisition(self, project, region):
        return {"active": [], "observed_executions": 9}


def _install_cutover_cloud(monkeypatch, cloud: _CutoverCloud) -> None:
    module = "infra.cutover_semantic_deltas"
    monkeypatch.setattr(f"{module}.bigquery.Client", cloud.bigquery_client)
    monkeypatch.setattr(f"{module}.scheduler_pause", cloud.pause)
    monkeypatch.setattr(f"{module}.scheduler_resume", cloud.resume)
    monkeypatch.setattr(f"{module}.scheduler_readback", cloud.scheduler_readback)
    monkeypatch.setattr(f"{module}.rename_table", cloud.rename_table)
    monkeypatch.setattr(f"{module}.table_inventory", cloud.inventory)
    monkeypatch.setattr(f"{module}.canonical_invariants", cloud.invariants)
    monkeypatch.setattr(f"{module}.pair_invariants", cloud.pair_invariants)
    monkeypatch.setattr(f"{module}.subscription_readback", cloud.subscription)
    monkeypatch.setattr(f"{module}.subscription_backlog_readback", cloud.backlog)
    monkeypatch.setattr(f"{module}.verify_no_active_acquisition", cloud.acquisition)
    monkeypatch.setattr(f"{module}.ensure_candidate", lambda *a, **k: None)
    monkeypatch.setattr(f"{module}.copy_validated_v2_rows", lambda *a, **k: None)


def _run_apply(evidence_path: Path):
    return run_cutover(
        project="project",
        dataset="tycho",
        region="us-central1",
        scheduler="tycho-nightly",
        subscription="tycho-analyst-push",
        candidate_name="deltas_v2_candidate",
        audit_name="delta_audit_log_20260826",
        evidence_path=evidence_path,
        apply=True,
        resume=True,
    )


def test_cutover_resumes_scheduler_when_streaming_blocks_the_first_rename(
    monkeypatch, tmp_path
):
    """The original blocker: nothing renamed, so `deltas` is still authoritative."""
    cloud = _CutoverCloud(
        {"deltas", "deltas_v2_candidate", "observations"},
        rename_failures={
            "deltas": "BigQuery table rename is blocked by streaming data; "
            "source=project.tycho.deltas"
        },
    )
    _install_cutover_cloud(monkeypatch, cloud)
    evidence_path = tmp_path / "cutover.json"

    with pytest.raises(CutoverError, match="streaming data"):
        _run_apply(evidence_path)

    assert cloud.renames == []
    assert cloud.client.tables >= {"deltas", "deltas_v2_candidate"}
    assert "delta_audit_log_20260826" not in cloud.client.tables
    assert cloud.scheduler_calls == ["pause", "resume"]

    evidence = json.loads(evidence_path.read_text())
    assert evidence["scheduler_resumed"] is True
    assert evidence["scheduler_final"]["state"] == "ENABLED"
    assert "archive_renamed" not in evidence


def test_cutover_leaves_scheduler_paused_when_second_rename_fails(monkeypatch, tmp_path):
    """Archive rename succeeded, so canonical `deltas` is absent: stay paused."""
    cloud = _CutoverCloud(
        {"deltas", "deltas_v2_candidate", "observations"},
        rename_failures={"deltas_v2_candidate": "rename destination already exists"},
    )
    _install_cutover_cloud(monkeypatch, cloud)
    evidence_path = tmp_path / "cutover.json"

    with pytest.raises(CutoverError, match="rename destination already exists"):
        _run_apply(evidence_path)

    assert cloud.renames == [("deltas", "delta_audit_log_20260826")]
    assert "deltas" not in cloud.client.tables
    assert {"delta_audit_log_20260826", "deltas_v2_candidate"} <= cloud.client.tables
    assert cloud.scheduler_calls == ["pause"]

    evidence = json.loads(evidence_path.read_text())
    assert evidence["scheduler_resumed"] is False
    assert evidence["scheduler_left_paused"] is True
    assert evidence["archive_renamed"] is True
    assert evidence["scheduler_final"]["state"] == "PAUSED"
    state = evidence["resumable_state"]
    assert state["canonical_exists"] is False
    assert state["candidate_exists"] is True
    assert state["audit_exists"] is True
    assert "--apply --resume" in evidence["resume_command"]


def test_partial_cutover_state_remains_resumable(monkeypatch, tmp_path):
    """The paused partial state completes on the documented resume path."""
    failing = _CutoverCloud(
        {"deltas", "deltas_v2_candidate", "observations"},
        rename_failures={"deltas_v2_candidate": "rename destination already exists"},
    )
    _install_cutover_cloud(monkeypatch, failing)
    evidence_path = tmp_path / "cutover.json"
    with pytest.raises(CutoverError):
        _run_apply(evidence_path)
    assert failing.scheduler_calls == ["pause"]

    resumed = _CutoverCloud(failing.client.tables)
    _install_cutover_cloud(monkeypatch, resumed)
    evidence = _run_apply(evidence_path)

    assert resumed.renames == [("deltas_v2_candidate", "deltas")]
    assert "deltas" in resumed.client.tables
    assert "delta_audit_log_20260826" in resumed.client.tables
    assert "deltas_v2_candidate" not in resumed.client.tables
    assert resumed.scheduler_calls == ["pause", "resume"]
    assert evidence["candidate_renamed"] is True
    assert "archive_renamed" not in evidence
    assert evidence["scheduler_resumed"] is True
    assert evidence["scheduler_final"]["state"] == "ENABLED"

    stored = json.loads(evidence_path.read_text())
    assert stored["scheduler_resumed"] is True
    assert "resumable_state" not in stored
