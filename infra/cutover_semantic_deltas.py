"""Cut over Tycho to a strict Delta@2 BigQuery table.

The command is intentionally explicit and resumable.  Before ``--apply`` it
only inspects metadata and runs validation queries.  Apply pauses the bounded
Scheduler window, copies validated v2 rows into a fresh candidate, verifies the
candidate, renames the old physical table to the immutable audit name, and
renames the candidate to ``deltas``.  No table is deleted or truncated.

``--rollback`` performs the documented operational rollback: pause Scheduler,
rename the failed canonical table to a timestamped name, rename the audit table
back to ``deltas``, and leave acquisition semantic-only.

Scheduler is resumed only when acquisition would be safe: either the swap
completed and read back, or nothing was renamed and the original ``deltas``
table is still authoritative.  A failure at or after the archive rename leaves
Scheduler paused and reports the exact resumable physical-table state.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import google.auth
from google.api_core.exceptions import NotFound
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery

from infra.bootstrap import V2_DELTA_SCHEMA

DEFAULT_PROJECT = "gen-lang-client-0110801105"
DEFAULT_DATASET = "tycho"
DEFAULT_REGION = "us-central1"
DEFAULT_SCHEDULER = "tycho-nightly"
DEFAULT_SUBSCRIPTION = "tycho-analyst-push"
DEFAULT_CANDIDATE = "deltas_v2_candidate"
DEFAULT_AUDIT = "delta_audit_log_20260826"
DEFAULT_EVIDENCE = Path("data/semantic_delta_cutover_evidence.json")


class CutoverError(RuntimeError):
    """The cutover cannot safely continue."""


def table_id(project: str, dataset: str, name: str) -> str:
    return f"{project}.{dataset}.{name}"


def _schema_dict(schema: list[bigquery.SchemaField]) -> list[dict[str, Any]]:
    def one(field: bigquery.SchemaField) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": field.name,
            "type": field.field_type,
            "mode": field.mode,
        }
        if field.fields:
            value["fields"] = [one(child) for child in field.fields]
        return value

    return [one(field) for field in schema]


def _run_gcloud(*args: str, capture: bool = True) -> str:
    command = ["gcloud", *args]
    result = subprocess.run(command, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def scheduler_pause(project: str, region: str, scheduler: str) -> None:
    _run_gcloud(
        "scheduler",
        "jobs",
        "pause",
        scheduler,
        f"--location={region}",
        f"--project={project}",
        "--quiet",
    )


def scheduler_resume(project: str, region: str, scheduler: str) -> None:
    _run_gcloud(
        "scheduler",
        "jobs",
        "resume",
        scheduler,
        f"--location={region}",
        f"--project={project}",
        "--quiet",
    )


def scheduler_readback(project: str, region: str, scheduler: str) -> dict[str, Any]:
    raw = _run_gcloud(
        "scheduler",
        "jobs",
        "describe",
        scheduler,
        f"--location={region}",
        f"--project={project}",
        "--format=json",
    )
    data = json.loads(raw or "{}")
    return {
        "name": data.get("name"),
        "state": data.get("state"),
        "schedule": data.get("schedule"),
        "time_zone": data.get("timeZone"),
        "next_schedule_time": data.get("nextScheduleTime"),
    }


def verify_no_active_acquisition(project: str, region: str) -> dict[str, Any]:
    raw = _run_gcloud(
        "run",
        "jobs",
        "executions",
        "list",
        "--job=tycho-acquire",
        f"--region={region}",
        f"--project={project}",
        "--format=json",
    )
    executions = json.loads(raw or "[]")
    active = []
    for execution in executions:
        conditions = execution.get("conditions") or []
        if any(condition.get("state") == "CONDITION_TRUE" for condition in conditions):
            # Cloud Run exposes a successful completion condition as true too;
            # only an execution with no terminal condition is considered active.
            if not execution.get("completionTime"):
                active.append(execution.get("name"))
    if active:
        raise CutoverError(f"tycho-acquire execution is active: {active}")
    return {"active": [], "observed_executions": len(executions)}


def subscription_readback(project: str, subscription: str) -> dict[str, Any]:
    raw = _run_gcloud(
        "pubsub",
        "subscriptions",
        "describe",
        subscription,
        f"--project={project}",
        "--format=json",
    )
    data = json.loads(raw or "{}")
    push = data.get("pushConfig") or {}
    return {
        "name": data.get("name"),
        "topic": data.get("topic"),
        "ack_deadline_seconds": data.get("ackDeadlineSeconds"),
        "push_endpoint": push.get("pushEndpoint"),
        "push_service_account": push.get("oidcToken", {}).get("serviceAccountEmail"),
        "push_audience": push.get("oidcToken", {}).get("audience"),
        "message_retention_duration": data.get("messageRetentionDuration"),
    }


def subscription_backlog_readback(
    project: str,
    subscription: str,
    *,
    lookback_minutes: int = 10,
) -> dict[str, Any]:
    """Read the bounded Pub/Sub backlog metric without consuming messages."""
    end = datetime.now(UTC).replace(microsecond=0)
    params = {
        "filter": (
            'metric.type="pubsub.googleapis.com/subscription/'
            f'num_undelivered_messages" AND resource.labels.subscription_id="{subscription}"'
        ),
        "interval.endTime": end.isoformat().replace("+00:00", "Z"),
        "interval.startTime": (end - timedelta(minutes=lookback_minutes))
        .isoformat()
        .replace("+00:00", "Z"),
        "view": "FULL",
    }
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    response = AuthorizedSession(credentials).get(
        f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries",
        params=params,
        timeout=60,
    )
    response.raise_for_status()
    series = response.json().get("timeSeries") or []
    points = series[0].get("points") if series else []
    values = [
        int(point["value"]["int64Value"])
        for point in (points or [])
        if "int64Value" in point.get("value", {})
    ]
    if not values:
        raise CutoverError(
            "Pub/Sub backlog metric has no recent points; refusing table cutover"
        )
    return {
        "metric": "pubsub.googleapis.com/subscription/num_undelivered_messages",
        "subscription": subscription,
        "lookback_minutes": lookback_minutes,
        "observed_values": values,
        "latest_undelivered_messages": values[0],
        "zero_pending_messages": all(value == 0 for value in values),
    }


def table_exists(client: bigquery.Client, table: str) -> bool:
    try:
        client.get_table(table)
    except NotFound:
        return False
    return True


def table_inventory(client: bigquery.Client, table: str) -> dict[str, Any]:
    resource = client.get_table(table)
    query = f"""
        SELECT
          COUNT(*) AS row_count,
          COUNTIF(schema_version IS NULL OR schema_version = 'delta@1') AS legacy_rows,
          COUNTIF(schema_version = 'delta@2') AS semantic_rows,
          COUNT(DISTINCT delta_id) AS distinct_delta_ids,
          COUNT(DISTINCT comparison_id) AS distinct_comparison_ids,
          TO_HEX(SHA256(COALESCE(STRING_AGG(delta_id, ',' ORDER BY delta_id), ''))) AS delta_id_hash
        FROM `{table}`
    """
    row = next(iter(client.query(query).result()))
    partition = resource.time_partitioning
    return {
        "table": table,
        "row_count": int(row["row_count"]),
        "legacy_rows": int(row["legacy_rows"]),
        "semantic_rows": int(row["semantic_rows"]),
        "distinct_delta_ids": int(row["distinct_delta_ids"]),
        "distinct_comparison_ids": int(row["distinct_comparison_ids"]),
        "delta_id_hash": row["delta_id_hash"],
        "schema": _schema_dict(list(resource.schema)),
        "partition_field": getattr(partition, "field", None) if partition else None,
        "clustering_fields": list(resource.clustering_fields or []),
    }


def canonical_invariants(client: bigquery.Client, table: str) -> dict[str, Any]:
    query = f"""
        SELECT
          COUNT(*) AS row_count,
          COUNTIF(schema_version != 'delta@2') AS bad_schema_version,
          COUNTIF(diff_kind != 'semantic') AS bad_diff_kind,
          COUNTIF(generated_by != 'gemini-3.7-flash@semantic-differ-1') AS bad_generator,
          COUNTIF(prompt_version != 'semantic-delta@2') AS bad_prompt,
          COUNTIF(comparison_id IS NULL OR comparison_id = '') AS missing_comparison,
          COUNTIF(triage = 'meaningful' AND (ARRAY_LENGTH(changes) < 1 OR ARRAY_LENGTH(changes) > 8)) AS bad_meaningful_bounds,
          COUNTIF(triage = 'meaningful' AND ARRAY_LENGTH(routed_to) = 0) AS meaningful_without_scope,
          COUNTIF(triage = 'noise' AND (ARRAY_LENGTH(changes) != 0 OR ARRAY_LENGTH(routed_to) != 0 OR triage_reason IS NULL OR triage_reason = '')) AS bad_noise,
          COUNT(*) - COUNT(DISTINCT comparison_id) AS duplicate_comparison_ids
        FROM `{table}`
    """
    row = next(iter(client.query(query).result()))
    result = {key: int(row[key]) for key in row.keys()}
    result["rows"] = result.pop("row_count")
    return result


def pair_invariants(
    client: bigquery.Client,
    *,
    raw_table: str,
    canonical_table: str,
    observations_table: str,
) -> dict[str, Any]:
    query = f"""
        WITH legacy AS (
          SELECT obs_before, obs_after
          FROM `{raw_table}`
          WHERE schema_version IS NULL OR schema_version = 'delta@1'
          GROUP BY obs_before, obs_after
        ), replacements AS (
          SELECT obs_before, obs_after, COUNT(*) AS count_v2
          FROM `{canonical_table}`
          WHERE schema_version = 'delta@2'
          GROUP BY obs_before, obs_after
        ), missing AS (
          SELECT COUNT(*) AS value FROM legacy l
          LEFT JOIN replacements r USING (obs_before, obs_after)
          WHERE r.obs_before IS NULL OR r.count_v2 != 1
        ), duplicate_pairs AS (
          SELECT COUNT(*) AS value FROM replacements WHERE count_v2 != 1
        ), bad_observations AS (
          SELECT COUNT(*) AS value
          FROM `{canonical_table}` d
          LEFT JOIN `{observations_table}` before ON before.obs_id = d.obs_before
          LEFT JOIN `{observations_table}` after ON after.obs_id = d.obs_after
          WHERE d.schema_version = 'delta@2'
            AND (before.obs_id IS NULL OR after.obs_id IS NULL
                 OR before.entity != d.entity OR after.entity != d.entity
                 OR before.source != d.source OR after.source != d.source)
        )
        SELECT
          (SELECT COUNT(*) FROM legacy) AS legacy_pairs,
          (SELECT value FROM missing) AS missing_or_wrong_replacements,
          (SELECT value FROM duplicate_pairs) AS duplicate_pairs,
          (SELECT value FROM bad_observations) AS bad_observation_joins
    """
    row = next(iter(client.query(query).result()))
    return {key: int(row[key]) for key in row.keys()}


def ensure_candidate(
    client: bigquery.Client,
    candidate: str,
    *,
    dry_run: bool,
) -> None:
    try:
        current = client.get_table(candidate)
    except NotFound:
        if dry_run:
            return
        table = bigquery.Table(candidate, schema=V2_DELTA_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="computed_at",
        )
        table.clustering_fields = ["entity", "source"]
        table.description = "Tycho canonical strict Delta@2 table"
        client.create_table(table)
        return
    if _schema_dict(list(current.schema)) != _schema_dict(V2_DELTA_SCHEMA):
        raise CutoverError("existing candidate schema is not the strict v2 schema")
    partition = current.time_partitioning
    if not partition or partition.field != "computed_at":
        raise CutoverError("existing candidate has the wrong partitioning")


def copy_validated_v2_rows(
    client: bigquery.Client,
    *,
    raw_table: str,
    candidate: str,
    dry_run: bool,
) -> None:
    query = f"""
        INSERT INTO `{candidate}` (
          schema_version, delta_id, comparison_id, entity, source,
          obs_before, obs_after, computed_at, diff_kind, generated_by,
          prompt_version, changes, summary, triage, triage_reason,
          triage_by, routed_to
        )
        SELECT
          d.schema_version, d.delta_id, d.comparison_id, d.entity, d.source,
          d.obs_before, d.obs_after, d.computed_at, d.diff_kind, d.generated_by,
          d.prompt_version,
          ARRAY(
            SELECT AS STRUCT
              c.before, c.after, c.category, c.scope, c.statement,
              c.evidence_before, c.evidence_after
            FROM UNNEST(d.changes) AS c
          ),
          d.summary, d.triage, d.triage_reason, d.triage_by, d.routed_to
        FROM `{raw_table}` AS d
        WHERE d.schema_version = 'delta@2'
          AND d.diff_kind = 'semantic'
          AND d.generated_by = 'gemini-3.7-flash@semantic-differ-1'
          AND d.prompt_version = 'semantic-delta@2'
          AND d.comparison_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM `{candidate}` AS existing
            WHERE existing.comparison_id = d.comparison_id
          )
    """
    if not dry_run:
        client.query(query).result()


def rename_table(client: bigquery.Client, source: str, destination_name: str) -> None:
    """Use BigQuery's same-dataset atomic rename; never copy/delete rows."""
    dataset = source.rsplit(".", 1)[0]
    destination = f"{dataset}.{destination_name}"
    try:
        client.get_table(destination)
    except NotFound:
        pass
    else:
        raise CutoverError(f"rename destination already exists: {destination}")
    try:
        client.query(
            f"ALTER TABLE `{source}` RENAME TO `{destination_name}`"
        ).result()
    except Exception as exc:
        if "streaming data" in str(exc).lower():
            raise CutoverError(
                "BigQuery table rename is blocked by streaming data; "
                f"source={source}; exact blocker: {exc}"
            ) from exc
        raise


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _canonical_still_authoritative(
    client: bigquery.Client,
    *,
    raw: str,
    audit: str,
) -> bool:
    """True when the archive rename did not commit and ``deltas`` is unchanged.

    This reads the real physical names rather than trusting how far the cutover
    believed it got, so an ambiguous rename failure fails closed.
    """
    try:
        return table_exists(client, raw) and not table_exists(client, audit)
    except Exception:  # noqa: BLE001 - fail closed and leave Scheduler paused
        return False


def _physical_state(
    client: bigquery.Client,
    *,
    raw: str,
    candidate: str,
    audit: str,
) -> dict[str, Any]:
    """Report which physical table names exist, for a paused partial cutover."""
    state: dict[str, Any] = {}
    for label, table in (("canonical", raw), ("candidate", candidate), ("audit", audit)):
        try:
            state[f"{label}_table"] = table
            state[f"{label}_exists"] = table_exists(client, table)
        except Exception as exc:  # noqa: BLE001 - the state report must not raise
            state[f"{label}_exists"] = None
            state[f"{label}_lookup_error"] = str(exc)
    return state


def run_cutover(
    *,
    project: str,
    dataset: str,
    region: str,
    scheduler: str,
    subscription: str,
    candidate_name: str,
    audit_name: str,
    evidence_path: Path,
    apply: bool,
    resume: bool,
) -> dict[str, Any]:
    del resume  # Physical table names are the durable cutover checkpoint.
    client = bigquery.Client(project=project)
    raw = table_id(project, dataset, "deltas")
    candidate = table_id(project, dataset, candidate_name)
    audit = table_id(project, dataset, audit_name)
    observations = table_id(project, dataset, "observations")
    raw_exists = table_exists(client, raw)
    candidate_exists = table_exists(client, candidate)
    audit_exists = table_exists(client, audit)

    # A completed swap is safe to read back and never needs a second swap.
    if apply and raw_exists and audit_exists and not candidate_exists:
        canonical_checks = canonical_invariants(client, raw)
        if any(value for key, value in canonical_checks.items() if key != "rows"):
            raise CutoverError(
                f"canonical table exists beside the audit table but is not strict v2: {canonical_checks}"
            )
        result = {
            "evidence_version": "semantic-cutover@1",
            "project": project,
            "dataset": dataset,
            "mode": "already-complete",
            "canonical_table_after": raw,
            "audit_table": audit,
            "canonical_inventory": table_inventory(client, raw),
            "archive_inventory": table_inventory(client, audit),
            "canonical_invariants": canonical_checks,
            "scheduler": scheduler_readback(project, region, scheduler),
            "subscription": subscription_readback(project, subscription),
            "differ_mode": "semantic",
        }
        _write_evidence(evidence_path, result)
        return result

    if not apply and not raw_exists:
        raise CutoverError(f"canonical table does not exist: {raw}")
    if apply and raw_exists and audit_exists:
        raise CutoverError(
            f"ambiguous cutover state: both canonical and audit names exist ({raw}, {audit})"
        )
    if apply and not raw_exists and not audit_exists:
        raise CutoverError(
            f"cutover cannot resume: neither canonical nor audit table exists ({raw}, {audit})"
        )
    source_table = raw if raw_exists else audit
    before = table_inventory(client, source_table)

    ensure_candidate(client, candidate, dry_run=not apply)
    if apply and not raw_exists and not candidate_exists:
        raise CutoverError(
            f"cutover resumed after archive rename but candidate is missing: {candidate}"
        )
    if raw_exists:
        copy_validated_v2_rows(
            client, raw_table=raw, candidate=candidate, dry_run=not apply
        )
    candidate_invariants = (
        canonical_invariants(client, candidate)
        if apply or (not apply and candidate_exists)
        else {"not_run": "candidate does not exist until --apply"}
    )
    if apply and any(
        candidate_invariants.get(key, 0)
        for key in (
            "bad_schema_version",
            "bad_diff_kind",
            "bad_generator",
            "bad_prompt",
            "missing_comparison",
            "bad_meaningful_bounds",
            "meaningful_without_scope",
            "bad_noise",
            "duplicate_comparison_ids",
        )
    ):
        raise CutoverError(f"candidate invariants failed: {candidate_invariants}")

    evidence: dict[str, Any] = {
        "evidence_version": "semantic-cutover@1",
        "project": project,
        "dataset": dataset,
        "canonical_table_before": raw,
        "candidate_table": candidate,
        "audit_table": audit,
        "cutover_source_table": source_table,
        "captured_at": datetime.now(UTC).isoformat(),
        "mode": "apply" if apply else "dry-run",
        "before_inventory": before,
        "candidate_invariants": candidate_invariants,
        "subscription_before": subscription_readback(project, subscription),
        "pubsub_backlog_before": subscription_backlog_readback(project, subscription),
        "scheduler_before": scheduler_readback(project, region, scheduler),
        "acquisition_before": verify_no_active_acquisition(project, region),
        "pubsub_published_by_cutover": 0,
        "runtime_invocations_by_cutover": 0,
        "alerts_by_cutover": 0,
        "differ_mode": "semantic",
    }
    if not apply:
        _write_evidence(evidence_path, evidence)
        return evidence

    scheduler_pause(project, region, scheduler)
    paused = True
    swap_verified = False
    try:
        evidence["scheduler_paused"] = True
        evidence["acquisition_during_cutover"] = verify_no_active_acquisition(project, region)
        evidence["subscription_during_cutover"] = subscription_readback(project, subscription)
        evidence["pubsub_backlog_during_cutover"] = subscription_backlog_readback(
            project, subscription
        )
        # The importer never publishes, so any backlog here predates the repair.
        if evidence["subscription_during_cutover"] != evidence["subscription_before"]:
            raise CutoverError("Pub/Sub endpoint/identity/audience changed during cutover")
        if not evidence["pubsub_backlog_during_cutover"]["zero_pending_messages"]:
            raise CutoverError("Pub/Sub has undelivered messages; refusing table cutover")
        pair_checks = pair_invariants(
            client,
            raw_table=source_table,
            canonical_table=candidate,
            observations_table=observations,
        )
        evidence["candidate_pair_invariants"] = pair_checks
        if any(pair_checks[key] for key in (
            "missing_or_wrong_replacements",
            "duplicate_pairs",
            "bad_observation_joins",
        )):
            raise CutoverError(f"candidate pair invariants failed: {pair_checks}")

        if raw_exists:
            rename_table(client, raw, audit_name)
            evidence["archive_renamed"] = True
        if table_exists(client, candidate):
            rename_table(client, candidate, "deltas")
            evidence["candidate_renamed"] = True
        else:
            raise CutoverError(f"candidate table disappeared before rename: {candidate}")
        evidence["archive_inventory"] = table_inventory(client, audit)
        evidence["canonical_inventory"] = table_inventory(client, raw)
        evidence["canonical_invariants"] = canonical_invariants(client, raw)
        evidence["canonical_pair_invariants"] = pair_invariants(
            client,
            raw_table=audit,
            canonical_table=raw,
            observations_table=observations,
        )
        evidence["scheduler_after"] = scheduler_readback(project, region, scheduler)
        evidence["subscription_after"] = subscription_readback(project, subscription)
        swap_verified = True
        _write_evidence(evidence_path, evidence)
    finally:
        if paused:
            canonical_intact = _canonical_still_authoritative(
                client, raw=raw, audit=audit
            )
            if swap_verified or canonical_intact:
                scheduler_resume(project, region, scheduler)
                evidence["scheduler_resumed"] = True
                evidence["scheduler_final"] = scheduler_readback(project, region, scheduler)
            else:
                # The archive rename was attempted, so canonical ``deltas`` is
                # absent or unverified.  Resuming here would let acquisition run
                # against a missing canonical table.
                evidence["scheduler_resumed"] = False
                evidence["scheduler_left_paused"] = True
                evidence["resumable_state"] = _physical_state(
                    client, raw=raw, candidate=candidate, audit=audit
                )
                evidence["resume_command"] = (
                    "uv run python -m infra.cutover_semantic_deltas "
                    f"--project {project} --region {region} --apply --resume"
                )
                try:
                    evidence["scheduler_final"] = scheduler_readback(
                        project, region, scheduler
                    )
                except Exception as exc:  # noqa: BLE001 - never mask the failure
                    evidence["scheduler_final_error"] = str(exc)
                print(
                    json.dumps(
                        {
                            "cutover": "partial",
                            "scheduler": f"{scheduler} LEFT PAUSED",
                            "resumable_state": evidence["resumable_state"],
                            "resume_command": evidence["resume_command"],
                            "evidence": str(evidence_path),
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
            _write_evidence(evidence_path, evidence)
    return evidence


def rollback(
    *,
    project: str,
    dataset: str,
    region: str,
    scheduler: str,
    audit_name: str,
    evidence_path: Path,
) -> dict[str, Any]:
    client = bigquery.Client(project=project)
    canonical = table_id(project, dataset, "deltas")
    audit = table_id(project, dataset, audit_name)
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    failed_name = f"deltas_failed_v2_{timestamp}"
    scheduler_pause(project, region, scheduler)
    try:
        rename_table(client, canonical, failed_name)
        rename_table(client, audit, "deltas")
        result = {
            "mode": "rollback",
            "failed_v2_table": table_id(project, dataset, failed_name),
            "restored_table": canonical,
            "differ_mode": "semantic",
            "python_delta_generation_reenabled": False,
            "scheduler_paused": True,
        }
        _write_evidence(evidence_path, result)
        return result
    finally:
        scheduler_resume(project, region, scheduler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--scheduler", default=DEFAULT_SCHEDULER)
    parser.add_argument("--subscription", default=DEFAULT_SUBSCRIPTION)
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    parser.add_argument("--audit", default=DEFAULT_AUDIT)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if sum(bool(value) for value in (args.apply, args.dry_run, args.rollback)) > 1:
        parser.error("choose only one of --dry-run, --apply, or --rollback")
    try:
        if args.rollback:
            result = rollback(
                project=args.project,
                dataset=args.dataset,
                region=args.region,
                scheduler=args.scheduler,
                audit_name=args.audit,
                evidence_path=args.evidence,
            )
        else:
            result = run_cutover(
                project=args.project,
                dataset=args.dataset,
                region=args.region,
                scheduler=args.scheduler,
                subscription=args.subscription,
                candidate_name=args.candidate,
                audit_name=args.audit,
                evidence_path=args.evidence,
                apply=args.apply,
                resume=args.resume,
            )
    except (CutoverError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        "mode": result.get("mode"),
        "canonical_table": result.get("canonical_table_after", result.get("restored_table")),
        "audit_table": result.get("audit_table", result.get("failed_v2_table")),
        "scheduler": result.get("scheduler_after", result.get("scheduler_final")),
        "evidence": str(args.evidence),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
