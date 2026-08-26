import json
from pathlib import Path

import pytest

from pipeline.cloud import canonical_delta_row_for_bigquery, delta_row_for_bigquery
from schemas.delta import Delta


FIXTURE = Path("schemas/fixtures/delta.example.json")
FIXTURES = Path("schemas/fixtures")


def test_delta_row_encodes_nested_bigquery_json_fields():
    delta = Delta.model_validate_json(FIXTURE.read_text())

    row = delta_row_for_bigquery(delta)
    change = row["changes"][0]

    assert change["before"] is None
    assert isinstance(change["after"], str)
    assert json.loads(change["after"]) == {
        "tag_name": "v2.1.237",
        "name": "v2.1.237",
    }


def test_canonical_delta_row_is_strict_v2_without_legacy_path():
    delta = Delta.model_validate_json(
        (FIXTURES / "delta.example.json").read_text()
    )

    row = canonical_delta_row_for_bigquery(delta)

    assert row["schema_version"] == "delta@2"
    assert "path" not in row["changes"][0]
    assert row["changes"][0]["evidence_after"]["obs_id"] == delta.obs_after


def test_canonical_delta_row_rejects_archive_v1():
    legacy = Delta.model_validate_json(
        (FIXTURES / "delta.archive.legacy.example.json").read_text()
    )

    with pytest.raises(ValueError, match="only delta@2"):
        canonical_delta_row_for_bigquery(legacy)
