"""Resumable, non-publishing historical Delta@2 repair.

This command is deliberately separate from acquisition.  It reads the legacy
Delta inventory only for entity/source/observation IDs and chronology, loads the
immutable snapshots, validates saved replay proposals with the current semantic
validator, and inserts only canonical Delta@2 rows.  Saved replay outputs never
cause a provider call.  The four pairs absent from the saved reports may make at
most four bounded Gemini calls through the ``historical_backfill`` traffic path.

The manifest contains structural provenance and token/cost metadata only.  It
never stores prompts, responses, quotes, snapshots, or claim text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from google.cloud import bigquery, storage

from pipeline.cloud import CloudBackend, CloudSettings
from pipeline.semantic_differ import (
    GENERATED_BY,
    PROMPT_VERSION,
    GenerationPair,
    SemanticDeltaProposal,
    SemanticDiffer,
    bounded_error,
    build_comparison_bundle,
    construct_delta,
    run_semantic_generation,
)
from schemas.delta import Delta, DeltaSchemaVersion
from schemas.observation import Observation

REPORT_VERSION = "semantic-backfill@1"
MANIFEST_VERSION = "semantic-backfill-manifest@1"
DEFAULT_REPORT = Path("data/semantic_delta_replay.json")
DEFAULT_RETRY_REPORT = Path("data/semantic_delta_replay_retry.json")
DEFAULT_MANIFEST = Path("data/semantic_delta_backfill_manifest.json")
DEFAULT_MAX_PROVIDER_CALLS = 4
AUDIT_TABLE_NAME = "delta_audit_log_20260826"


@dataclass(frozen=True)
class LegacyPair:
    legacy_delta_id: str
    entity: str
    source: str
    obs_before: Observation
    obs_after: Observation
    computed_at: datetime

    @property
    def key(self) -> str:
        return pair_key(
            self.obs_before.obs_id,
            self.obs_after.obs_id,
            GENERATED_BY,
            PROMPT_VERSION,
        )


class BackfillError(RuntimeError):
    """The repair inventory or manifest is unsafe to continue."""


def pair_key(
    obs_before: str,
    obs_after: str,
    generated_by: str = GENERATED_BY,
    prompt_version: str = PROMPT_VERSION,
) -> str:
    """Stable JSON representation of the required manifest key tuple."""
    return json.dumps(
        [obs_before, obs_after, generated_by, prompt_version],
        separators=(",", ":"),
    )


def report_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _read_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BackfillError(f"cannot read replay report {path}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("cases"), list):
        raise BackfillError(f"replay report has an invalid shape: {path}")
    return value


def _case_key(case: dict[str, Any]) -> tuple[str, str] | None:
    before = case.get("obs_before")
    after = case.get("obs_after")
    if not isinstance(before, str) or not isinstance(after, str):
        return None
    return before, after


def _successful_saved_cases(
    report: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for case in report["cases"]:
        if not isinstance(case, dict):
            continue
        key = _case_key(case)
        if key is None or case.get("outcome") not in {"meaningful", "noise"}:
            continue
        if not isinstance(case.get("model_output"), dict):
            continue
        result[key] = case
    return result


def _download(gcs: storage.Client, content_ref: str) -> bytes:
    if not content_ref.startswith("gs://"):
        raise BackfillError("historical backfill requires immutable gs:// snapshots")
    bucket, _, object_name = content_ref[5:].partition("/")
    if not bucket or not object_name:
        raise BackfillError("invalid immutable GCS content reference")
    return gcs.bucket(bucket).blob(object_name).download_as_bytes()


def _aware_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise BackfillError("legacy Delta chronology is not a timestamp")


def load_legacy_pairs(
    bq: bigquery.Client,
    *,
    project: str,
    dataset: str,
) -> list[LegacyPair]:
    """Join the physical raw Delta table to observations; never guess IDs."""
    table = f"{project}.{dataset}"
    query = f"""
        SELECT
          d.delta_id,
          d.entity,
          d.source,
          d.obs_before,
          d.obs_after,
          d.computed_at,
          before.obs_id AS before_obs_id,
          before.entity AS before_entity,
          before.source AS before_source,
          before.kind AS before_kind,
          before.fetched_at AS before_fetched_at,
          before.content_ref AS before_content_ref,
          before.content_hash AS before_content_hash,
          before.adapter_ver AS before_adapter_ver,
          before.status AS before_status,
          after.obs_id AS after_obs_id,
          after.entity AS after_entity,
          after.source AS after_source,
          after.kind AS after_kind,
          after.fetched_at AS after_fetched_at,
          after.content_ref AS after_content_ref,
          after.content_hash AS after_content_hash,
          after.adapter_ver AS after_adapter_ver,
          after.status AS after_status
        FROM `{table}.deltas` AS d
        JOIN `{table}.observations` AS before ON before.obs_id = d.obs_before
        JOIN `{table}.observations` AS after ON after.obs_id = d.obs_after
        WHERE d.schema_version IS NULL OR d.schema_version = 'delta@1'
        ORDER BY d.computed_at, d.delta_id
    """
    rows = list(bq.query(query).result())
    pairs: list[LegacyPair] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        data = dict(row)
        before = Observation.model_validate(
            {
                "obs_id": data["before_obs_id"],
                "entity": data["before_entity"],
                "source": data["before_source"],
                "kind": data["before_kind"],
                "fetched_at": data["before_fetched_at"],
                "content_ref": data["before_content_ref"],
                "content_hash": data["before_content_hash"],
                "adapter_ver": data["before_adapter_ver"],
                "status": data["before_status"],
            }
        )
        after = Observation.model_validate(
            {
                "obs_id": data["after_obs_id"],
                "entity": data["after_entity"],
                "source": data["after_source"],
                "kind": data["after_kind"],
                "fetched_at": data["after_fetched_at"],
                "content_ref": data["after_content_ref"],
                "content_hash": data["after_content_hash"],
                "adapter_ver": data["after_adapter_ver"],
                "status": data["after_status"],
            }
        )
        if data["entity"] != before.entity or data["entity"] != after.entity:
            raise BackfillError("legacy Delta and observations disagree on entity")
        if data["source"] != before.source or data["source"] != after.source:
            raise BackfillError("legacy Delta and observations disagree on source")
        pair = (before.obs_id, after.obs_id)
        if pair in seen:
            raise BackfillError(f"duplicate legacy observation pair: {pair}")
        seen.add(pair)
        pairs.append(
            LegacyPair(
                legacy_delta_id=data["delta_id"],
                entity=data["entity"],
                source=data["source"],
                obs_before=before,
                obs_after=after,
                computed_at=_aware_datetime(data["computed_at"]),
            )
        )
    return pairs


def _manifest_key(entry: dict[str, Any]) -> str:
    return pair_key(
        entry["obs_before"],
        entry["obs_after"],
        entry["generated_by"],
        entry["prompt_version"],
    )


def _new_manifest(
    *,
    project: str,
    dataset: str,
    report: Path,
    retry_report: Path,
) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "report_version": REPORT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "project": project,
        "dataset": dataset,
        "canonical_table": f"{project}.{dataset}.deltas",
        "audit_table": f"{project}.{dataset}.{AUDIT_TABLE_NAME}",
        "generated_by": GENERATED_BY,
        "prompt_version": PROMPT_VERSION,
        "reports": {
            "replay": {"path": str(report), "sha256": report_sha256(report)},
            "retry": {
                "path": str(retry_report),
                "sha256": report_sha256(retry_report),
            },
        },
        "provider_call_limit": DEFAULT_MAX_PROVIDER_CALLS,
        "provider_calls": 0,
        "entries": {},
    }


def _atomic_write(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = datetime.now(UTC).isoformat()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _load_or_create_manifest(
    path: Path,
    *,
    project: str,
    dataset: str,
    report: Path,
    retry_report: Path,
    resume: bool,
) -> dict[str, Any]:
    if path.exists():
        try:
            manifest = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BackfillError(f"cannot load manifest {path}") from exc
        if manifest.get("manifest_version") != MANIFEST_VERSION:
            raise BackfillError(f"unsupported manifest version in {path}")
        if manifest.get("project") != project or manifest.get("dataset") != dataset:
            raise BackfillError("manifest project/dataset does not match this run")
        if not resume:
            raise BackfillError(f"manifest exists; pass --resume to continue: {path}")
        return manifest
    if resume:
        raise BackfillError(f"--resume requested but manifest is missing: {path}")
    return _new_manifest(
        project=project,
        dataset=dataset,
        report=report,
        retry_report=retry_report,
    )


def _safe_usage(case: dict[str, Any]) -> dict[str, int | float]:
    usage = case.get("usage")
    if not isinstance(usage, dict):
        return {}
    result: dict[str, int | float] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
        "total_tokens",
        "estimated_cost_usd",
    ):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = value
    return result


def _manifest_entry(
    pair: LegacyPair,
    *,
    legacy_report_sha: str,
    retry_report_sha: str,
    source: str,
    state: str,
    outcome: str | None = None,
    delta_id: str | None = None,
    validation: str | None = None,
    usage: dict[str, int | float] | None = None,
    error_class: str | None = None,
    error: str | None = None,
    provider_call: bool = False,
) -> dict[str, Any]:
    return {
        "key": pair.key,
        "obs_before": pair.obs_before.obs_id,
        "obs_after": pair.obs_after.obs_id,
        "generated_by": GENERATED_BY,
        "prompt_version": PROMPT_VERSION,
        "legacy_delta_id": pair.legacy_delta_id,
        "entity": pair.entity,
        "source": pair.source,
        "computed_at": pair.computed_at.isoformat(),
        "source_kind": source,
        "historical_replay_import": source in {"saved_replay", "retry_replay"},
        "legacy_report_sha256": legacy_report_sha,
        "retry_report_sha256": retry_report_sha,
        "state": state,
        "outcome": outcome,
        "validation": validation,
        "delta_id": delta_id,
        "usage": usage or {},
        "provider_call": provider_call,
        "error_class": error_class,
        "error": error,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _validated_saved_delta(
    pair: LegacyPair,
    case: dict[str, Any],
    *,
    gcs: storage.Client,
) -> tuple[Delta, dict[str, int | float], int, int]:
    before_payload = _download(gcs, pair.obs_before.content_ref)
    after_payload = _download(gcs, pair.obs_after.content_ref)
    bundle = build_comparison_bundle(
        pair.entity,
        pair.source,
        before_payload,
        after_payload,
        obs_before=pair.obs_before.obs_id,
        obs_after=pair.obs_after.obs_id,
    )
    proposal = SemanticDeltaProposal.from_model_output(case["model_output"])
    delta = construct_delta(
        proposal,
        entity=pair.entity,
        source=pair.source,
        obs_before=pair.obs_before.obs_id,
        obs_after=pair.obs_after.obs_id,
        computed_at=pair.computed_at,
        generated_by=GENERATED_BY,
        prompt_version=PROMPT_VERSION,
        before_snapshot=bundle.before,
        after_snapshot=bundle.after,
    )
    expected_comparison = case.get("comparison_id")
    if expected_comparison and delta.comparison_id != expected_comparison:
        raise BackfillError("saved replay comparison identity does not match the pair")
    return (
        delta,
        _safe_usage(case),
        bundle.input_bytes,
        bundle.estimated_input_tokens,
    )


def _record_import_metadata(
    backend: CloudBackend,
    *,
    delta: Delta,
    legacy_delta_id: str,
    usage: dict[str, int | float],
    input_bytes: int,
    estimated_input_tokens: int,
    source: str,
) -> None:
    recorder = getattr(backend, "record_historical_delta_import", None)
    if callable(recorder):
        recorder(
            delta=delta,
            legacy_delta_id=legacy_delta_id,
            usage=usage,
            input_bytes=input_bytes,
            estimated_input_tokens=estimated_input_tokens,
            source=source,
        )


def _checkpoint_entry(
    manifest: dict[str, Any],
    manifest_path: Path,
    entry: dict[str, Any],
) -> None:
    manifest.setdefault("entries", {})[entry["key"]] = entry
    _atomic_write(manifest_path, manifest)


def _canonical_existing(backend: CloudBackend, comparison_id: str) -> Delta | None:
    existing = backend.find_delta_by_comparison_id(comparison_id)
    if existing is not None and existing.schema_version is not DeltaSchemaVersion.V2:
        raise BackfillError("canonical lookup returned a non-v2 row")
    return existing


def _import_saved_pair(
    pair: LegacyPair,
    case: dict[str, Any],
    *,
    backend: CloudBackend | None,
    gcs: storage.Client,
    manifest: dict[str, Any],
    manifest_path: Path,
    report_sha: str,
    retry_report_sha: str,
    source: str,
    apply: bool,
) -> dict[str, Any]:
    existing_entry = manifest.get("entries", {}).get(pair.key)
    if existing_entry and existing_entry.get("state") == "inserted" and backend is None:
        return existing_entry
    delta, usage, input_bytes, estimated_tokens = _validated_saved_delta(
        pair, case, gcs=gcs
    )
    if not apply:
        entry = _manifest_entry(
            pair,
            legacy_report_sha=report_sha,
            retry_report_sha=retry_report_sha,
            source=source,
            state="validated",
            outcome=delta.triage.value,
            validation="historical_replay_import",
            usage=usage,
        )
        _checkpoint_entry(manifest, manifest_path, entry)
        return entry
    if backend is None:
        raise BackfillError("apply requires a CloudBackend")
    existing = _canonical_existing(backend, delta.comparison_id or "")
    if existing is None:
        backend.insert_delta(delta, enqueue=False)
        existing = delta
    _record_import_metadata(
        backend,
        delta=existing,
        legacy_delta_id=pair.legacy_delta_id,
        usage=usage,
        input_bytes=input_bytes,
        estimated_input_tokens=estimated_tokens,
        source=source,
    )
    entry = _manifest_entry(
        pair,
        legacy_report_sha=report_sha,
        retry_report_sha=retry_report_sha,
        source=source,
        state="inserted",
        outcome=existing.triage.value,
        delta_id=existing.delta_id,
        validation="historical_replay_import",
        usage=usage,
    )
    _checkpoint_entry(manifest, manifest_path, entry)
    return entry


def _run_missing_pair(
    pair: LegacyPair,
    *,
    backend: CloudBackend | None,
    differ: SemanticDiffer | None,
    manifest: dict[str, Any],
    manifest_path: Path,
    report_sha: str,
    retry_report_sha: str,
    apply: bool,
    provider_calls_left: int,
) -> tuple[dict[str, Any], int]:
    previous = manifest.get("entries", {}).get(pair.key)
    if previous and previous.get("state") == "inserted":
        return previous, 0
    if not apply:
        entry = _manifest_entry(
            pair,
            legacy_report_sha=report_sha,
            retry_report_sha=retry_report_sha,
            source="generated_missing_pair",
            state="pending_provider_call",
            validation="not_run_dry_run",
        )
        _checkpoint_entry(manifest, manifest_path, entry)
        return entry, 0
    if provider_calls_left <= 0:
        entry = _manifest_entry(
            pair,
            legacy_report_sha=report_sha,
            retry_report_sha=retry_report_sha,
            source="generated_missing_pair",
            state="retryable",
            validation="not_run_call_limit",
            error_class="ProviderCallLimit",
            error="historical backfill call limit reached",
        )
        _checkpoint_entry(manifest, manifest_path, entry)
        return entry, 0
    if backend is None or differ is None:
        raise BackfillError("missing-pair apply requires backend and semantic differ")
    try:
        result = run_semantic_generation(
            GenerationPair(
                pair.obs_before,
                pair.obs_after,
                generated_by=GENERATED_BY,
                prompt_version=PROMPT_VERSION,
            ),
            backend=backend,
            differ=differ,
            mode="historical_backfill",
            now=pair.computed_at,
        )
        calls = 1
        manifest["provider_calls"] = int(manifest.get("provider_calls", 0)) + calls
        if result.delta is not None and result.state == "completed":
            entry = _manifest_entry(
                pair,
                legacy_report_sha=report_sha,
                retry_report_sha=retry_report_sha,
                source="generated_missing_pair",
                state="inserted",
                outcome=result.delta.triage.value,
                delta_id=result.delta.delta_id,
                validation="historical_backfill",
                usage=result.usage,
                provider_call=True,
            )
        else:
            entry = _manifest_entry(
                pair,
                legacy_report_sha=report_sha,
                retry_report_sha=retry_report_sha,
                source="generated_missing_pair",
                state="retryable",
                validation=result.validation,
                usage=result.usage,
                error_class="SemanticGenerationFailed",
                error="provider or validation failure; pair remains retryable",
                provider_call=True,
            )
    except Exception as exc:
        error_class, error_message = bounded_error(exc)
        manifest["provider_calls"] = int(manifest.get("provider_calls", 0)) + 1
        entry = _manifest_entry(
            pair,
            legacy_report_sha=report_sha,
            retry_report_sha=retry_report_sha,
            source="generated_missing_pair",
            state="retryable",
            validation="failed",
            error_class=error_class,
            error=error_message,
            provider_call=True,
        )
        calls = 1
    _checkpoint_entry(manifest, manifest_path, entry)
    return entry, calls


def _bounded_summary(
    pairs: Iterable[LegacyPair],
    entries: Iterable[dict[str, Any]],
    *,
    apply: bool,
    manifest_path: Path,
) -> dict[str, Any]:
    pair_list = list(pairs)
    entry_list = list(entries)
    return {
        "report_version": REPORT_VERSION,
        "mode": "apply" if apply else "dry-run",
        "legacy_pairs": len(pair_list),
        "saved_replay_entries": sum(
            item.get("source_kind") in {"saved_replay", "retry_replay"}
            for item in entry_list
        ),
        "generated_missing_entries": sum(
            item.get("source_kind") == "generated_missing_pair" for item in entry_list
        ),
        "validated_entries": sum(item.get("state") in {"validated", "inserted"} for item in entry_list),
        "inserted_entries": sum(item.get("state") == "inserted" for item in entry_list),
        "meaningful": sum(item.get("outcome") == "meaningful" for item in entry_list),
        "noise": sum(item.get("outcome") == "noise" for item in entry_list),
        "retryable": sum(item.get("state") == "retryable" for item in entry_list),
        "provider_calls": sum(bool(item.get("provider_call")) for item in entry_list),
        "unresolved_legacy_delta_ids": [
            item["legacy_delta_id"]
            for item in entry_list
            if item.get("state") == "retryable"
        ],
        "manifest": str(manifest_path),
    }


def run_backfill(
    *,
    project: str,
    dataset: str,
    report: Path,
    retry_report: Path,
    manifest_path: Path,
    apply: bool,
    resume: bool,
    max_provider_calls: int = DEFAULT_MAX_PROVIDER_CALLS,
) -> dict[str, Any]:
    if not report.exists() or not retry_report.exists():
        raise BackfillError("both replay report paths must exist")
    report_data = _read_report(report)
    retry_data = _read_report(retry_report)
    report_sha = report_sha256(report)
    retry_report_sha = report_sha256(retry_report)
    retry_saved = _successful_saved_cases(retry_data)
    saved = _successful_saved_cases(report_data)
    saved.update(retry_saved)

    bq = bigquery.Client(project=project)
    gcs = storage.Client(project=project)
    pairs = load_legacy_pairs(bq, project=project, dataset=dataset)
    manifest = _load_or_create_manifest(
        manifest_path,
        project=project,
        dataset=dataset,
        report=report,
        retry_report=retry_report,
        resume=resume,
    )
    manifest["provider_call_limit"] = max_provider_calls
    _atomic_write(manifest_path, manifest)

    backend: CloudBackend | None = None
    differ: SemanticDiffer | None = None
    if apply:
        backend = CloudBackend(
            CloudSettings(
                project=project,
                bucket=os.getenv("TYCHO_BUCKET", f"{project}-tycho-raw"),
                dataset=dataset,
            )
        )
        differ = SemanticDiffer(project=project)

    entries: list[dict[str, Any]] = []
    missing: list[LegacyPair] = []
    for pair in pairs:
        case = saved.get((pair.obs_before.obs_id, pair.obs_after.obs_id))
        if case is None:
            missing.append(pair)
            continue
        source = "retry_replay" if (pair.obs_before.obs_id, pair.obs_after.obs_id) in retry_saved else "saved_replay"
        entry = _import_saved_pair(
            pair,
            case,
            backend=backend,
            gcs=gcs,
            manifest=manifest,
            manifest_path=manifest_path,
            report_sha=report_sha,
            retry_report_sha=retry_report_sha,
            source=source,
            apply=apply,
        )
        entries.append(entry)

    # Missing pairs are derived from the joined legacy inventory above. Never
    # replace this with a hard-coded list of observation or Delta IDs.
    calls_used = int(manifest.get("provider_calls", 0))
    for pair in missing:
        entry, calls = _run_missing_pair(
            pair,
            backend=backend,
            differ=differ,
            manifest=manifest,
            manifest_path=manifest_path,
            report_sha=report_sha,
            retry_report_sha=retry_report_sha,
            apply=apply,
            provider_calls_left=max_provider_calls - calls_used,
        )
        entries.append(entry)
        calls_used += calls

    manifest["missing_pair_count"] = len(missing)
    manifest["legacy_pair_count"] = len(pairs)
    _atomic_write(manifest_path, manifest)
    return _bounded_summary(pairs, entries, apply=apply, manifest_path=manifest_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--dataset", default="tycho")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--retry-report", type=Path, default=DEFAULT_RETRY_REPORT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and checkpoint locally without BigQuery inserts or Gemini calls",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="insert validated rows and run at most the four missing-pair calls",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-provider-calls", type=int, default=DEFAULT_MAX_PROVIDER_CALLS)
    args = parser.parse_args()
    if args.dry_run and args.apply:
        parser.error("--dry-run and --apply are mutually exclusive")
    if args.max_provider_calls < 0 or args.max_provider_calls > DEFAULT_MAX_PROVIDER_CALLS:
        parser.error("--max-provider-calls must be between 0 and 4")
    apply = bool(args.apply)
    try:
        summary = run_backfill(
            project=args.project,
            dataset=args.dataset,
            report=args.report,
            retry_report=args.retry_report,
            manifest_path=args.manifest,
            apply=apply,
            resume=args.resume,
            max_provider_calls=args.max_provider_calls,
        )
    except BackfillError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
