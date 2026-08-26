"""Create the append-only storage, warehouse tables, and delta topic."""

from __future__ import annotations

import argparse

from google.api_core.exceptions import Conflict, NotFound
from google.cloud import bigquery, pubsub_v1, storage


OBSERVATION_SCHEMA = [
    bigquery.SchemaField("obs_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("entity", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("source", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("kind", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("fetched_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("content_ref", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("content_hash", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("adapter_ver", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("status", "STRING", mode="REQUIRED"),
]

# Compatibility schema used only when decoding/auditing the pre-repair table.
# Fresh canonical environments use V2_DELTA_SCHEMA below and cannot accept v1.
DELTA_SCHEMA = [
    # These additive fields are NULLABLE so rows written before delta@2 remain
    # valid and are loaded as delta@1 by Pydantic.
    bigquery.SchemaField("schema_version", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("delta_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("comparison_id", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("entity", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("source", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("obs_before", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("obs_after", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("computed_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("diff_kind", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("generated_by", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("prompt_version", "STRING", mode="NULLABLE"),
    bigquery.SchemaField(
        "changes",
        "RECORD",
        mode="REPEATED",
        fields=[
            bigquery.SchemaField("path", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("before", "JSON", mode="NULLABLE"),
            bigquery.SchemaField("after", "JSON", mode="NULLABLE"),
            bigquery.SchemaField("category", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("scope", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("statement", "STRING", mode="NULLABLE"),
            bigquery.SchemaField(
                "evidence_before",
                "RECORD",
                mode="NULLABLE",
                fields=[
                    bigquery.SchemaField("obs_id", "STRING", mode="REQUIRED"),
                    bigquery.SchemaField("quote", "STRING", mode="REQUIRED"),
                ],
            ),
            bigquery.SchemaField(
                "evidence_after",
                "RECORD",
                mode="NULLABLE",
                fields=[
                    bigquery.SchemaField("obs_id", "STRING", mode="REQUIRED"),
                    bigquery.SchemaField("quote", "STRING", mode="REQUIRED"),
                ],
            ),
        ],
    ),
    bigquery.SchemaField("summary", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("triage", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("triage_reason", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("triage_by", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("routed_to", "STRING", mode="REPEATED"),
]


V2_DELTA_SCHEMA = [
    bigquery.SchemaField("schema_version", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("delta_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("comparison_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("entity", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("source", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("obs_before", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("obs_after", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("computed_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("diff_kind", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("generated_by", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("prompt_version", "STRING", mode="REQUIRED"),
    bigquery.SchemaField(
        "changes",
        "RECORD",
        mode="REPEATED",
        fields=[
            bigquery.SchemaField("before", "JSON", mode="NULLABLE"),
            bigquery.SchemaField("after", "JSON", mode="NULLABLE"),
            bigquery.SchemaField("category", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("scope", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("statement", "STRING", mode="REQUIRED"),
            bigquery.SchemaField(
                "evidence_before",
                "RECORD",
                mode="NULLABLE",
                fields=[
                    bigquery.SchemaField("obs_id", "STRING", mode="REQUIRED"),
                    bigquery.SchemaField("quote", "STRING", mode="REQUIRED"),
                ],
            ),
            bigquery.SchemaField(
                "evidence_after",
                "RECORD",
                mode="REQUIRED",
                fields=[
                    bigquery.SchemaField("obs_id", "STRING", mode="REQUIRED"),
                    bigquery.SchemaField("quote", "STRING", mode="REQUIRED"),
                ],
            ),
        ],
    ),
    bigquery.SchemaField("summary", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("triage", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("triage_reason", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("triage_by", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("routed_to", "STRING", mode="REPEATED"),
]


def ensure_bucket(client: storage.Client, bucket_name: str, location: str) -> None:
    bucket = client.bucket(bucket_name)
    if bucket.exists():
        return
    bucket.storage_class = "STANDARD"
    client.create_bucket(bucket, location=location)
    print(f"created gs://{bucket_name}")


def ensure_table(
    client: bigquery.Client,
    table_id: str,
    schema: list[bigquery.SchemaField],
    partition_field: str,
) -> None:
    try:
        client.get_table(table_id)
        return
    except NotFound:
        pass
    table = bigquery.Table(table_id, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field=partition_field,
    )
    table.clustering_fields = ["entity", "source"]
    client.create_table(table)
    print(f"created {table_id}")


def bootstrap(
    project: str,
    location: str,
    bucket_name: str,
    dataset_name: str,
    topic_name: str,
) -> None:
    storage_client = storage.Client(project=project)
    bq_client = bigquery.Client(project=project)
    publisher = pubsub_v1.PublisherClient()

    ensure_bucket(storage_client, bucket_name, location)
    dataset_id = f"{project}.{dataset_name}"
    try:
        bq_client.get_dataset(dataset_id)
    except NotFound:
        dataset = bigquery.Dataset(dataset_id)
        dataset.location = location
        bq_client.create_dataset(dataset)
        print(f"created {dataset_id}")
    ensure_table(
        bq_client,
        f"{dataset_id}.observations",
        OBSERVATION_SCHEMA,
        "fetched_at",
    )
    ensure_table(
        bq_client,
        f"{dataset_id}.deltas",
        V2_DELTA_SCHEMA,
        "computed_at",
    )

    topic_path = publisher.topic_path(project, topic_name)
    try:
        publisher.create_topic(request={"name": topic_path})
        print(f"created {topic_path}")
    except Conflict:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", default="us-central1")
    parser.add_argument("--bucket")
    parser.add_argument("--dataset", default="tycho")
    parser.add_argument("--topic", default="tycho-deltas")
    args = parser.parse_args()
    bootstrap(
        project=args.project,
        location=args.location,
        bucket_name=args.bucket or f"{args.project}-tycho-raw",
        dataset_name=args.dataset,
        topic_name=args.topic,
    )


if __name__ == "__main__":
    main()
