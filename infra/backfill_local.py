"""Idempotently backfill local Tycho history into provisioned Google Cloud stores."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from urllib.parse import unquote, urlparse

from google.api_core.exceptions import AlreadyExists, PreconditionFailed
from google.cloud import bigquery, firestore, storage

from pipeline.cloud import delta_row_for_bigquery
from pipeline.local_backend import LocalBackend, LocalSettings
from schemas.config import load_config


def existing_ids(
    client: bigquery.Client,
    table: str,
    field: str,
    ids: list[str],
) -> set[str]:
    if not ids:
        return set()
    query = f"SELECT {field} FROM `{table}` WHERE {field} IN UNNEST(@ids)"
    config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)]
    )
    return {row[field] for row in client.query(query, job_config=config).result()}


def upload_raw(
    client: storage.Client,
    bucket_name: str,
    observation,
) -> str:
    parsed = urlparse(observation.content_ref)
    if parsed.scheme != "file":
        raise ValueError(f"expected local raw reference: {observation.content_ref}")
    local_path = Path(unquote(parsed.path))
    payload = local_path.read_bytes()
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if digest != observation.content_hash:
        raise ValueError(f"raw hash mismatch for {observation.obs_id}")

    suffix = ".error.json" if local_path.name.endswith(".error.json") else ".json"
    object_name = (
        f"{observation.entity}/{observation.source}/{observation.obs_id}{suffix}"
    )
    blob = client.bucket(bucket_name).blob(object_name)
    payload_digest = hashlib.sha256(payload).digest()
    if blob.exists(timeout=30):
        if hashlib.sha256(blob.download_as_bytes(timeout=60)).digest() != payload_digest:
            raise ValueError(f"cloud raw object differs for {observation.obs_id}")
        return f"gs://{bucket_name}/{object_name}"
    try:
        blob.upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=0,
        )
    except PreconditionFailed:
        if hashlib.sha256(blob.download_as_bytes(timeout=60)).digest() != payload_digest:
            raise ValueError(f"cloud raw object differs for {observation.obs_id}")
    return f"gs://{bucket_name}/{object_name}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--config", default="tycho.yaml")
    parser.add_argument("--bucket")
    parser.add_argument("--dataset", default="tycho")
    args = parser.parse_args()

    settings = LocalSettings(root=Path(args.data_dir).resolve())
    if not settings.database.exists():
        raise SystemExit(f"local database does not exist: {settings.database}")
    config = load_config(args.config)
    bucket_name = args.bucket or f"{args.project}-tycho-raw"
    bq = bigquery.Client(project=args.project)
    gcs = storage.Client(project=args.project)
    db = firestore.Client(project=args.project)
    observation_table = f"{args.project}.{args.dataset}.observations"
    delta_table = f"{args.project}.{args.dataset}.deltas"

    with LocalBackend(config, settings) as local:
        if local.pending_count():
            raise SystemExit("local analyst outbox is not empty; run pipeline.run_local first")

        observations = local.observations()
        cloud_refs = {
            item.obs_id: upload_raw(gcs, bucket_name, item) for item in observations
        }
        known_observations = existing_ids(
            bq, observation_table, "obs_id", [item.obs_id for item in observations]
        )
        observation_rows = []
        for item in observations:
            if item.obs_id in known_observations:
                continue
            row = item.model_dump(mode="json")
            row["content_ref"] = cloud_refs[item.obs_id]
            observation_rows.append(row)
        if observation_rows:
            errors = bq.insert_rows_json(observation_table, observation_rows)
            if errors:
                raise RuntimeError(f"observation backfill failed: {errors}")

        deltas = local.deltas()
        known_deltas = existing_ids(
            bq, delta_table, "delta_id", [item.delta_id for item in deltas]
        )
        delta_rows = [
            delta_row_for_bigquery(item)
            for item in deltas
            if item.delta_id not in known_deltas
        ]
        if delta_rows:
            errors = bq.insert_rows_json(delta_table, delta_rows)
            if errors:
                raise RuntimeError(f"delta backfill failed: {errors}")

        claims_written = 0
        for claim in local.claims():
            try:
                db.collection("claims").document(claim.claim_id).create(
                    claim.model_dump(mode="json", by_alias=True)
                )
                claims_written += 1
            except AlreadyExists:
                pass

        receipts_written = 0
        for receipt in local.receipts():
            try:
                db.collection("receipts").document(receipt.receipt_id).create(
                    receipt.model_dump(mode="json")
                )
                receipts_written += 1
            except AlreadyExists:
                pass

    print(
        {
            "observations_written": len(observation_rows),
            "deltas_written": len(delta_rows),
            "claims_written": claims_written,
            "receipts_written": receipts_written,
        }
    )


if __name__ == "__main__":
    main()
