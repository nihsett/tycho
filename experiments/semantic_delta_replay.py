"""Read-only historical replay for production semantic Delta calibration.

This command reads existing Delta/Observation rows and raw GCS payloads, runs the
same comparison builder, Gemini request, and validators as acquisition, and
writes only a bounded local report. It never inserts BigQuery rows, publishes
Pub/Sub messages, or writes claims.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.cloud import bigquery, storage

from pipeline.semantic_differ import (
    GENERATED_BY,
    PROMPT_VERSION,
    SemanticDiffer,
    build_comparison_bundle,
    bounded_error,
    comparison_id_for,
)

DEFAULT_PROJECT = "gen-lang-client-0110801105"
KNOWN_CASES = {
    "pi_github_release": "dlt_01M0VAH1ZKXR86JKD5JGERV5MD",
    "codex_changelog": "dlt_01M0VAGWYP8DSE42X5HPX38Z76",
    "gemini_nightly": "dlt_01M0VAGYR85QFEFE9360CQS6Y9",
    "pi_rolling_changelog": "dlt_01M0VAH4P2AV1WG9G6B5958TSC",
}


def _download(gcs: storage.Client, uri: str) -> bytes:
    if not uri.startswith("gs://"):
        raise ValueError(f"historical replay requires a GCS reference: {uri}")
    bucket, _, object_name = uri[5:].partition("/")
    if not bucket or not object_name:
        raise ValueError(f"invalid GCS reference: {uri}")
    return gcs.bucket(bucket).blob(object_name).download_as_bytes()


def load_pairs(
    bq: bigquery.Client,
    *,
    project: str,
    dataset: str,
    delta_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    table = f"{project}.{dataset}"
    query = f"""
        SELECT d.delta_id, d.entity, d.source, d.obs_before, d.obs_after,
               before.content_ref AS before_ref, after.content_ref AS after_ref
        FROM `{table}.deltas` AS d
        JOIN `{table}.observations` AS before ON before.obs_id = d.obs_before
        JOIN `{table}.observations` AS after ON after.obs_id = d.obs_after
    """
    parameters = []
    if delta_ids:
        query += " WHERE d.delta_id IN UNNEST(@ids)"
        parameters.append(bigquery.ArrayQueryParameter("ids", "STRING", delta_ids))
    query += " ORDER BY d.computed_at, d.delta_id"
    config = bigquery.QueryJobConfig(query_parameters=parameters)
    return [dict(row) for row in bq.query(query, job_config=config).result()]


def replay(
    *,
    project: str,
    dataset: str,
    output: Path,
    delta_ids: list[str] | None = None,
    known_cases: bool = False,
) -> dict[str, Any]:
    bq = bigquery.Client(project=project)
    gcs = storage.Client(project=project)
    differ = SemanticDiffer(project=project)
    selected_ids = delta_ids
    if known_cases:
        selected_ids = list(KNOWN_CASES.values())

    reports: list[dict[str, Any]] = []
    for row in load_pairs(bq, project=project, dataset=dataset, delta_ids=selected_ids):
        item: dict[str, Any] = {
            "delta_id": row["delta_id"],
            "entity": row["entity"],
            "source": row["source"],
            "obs_before": row["obs_before"],
            "obs_after": row["obs_after"],
            "comparison_id": comparison_id_for(row["obs_before"], row["obs_after"]),
        }
        try:
            before_payload = _download(gcs, row["before_ref"])
            after_payload = _download(gcs, row["after_ref"])
            bundle = build_comparison_bundle(
                row["entity"],
                row["source"],
                before_payload,
                after_payload,
                obs_before=row["obs_before"],
                obs_after=row["obs_after"],
            )
            result = differ.compare_bundle(
                bundle,
                obs_before=row["obs_before"],
                obs_after=row["obs_after"],
            )
            item.update(
                {
                    "outcome": result.delta.triage.value,
                    "validation": result.validation,
                    "input_bytes": result.input_bytes,
                    "estimated_input_tokens": result.estimated_input_tokens,
                    "usage": result.usage,
                    # The replay report is local and explicitly opt-in; unlike
                    # Firestore/traces it may contain the reviewed model output.
                    "model_output": result.proposal.model_dump(mode="json"),
                    "candidate_delta": result.delta.model_dump(mode="json"),
                }
            )
        except Exception as exc:
            error_class, error_message = bounded_error(exc)
            item.update(
                {
                    "outcome": "failed",
                    "validation": "failed",
                    "error_class": error_class,
                    "error_message": error_message,
                }
            )
        reports.append(item)

    grouped: dict[str, dict[str, int | float]] = defaultdict(
        lambda: {"total": 0, "meaningful": 0, "noise": 0, "failed": 0, "cost_usd": 0.0}
    )
    total_cost = 0.0
    for item in reports:
        key = f"{item['entity']}/{item['source']}"
        summary = grouped[key]
        summary["total"] += 1
        outcome = item.get("outcome")
        if outcome in {"meaningful", "noise", "failed"}:
            summary[outcome] += 1
        cost = float((item.get("usage") or {}).get("estimated_cost_usd", 0.0))
        summary["cost_usd"] += cost
        total_cost += cost

    report = {
        "report_version": "semantic-replay@1",
        "generated_at": datetime.now(UTC).isoformat(),
        "project": project,
        "dataset": dataset,
        "model": GENERATED_BY,
        "prompt_version": PROMPT_VERSION,
        "writes_performed": [],
        "cases": reports,
        "summary_by_entity_source": dict(sorted(grouped.items())),
        "summary": {
            "total": len(reports),
            "meaningful": sum(item.get("outcome") == "meaningful" for item in reports),
            "noise": sum(item.get("outcome") == "noise" for item in reports),
            "failed": sum(item.get("outcome") == "failed" for item in reports),
            "estimated_cost_usd": round(total_cost, 6),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--dataset", default="tycho")
    parser.add_argument("--output", type=Path, default=Path("data/semantic_delta_replay.json"))
    parser.add_argument("--delta-id", action="append", dest="delta_ids")
    parser.add_argument(
        "--known-cases",
        action="store_true",
        help="replay the four bake-off cases only",
    )
    args = parser.parse_args()
    report = replay(
        project=args.project,
        dataset=args.dataset,
        output=args.output,
        delta_ids=args.delta_ids,
        known_cases=args.known_cases,
    )
    print(json.dumps(report["summary"], sort_keys=True))
    for key, summary in report["summary_by_entity_source"].items():
        print(f"{key}: {json.dumps(summary, sort_keys=True)}")


if __name__ == "__main__":
    main()
