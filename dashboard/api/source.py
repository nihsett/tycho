"""The only store surface the dashboard is allowed to touch.

This module is the structural half of the dashboard's read-only guarantee.  It
exposes a narrow protocol and one Google Cloud implementation whose public
surface is exactly nine read methods.  There is deliberately no method here that
writes a claim, appends a Delta, publishes to Pub/Sub, reads Cloud Storage, or
reads the immutable ``delta_audit_log_*`` archive - so a route handler cannot
reach any of those even by accident.

Every Delta query is pinned to the canonical ``tycho.deltas`` table AND to
``schema_version = 'delta@2'``.  Retired mechanical ``delta@1`` rows live only in
the migration audit table and are not a normal read source.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from schemas.claim import Claim
from schemas.brief import Brief
from schemas.delta import Delta
from schemas.observation import Observation
from schemas.strategy import StrategySession

#: The one Delta table a dashboard read may name.
CANONICAL_DELTA_TABLE = "deltas"
#: The one Delta schema version a dashboard read may accept.
CANONICAL_SCHEMA_VERSION = "delta@2"

_DELTA_COLUMNS = """
    schema_version, delta_id, comparison_id, entity, source,
    obs_before, obs_after, computed_at, diff_kind, generated_by,
    prompt_version, changes, summary, triage, triage_reason,
    triage_by, routed_to
"""
_OBSERVATION_COLUMNS = """
    obs_id, entity, source, kind, fetched_at, content_ref,
    content_hash, adapter_ver, status
"""


class EntityObservation(Protocol):
    """Bounded acquisition freshness for one (entity, source) watcher."""

    entity: str
    source: str
    last_fetched_at: Any
    observation_count: int


class ReadSource(Protocol):
    """Exactly what a dashboard read model may ask a store for."""

    def list_claims(self) -> list[Claim]: ...

    def get_claim(self, claim_id: str) -> Claim | None: ...

    def get_delta(self, delta_id: str) -> Delta | None: ...

    def list_canonical_deltas(self) -> list[Delta]: ...

    def get_observation(self, obs_id: str) -> Observation | None: ...

    def watcher_activity(self) -> list[dict[str, Any]]: ...

    def strategy_sessions(self) -> list[StrategySession]: ...

    def get_strategy_session(self, session_id: str) -> StrategySession | None: ...

    def get_brief(self, brief_id: str) -> Brief | None: ...


def decode_delta_row(row: Any) -> Delta:
    """Decode one canonical BigQuery row into the strict Delta model."""
    data = dict(row)
    raw_changes = data.get("changes") or []
    data["changes"] = []
    for raw_change in raw_changes:
        change = dict(raw_change)
        for field in ("before", "after"):
            value = change.get(field)
            if isinstance(value, str):
                try:
                    change[field] = json.loads(value)
                except json.JSONDecodeError:
                    # A stored JSON field may already be a plain string.
                    pass
        data["changes"].append(change)
    delta = Delta.model_validate(data)
    if delta.schema_version.value != CANONICAL_SCHEMA_VERSION:
        raise ValueError("the dashboard reads canonical delta@2 rows only")
    return delta


class CloudReadSource:
    """BigQuery + Firestore reads, and nothing else.

    The clients are constructed here rather than borrowed from
    ``pipeline.cloud.CloudBackend`` on purpose: this object never holds a Cloud
    Storage or Pub/Sub client at all.
    """

    def __init__(self, project: str, dataset: str = "tycho") -> None:
        from google.cloud import bigquery

        from pipeline.cloud import _firestore_client

        self._project = project
        self._dataset = dataset
        self._bigquery = bigquery.Client(project=project)
        self._firestore = _firestore_client(project)

    @property
    def _deltas_table(self) -> str:
        return f"{self._project}.{self._dataset}.{CANONICAL_DELTA_TABLE}"

    @property
    def _observations_table(self) -> str:
        return f"{self._project}.{self._dataset}.observations"

    def _query(self, sql: str, parameters: list[Any] | None = None) -> list[Any]:
        from google.cloud import bigquery

        config = bigquery.QueryJobConfig(query_parameters=parameters or [])
        return list(self._bigquery.query(sql, job_config=config).result())

    # --- Claims -----------------------------------------------------------

    def list_claims(self) -> list[Claim]:
        return [
            Claim.model_validate(snapshot.to_dict())
            for snapshot in self._firestore.collection("claims").stream()
        ]

    def get_claim(self, claim_id: str) -> Claim | None:
        snapshot = self._firestore.collection("claims").document(claim_id).get()
        return Claim.model_validate(snapshot.to_dict()) if snapshot.exists else None

    # --- Canonical Deltas --------------------------------------------------

    def get_delta(self, delta_id: str) -> Delta | None:
        from google.cloud import bigquery

        rows = self._query(
            f"SELECT {_DELTA_COLUMNS} FROM `{self._deltas_table}` "
            f"WHERE delta_id = @delta_id AND schema_version = '{CANONICAL_SCHEMA_VERSION}' "
            "LIMIT 1",
            [bigquery.ScalarQueryParameter("delta_id", "STRING", delta_id)],
        )
        return decode_delta_row(rows[0]) if rows else None

    def list_canonical_deltas(self) -> list[Delta]:
        rows = self._query(
            f"SELECT {_DELTA_COLUMNS} FROM `{self._deltas_table}` "
            f"WHERE schema_version = '{CANONICAL_SCHEMA_VERSION}' "
            "ORDER BY computed_at, delta_id"
        )
        return [decode_delta_row(row) for row in rows]

    # --- Observations (metadata only; raw payloads are never read) ---------

    def get_observation(self, obs_id: str) -> Observation | None:
        from google.cloud import bigquery

        rows = self._query(
            f"SELECT {_OBSERVATION_COLUMNS} FROM `{self._observations_table}` "
            "WHERE obs_id = @obs_id LIMIT 1",
            [bigquery.ScalarQueryParameter("obs_id", "STRING", obs_id)],
        )
        return Observation.model_validate(dict(rows[0])) if rows else None

    def watcher_activity(self) -> list[dict[str, Any]]:
        """One row per (entity, source) watcher: freshness and volume only."""
        rows = self._query(
            "SELECT entity, source, MAX(fetched_at) AS last_fetched_at, "
            "COUNT(*) AS observation_count "
            f"FROM `{self._observations_table}` WHERE status = 'ok' "
            "GROUP BY entity, source ORDER BY entity, source"
        )
        return [
            {
                "entity": row.entity,
                "source": row.source,
                "last_fetched_at": row.last_fetched_at,
                "observation_count": int(row.observation_count),
            }
            for row in rows
        ]

    # --- Strategy sessions and briefs --------------------------------------

    def strategy_sessions(self) -> list[StrategySession]:
        return [
            StrategySession.model_validate(snapshot.to_dict())
            for snapshot in self._firestore.collection("strategy_sessions").stream()
        ]

    def get_strategy_session(self, session_id: str) -> StrategySession | None:
        snapshot = (
            self._firestore.collection("strategy_sessions").document(session_id).get()
        )
        return StrategySession.model_validate(snapshot.to_dict()) if snapshot.exists else None

    def get_brief(self, brief_id: str) -> Brief | None:
        snapshot = self._firestore.collection("briefs").document(brief_id).get()
        return Brief.model_validate(snapshot.to_dict()) if snapshot.exists else None


#: The complete public read surface.  ``tests/test_dashboard_readmodel.py``
#: asserts this set exactly, so adding a write method to the source is a test
#: failure rather than a silent capability grant.
READ_SOURCE_METHODS = frozenset(
    {
        "list_claims",
        "get_claim",
        "get_delta",
        "list_canonical_deltas",
        "get_observation",
        "watcher_activity",
        "strategy_sessions",
        "get_strategy_session",
        "get_brief",
    }
)
