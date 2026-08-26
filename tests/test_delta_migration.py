import pytest
from google.cloud import bigquery

from infra.bootstrap import DELTA_SCHEMA
from infra.migrate_delta_schema import DeltaSchemaMigrationError, desired_schema


def legacy_schema_with_required_path():
    schema = []
    for field in DELTA_SCHEMA:
        if field.name in {
            "schema_version",
            "comparison_id",
            "generated_by",
            "prompt_version",
            "triage_reason",
        }:
            continue
        if field.name == "changes":
            schema.append(
                bigquery.SchemaField(
                    "changes",
                    "RECORD",
                    mode="REPEATED",
                    fields=(
                        bigquery.SchemaField("path", "STRING", mode="REQUIRED"),
                        bigquery.SchemaField("before", "JSON"),
                        bigquery.SchemaField("after", "JSON"),
                    ),
                )
            )
        else:
            schema.append(field)
    return schema


def test_desired_schema_is_additive_and_relaxes_only_legacy_path():
    schema = desired_schema(legacy_schema_with_required_path())
    changes = next(field for field in schema if field.name == "changes")
    path = next(field for field in changes.fields if field.name == "path")

    assert path.mode == "NULLABLE"
    assert {field.name for field in schema} >= {
        "schema_version",
        "comparison_id",
        "generated_by",
        "prompt_version",
        "triage_reason",
    }
    assert {field.name for field in changes.fields} >= {
        "category",
        "scope",
        "statement",
        "evidence_before",
        "evidence_after",
    }


def test_migration_refuses_a_table_without_legacy_path():
    schema = [field for field in legacy_schema_with_required_path() if field.name != "changes"]
    with pytest.raises(DeltaSchemaMigrationError, match="changes RECORD"):
        desired_schema(schema)
