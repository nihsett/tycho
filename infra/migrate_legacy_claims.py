"""Governed, resumable retirement/supersession of legacy-backed claims.

Only active claims that cite an archived Delta@1 are selected.  Legacy evidence
is never rewritten in place.  Claims whose complete mapped evidence is semantic
noise are retired with an auditable history event.  Claims with mapped
meaningful Delta@2 evidence are processed by the existing analyst in explicit
``migration`` mode; that mode writes governed claims directly, publishes no
Pub/Sub message, and emits no external alert.  Any remaining active legacy
claim is then retired only after the bounded migration analyst run completed.

The report and manifest contain IDs, states, action names, and counts only.  They
do not contain claim statements, prompts, responses, quotes, or snapshots.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.cloud import CloudBackend, CloudSettings
from pipeline.gemini_analyst import run_analyst
from pipeline.semantic_differ import configure_vertex_adc
from schemas.claim import Claim, ClaimStatus
from schemas.config import load_config
from schemas.delta import Delta, DeltaSchemaVersion, Triage

MANIFEST_VERSION = "legacy-claim-migration@1"
DEFAULT_MANIFEST = Path("data/semantic_delta_backfill_manifest.json")
DEFAULT_REPORT = Path("data/legacy_claim_migration.json")


class ClaimMigrationError(RuntimeError):
    """The legacy claim map is incomplete or unsafe."""


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _load_manifest(path: Path, *, resume: bool) -> dict[str, Any]:
    if not path.exists():
        raise ClaimMigrationError(f"missing semantic backfill manifest: {path}")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaimMigrationError(f"cannot read semantic backfill manifest: {path}") from exc
    if value.get("manifest_version") != "semantic-backfill-manifest@1":
        raise ClaimMigrationError("unsupported semantic backfill manifest")
    if not resume and value.get("claim_migration_started"):
        raise ClaimMigrationError("claim migration already started; pass --resume")
    return value


def _load_or_create_report(path: Path, *, resume: bool) -> dict[str, Any]:
    if path.exists():
        if not resume:
            raise ClaimMigrationError(f"report exists; pass --resume: {path}")
        value = json.loads(path.read_text())
        if value.get("manifest_version") != MANIFEST_VERSION:
            raise ClaimMigrationError("unsupported claim migration report")
        return value
    return {
        "manifest_version": MANIFEST_VERSION,
        "started_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "entries": {},
    }


def _legacy_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for entry in (manifest.get("entries") or {}).values():
        if not isinstance(entry, dict):
            continue
        if entry.get("state") != "inserted":
            continue
        legacy_id = entry.get("legacy_delta_id")
        delta_id = entry.get("delta_id")
        if isinstance(legacy_id, str) and isinstance(delta_id, str):
            result[legacy_id] = entry
    return result


def _safe_claim_state(claim: Claim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "entity": claim.entity,
        "scope": claim.scope,
        "status": claim.status.value,
        "version": claim.version,
        "evidence_delta_ids": [item.delta_id for item in claim.evidence],
    }


def _history_event(
    claim: Claim,
    *,
    action: str,
    actor: str,
    reason: str,
    mapped_delta_ids: list[str],
    previous_state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event": "legacy_evidence_migration",
        "action": action,
        "actor": actor,
        "at": datetime.now(UTC).isoformat(),
        "reason": reason,
        "mapped_v2_delta_ids": mapped_delta_ids,
        "previous_state": previous_state,
        "claim_id": claim.claim_id,
    }


def _retire_claim(
    backend: CloudBackend,
    claim: Claim,
    *,
    actor: str,
    reason: str,
    mapped_delta_ids: list[str],
) -> None:
    if claim.status is not ClaimStatus.ACTIVE:
        return
    previous_state = _safe_claim_state(claim)
    event = _history_event(
        claim,
        action="retired",
        actor=actor,
        reason=reason,
        mapped_delta_ids=mapped_delta_ids,
        previous_state=previous_state,
    )
    history = [*claim.history, event][-20:]
    backend.update_claim(
        claim.claim_id,
        {
            "status": ClaimStatus.RETIRED.value,
            "superseded_by": None,
            "history": history,
        },
    )


def _record_supersession_history(
    backend: CloudBackend,
    claim: Claim,
    *,
    actor: str,
    mapped_delta_ids: list[str],
) -> None:
    current = backend.get_claim(claim.claim_id)
    if current is None:
        return
    event = _history_event(
        claim,
        action="superseded",
        actor=actor,
        reason="governed migration produced a canonical v2-backed replacement",
        mapped_delta_ids=mapped_delta_ids,
        previous_state=_safe_claim_state(claim),
    )
    backend.update_claim(
        claim.claim_id,
        {"history": [*current.history, event][-20:]},
    )


def _claim_entry(
    claim: Claim,
    *,
    legacy_ids: list[str],
    mapped_ids: list[str],
    state: str,
    action_names: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "entity": claim.entity,
        "scope": claim.scope,
        "legacy_delta_ids": legacy_ids,
        "mapped_v2_delta_ids": mapped_ids,
        "state": state,
        "action_names": action_names or [],
        "error": error,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def migrate_claims(
    *,
    project: str,
    dataset: str,
    config_path: str,
    backfill_manifest_path: Path,
    report_path: Path,
    apply: bool,
    resume: bool,
    actor: str,
) -> dict[str, Any]:
    backfill_manifest = _load_manifest(backfill_manifest_path, resume=True)
    report = _load_or_create_report(report_path, resume=resume)
    report["project"] = project
    report["actor"] = actor
    report["apply"] = apply

    configure_vertex_adc(project)
    backend = CloudBackend(
        CloudSettings(
            project=project,
            bucket=os.getenv("TYCHO_BUCKET", f"{project}-tycho-raw"),
            dataset=dataset,
        )
    )
    config = load_config(config_path)
    legacy_to_v2 = _legacy_map(backfill_manifest)
    claims = [claim for claim in backend.list_claims() if claim.status is ClaimStatus.ACTIVE]
    selected = [
        claim
        for claim in claims
        if any(item.delta_id in legacy_to_v2 for item in claim.evidence)
    ]
    report["selected_claim_count"] = len(selected)
    report["backfill_manifest"] = str(backfill_manifest_path)
    report["updated_at"] = datetime.now(UTC).isoformat()
    backfill_manifest["claim_migration_started"] = True
    _atomic_write(backfill_manifest_path, backfill_manifest)

    for claim in selected:
        existing_entry = report.get("entries", {}).get(claim.claim_id)
        if existing_entry and not str(existing_entry.get("state", "")).startswith(
            "retryable"
        ):
            continue
        legacy_ids = [item.delta_id for item in claim.evidence if item.delta_id in legacy_to_v2]
        # A claim may already carry a canonical v2 signal alongside archived
        # evidence. Only the archived IDs need migration; the governed analyst
        # can still use the canonical claim as context, but no new action may
        # cite the old ID.
        mapped_entries = [legacy_to_v2[item] for item in legacy_ids]
        mapped_ids = [entry["delta_id"] for entry in mapped_entries]
        deltas: list[Delta] = []
        unresolved = False
        for mapped_id in mapped_ids:
            delta = backend.get_delta(mapped_id)
            if delta is None or delta.schema_version is not DeltaSchemaVersion.V2:
                unresolved = True
                break
            deltas.append(delta)
        if unresolved:
            entry = _claim_entry(
                claim,
                legacy_ids=legacy_ids,
                mapped_ids=mapped_ids,
                state="retryable_missing_canonical_delta",
            )
            report.setdefault("entries", {})[claim.claim_id] = entry
            _atomic_write(report_path, report)
            continue

        if all(delta.triage is Triage.NOISE for delta in deltas):
            entry = _claim_entry(
                claim,
                legacy_ids=legacy_ids,
                mapped_ids=mapped_ids,
                state="validated_noise",
            )
            if apply:
                _retire_claim(
                    backend,
                    claim,
                    actor=actor,
                    reason="all archived evidence maps to validated canonical noise for the same transitions",
                    mapped_delta_ids=mapped_ids,
                )
                entry["state"] = "retired_noise"
            report.setdefault("entries", {})[claim.claim_id] = entry
            _atomic_write(report_path, report)
            continue

        if any(delta.triage is Triage.MEANINGFUL for delta in deltas):
            if not apply:
                entry = _claim_entry(
                    claim,
                    legacy_ids=legacy_ids,
                    mapped_ids=mapped_ids,
                    state="ready_meaningful_analyst",
                )
                report.setdefault("entries", {})[claim.claim_id] = entry
                _atomic_write(report_path, report)
                continue
            action_names: list[str] = []
            try:
                for delta in deltas:
                    if delta.triage is not Triage.MEANINGFUL:
                        continue
                    result = asyncio.run(
                        run_analyst(delta, config, backend, mode="migration")
                    )
                    action_names.extend(
                        str(action.get("action"))
                        for action in result.actions
                        if isinstance(action, dict) and action.get("action")
                    )
                current = backend.get_claim(claim.claim_id)
                if current is not None and current.status is ClaimStatus.ACTIVE:
                    # A completed governed curator that did not replace the old
                    # belief cannot leave legacy evidence active.
                    _retire_claim(
                        backend,
                        current,
                        actor=actor,
                        reason="migration analyst completed without a canonical replacement action",
                        mapped_delta_ids=mapped_ids,
                    )
                    state = (
                        "retired_after_analyst_action"
                        if any(
                            action in {"create_claim", "supersede_claim"}
                            for action in action_names
                        )
                        else "retired_without_replacement"
                    )
                else:
                    if current is not None:
                        _record_supersession_history(
                            backend,
                            claim,
                            actor=actor,
                            mapped_delta_ids=mapped_ids,
                        )
                    state = "superseded_or_retired_by_analyst"
                entry = _claim_entry(
                    claim,
                    legacy_ids=legacy_ids,
                    mapped_ids=mapped_ids,
                    state=state,
                    action_names=action_names,
                )
            except Exception as exc:
                entry = _claim_entry(
                    claim,
                    legacy_ids=legacy_ids,
                    mapped_ids=mapped_ids,
                    state="retryable_analyst_failure",
                    action_names=action_names,
                    error=type(exc).__name__,
                )
            report.setdefault("entries", {})[claim.claim_id] = entry
            _atomic_write(report_path, report)

    final_claims = backend.list_claims()
    active_legacy = [
        claim.claim_id
        for claim in final_claims
        if claim.status is ClaimStatus.ACTIVE
        and any(item.delta_id in legacy_to_v2 for item in claim.evidence)
    ]
    report["active_legacy_claim_ids"] = active_legacy
    report["active_legacy_claim_count"] = len(active_legacy)
    report["retired_noise"] = sum(
        entry.get("state") == "retired_noise"
        for entry in report.get("entries", {}).values()
    )
    report["analyst_migrations"] = sum(
        entry.get("state") in {
            "superseded_or_retired_by_analyst",
            "retired_after_analyst_action",
            "retired_without_replacement",
        }
        for entry in report.get("entries", {}).values()
    )
    report["retryable"] = sum(
        str(entry.get("state", "")).startswith("retryable")
        for entry in report.get("entries", {}).values()
    )
    report["updated_at"] = datetime.now(UTC).isoformat()
    _atomic_write(report_path, report)
    return {
        "report_version": MANIFEST_VERSION,
        "mode": "apply" if apply else "dry-run",
        "selected_claims": len(selected),
        "retired_noise": report["retired_noise"],
        "analyst_migrations": report["analyst_migrations"],
        "retryable": report["retryable"],
        "active_legacy_claims": len(active_legacy),
        "report": str(report_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--dataset", default="tycho")
    parser.add_argument("--config", default="tycho.yaml")
    parser.add_argument("--backfill-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--actor", default="legacy-claim-migration@1")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.dry_run and args.apply:
        parser.error("--dry-run and --apply are mutually exclusive")
    try:
        result = migrate_claims(
            project=args.project,
            dataset=args.dataset,
            config_path=args.config,
            backfill_manifest_path=args.backfill_manifest,
            report_path=args.report,
            apply=args.apply,
            resume=args.resume,
            actor=args.actor,
        )
    except ClaimMigrationError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
