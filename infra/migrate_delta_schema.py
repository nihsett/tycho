"""Idempotently migrate the existing tycho.deltas table for delta@2.

The migration is additive and never rewrites historical rows.  In particular,
it refuses to create a second Delta table when BigQuery cannot relax the legacy
nested changes.path field.
"""

from __future__ import annotations

import argparse
import json
from typing import Iterable

from google.cloud import bigquery
from google.api_core.exceptions import NotFound


class DeltaSchemaMigrationError(RuntimeError):
    """The canonical deltas table cannot be made backwards compatible."""


def _clone_field(field: bigquery.SchemaField, *, mode: str | None = None) -> bigquery.SchemaField:
    return bigquery.SchemaField(
        field.name,
        field.field_type,
        mode=mode or field.mode,
        description=field.description,
        fields=tuple(field.fields),
        policy_tags=field.policy_tags,
        precision=field.precision,
        scale=field.scale,
        max_length=field.max_length,
    )


def _field(name: str, field_type: str, mode: str = "NULLABLE", *, fields=()) -> bigquery.SchemaField:
    return bigquery.SchemaField(name, field_type, mode=mode, fields=tuple(fields))


def _semantic_nested_fields() -> tuple[bigquery.SchemaField, ...]:
    def evidence_before() -> bigquery.SchemaField:
        return _field(
            "evidence_before",
            "RECORD",
            fields=(
                _field("obs_id", "STRING", "REQUIRED"),
                _field("quote", "STRING", "REQUIRED"),
            ),
        )

    def evidence_after() -> bigquery.SchemaField:
        return _field(
            "evidence_after",
            "RECORD",
            fields=(
                _field("obs_id", "STRING", "REQUIRED"),
                _field("quote", "STRING", "REQUIRED"),
            ),
        )

    return (
        _field("category", "STRING"),
        _field("scope", "STRING"),
        _field("statement", "STRING"),
        evidence_before(),
        evidence_after(),
    )


def _semantic_top_level_fields() -> tuple[bigquery.SchemaField, ...]:
    return (
        _field("schema_version", "STRING"),
        _field("comparison_id", "STRING"),
        _field("generated_by", "STRING"),
        _field("prompt_version", "STRING"),
        _field("triage_reason", "STRING"),
    )


def _merge_change_fields(fields: Iterable[bigquery.SchemaField]) -> list[bigquery.SchemaField]:
    merged: list[bigquery.SchemaField] = []
    found_path = False
    present = set()
    for item in fields:
        present.add(item.name)
        if item.name == "path":
            found_path = True
            if item.mode == "REQUIRED":
                # This is the only schema relaxation.  BigQuery must accept it;
                # the caller verifies the resulting schema after update.
                item = _clone_field(item, mode="NULLABLE")
        merged.append(item)
    if not found_path:
        raise DeltaSchemaMigrationError(
            "canonical tycho.deltas.changes.path is missing; refusing an unsafe migration"
        )
    for item in _semantic_nested_fields():
        if item.name not in present:
            merged.append(item)
    return merged


def desired_schema(schema: Iterable[bigquery.SchemaField]) -> list[bigquery.SchemaField]:
    existing = list(schema)
    by_name = {field.name: field for field in existing}
    if "changes" not in by_name or by_name["changes"].field_type != "RECORD":
        raise DeltaSchemaMigrationError(
            "canonical tycho.deltas.changes RECORD field is missing; refusing a second table"
        )

    result: list[bigquery.SchemaField] = []
    for field in existing:
        if field.name == "changes":
            result.append(
                bigquery.SchemaField(
                    field.name,
                    field.field_type,
                    mode=field.mode,
                    description=field.description,
                    fields=tuple(_merge_change_fields(field.fields)),
                    policy_tags=field.policy_tags,
                    precision=field.precision,
                    scale=field.scale,
                    max_length=field.max_length,
                )
            )
        else:
            result.append(field)
    existing_names = {field.name for field in result}
    for field in _semantic_top_level_fields():
        if field.name not in existing_names:
            result.append(field)
    return result


def schema_as_dict(schema: Iterable[bigquery.SchemaField]) -> list[dict]:
    def one(field: bigquery.SchemaField) -> dict:
        item = {"name": field.name, "type": field.field_type, "mode": field.mode}
        if field.fields:
            item["fields"] = [one(child) for child in field.fields]
        return item

    return [one(field) for field in schema]


def migrate_table(client: bigquery.Client, table_id: str, *, dry_run: bool = False) -> list[dict]:
    try:
        table = client.get_table(table_id)
    except NotFound as exc:
        raise DeltaSchemaMigrationError(f"canonical table does not exist: {table_id}") from exc
    schema = desired_schema(table.schema)
    changes_field = next(field for field in schema if field.name == "changes")
    path_field = next(
        (child for child in changes_field.fields if child.name == "path"),
        None,
    )
    if path_field is None or path_field.mode != "NULLABLE":
        raise DeltaSchemaMigrationError(
            "BigQuery did not produce a NULLABLE changes.path field; "
            "refusing to create a second production Delta table"
        )
    if not dry_run:
        table.schema = schema
        try:
            client.update_table(table, ["schema"])
        except Exception as exc:
            raise DeltaSchemaMigrationError(
                "BigQuery could not relax nested changes.path in the canonical "
                f"table; exact blocker: {type(exc).__name__}: {exc}"
            ) from exc
        verified = client.get_table(table_id)
        verified_changes = next(field for field in verified.schema if field.name == "changes")
        verified_path = next(field for field in verified_changes.fields if field.name == "path")
        if verified_path.mode != "NULLABLE":
            raise DeltaSchemaMigrationError(
                "BigQuery update completed without a NULLABLE changes.path; "
                "refusing a second production Delta table"
            )
        schema = verified.schema
    return schema_as_dict(schema)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--dataset", default="tycho")
    parser.add_argument("--table", default="deltas")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    client = bigquery.Client(project=args.project)
    table_id = f"{args.project}.{args.dataset}.{args.table}"
    schema = migrate_table(client, table_id, dry_run=args.dry_run)
    print(json.dumps({"table": table_id, "dry_run": args.dry_run, "schema": schema}, indent=2))


if __name__ == "__main__":
    main()
