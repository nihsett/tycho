"""Persistent local stores with the same contracts as the Google Cloud backend."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pipeline.analyst import process_delta
from pipeline.analyst_lease import (
    AnalystLeaseDecision,
    lease_document_id,
    lease_is_active,
)
from pipeline.semantic_differ import (
    DeltaGenerationLeaseDecision,
    GenerationPair,
)
from pipeline.strategy_lease import (
    StrategyLeaseDecision,
    strategy_lease_document_id,
    strategy_lease_is_active,
)
from schemas.brief import Brief
from schemas.claim import Claim
from schemas.config import TychoConfig
from schemas.delta import Delta, DeltaSchemaVersion, Triage
from schemas.observation import Observation
from schemas.receipt import DeliveryReceipt
from schemas.strategy import SessionState, StrategySession


@dataclass(frozen=True)
class LocalSettings:
    root: Path = Path("data")

    @property
    def database(self) -> Path:
        return self.root / "tycho.sqlite3"

    @property
    def raw(self) -> Path:
        return self.root / "raw"


class LocalBackend:
    """SQLite metadata/claims plus write-once filesystem raw payloads."""

    def __init__(self, config: TychoConfig, settings: LocalSettings | None = None) -> None:
        self.config = config
        self.settings = settings or LocalSettings()
        self.settings.root.mkdir(parents=True, exist_ok=True)
        self.settings.raw.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.settings.database, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        try:
            self.connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as exc:
            # Another worker may be enabling WAL while this connection starts.
            # The connection can safely continue with the database's existing
            # journal mode; generation lease transactions still use IMMEDIATE.
            if "locked" not in str(exc).lower():
                raise
        self.connection.execute("PRAGMA synchronous = FULL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS observations (
                obs_id TEXT PRIMARY KEY,
                entity TEXT NOT NULL,
                source TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                status TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                document TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS observations_latest
                ON observations(entity, source, status, fetched_at DESC);

            CREATE TABLE IF NOT EXISTS deltas (
                delta_id TEXT PRIMARY KEY,
                entity TEXT NOT NULL,
                source TEXT NOT NULL,
                computed_at TEXT NOT NULL,
                triage TEXT NOT NULL,
                document TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS deltas_timeline
                ON deltas(entity, source, computed_at DESC);

            CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY,
                entity TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL,
                document TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS claims_active_scope
                ON claims(entity, status, scope);

            CREATE TABLE IF NOT EXISTS receipts (
                receipt_id TEXT PRIMARY KEY,
                dedup_key TEXT NOT NULL UNIQUE,
                claim_id TEXT NOT NULL,
                claim_version INTEGER NOT NULL,
                context_key TEXT NOT NULL,
                document TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delta_outbox (
                delta_id TEXT PRIMARY KEY REFERENCES deltas(delta_id),
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK(state IN ('pending', 'published')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                published_at TEXT
            );

            CREATE TABLE IF NOT EXISTS analyst_runs (
                run_id TEXT PRIMARY KEY,
                delta_id TEXT NOT NULL REFERENCES deltas(delta_id),
                mode TEXT NOT NULL CHECK(mode IN ('shadow', 'live')),
                analyst_version TEXT NOT NULL,
                model TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('running', 'completed', 'failed')),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                input_document TEXT NOT NULL,
                actions_document TEXT,
                final_text TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS analyst_runs_delta
                ON analyst_runs(delta_id, mode, analyst_version, started_at DESC);

            CREATE TABLE IF NOT EXISTS analyst_run_leases (
                lease_id TEXT PRIMARY KEY,
                delta_id TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('shadow', 'live')),
                analyst_version TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active', 'completed', 'failed')),
                run_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS alerts (
                alert_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL,
                delta_id TEXT NOT NULL,
                severity TEXT NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(claim_id, delta_id, kind)
            );

            CREATE TABLE IF NOT EXISTS delta_generation_runs (
                run_id TEXT PRIMARY KEY,
                obs_before TEXT NOT NULL,
                obs_after TEXT NOT NULL,
                obs_before_hash TEXT,
                obs_after_hash TEXT,
                generated_by TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                model TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('running', 'completed', 'failed')),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                input_bytes INTEGER NOT NULL,
                estimated_input_tokens INTEGER NOT NULL,
                traffic_type TEXT NOT NULL DEFAULT 'semantic',
                input_tokens INTEGER,
                output_tokens INTEGER,
                thinking_tokens INTEGER,
                total_tokens INTEGER,
                estimated_cost_usd REAL,
                latency_ms INTEGER,
                outcome TEXT,
                validation TEXT,
                delta_id TEXT,
                error_class TEXT,
                error_message TEXT
            );
            CREATE INDEX IF NOT EXISTS delta_generation_runs_key
                ON delta_generation_runs(obs_before, obs_after, generated_by, prompt_version);

            CREATE TABLE IF NOT EXISTS delta_generation_leases (
                lease_id TEXT PRIMARY KEY,
                obs_before TEXT NOT NULL,
                obs_after TEXT NOT NULL,
                generated_by TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active', 'completed', 'failed')),
                run_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                traffic_type TEXT NOT NULL DEFAULT 'semantic',
                finished_at TEXT,
                delta_id TEXT,
                outcome TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS delta_generation_leases_key
                ON delta_generation_leases(obs_before, obs_after, generated_by, prompt_version);

            CREATE TABLE IF NOT EXISTS strategy_sessions (
                session_id TEXT PRIMARY KEY,
                period_from TEXT NOT NULL,
                period_to TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                manifest_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('running', 'completed', 'failed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                brief_id TEXT,
                document TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS strategy_sessions_period
                ON strategy_sessions(period_from, period_to, strategy_version, created_at DESC);

            CREATE TABLE IF NOT EXISTS strategy_leases (
                lease_id TEXT PRIMARY KEY,
                period_from TEXT NOT NULL,
                period_to TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active', 'completed', 'failed')),
                session_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS briefs (
                brief_id TEXT PRIMARY KEY,
                strategy_session_id TEXT,
                created_at TEXT NOT NULL,
                document TEXT NOT NULL
            );
            """
        )
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(delta_generation_runs)"
            ).fetchall()
        }
        if "traffic_type" not in columns:
            self.connection.execute(
                "ALTER TABLE delta_generation_runs ADD COLUMN traffic_type TEXT NOT NULL DEFAULT 'semantic'"
            )
        for column in ("obs_before_hash", "obs_after_hash"):
            if column not in columns:
                self.connection.execute(
                    f"ALTER TABLE delta_generation_runs ADD COLUMN {column} TEXT"
                )
        lease_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(delta_generation_leases)"
            ).fetchall()
        }
        if "traffic_type" not in lease_columns:
            self.connection.execute(
                "ALTER TABLE delta_generation_leases ADD COLUMN traffic_type TEXT NOT NULL DEFAULT 'semantic'"
            )
        self.connection.commit()

    @staticmethod
    def _document(model: Any, *, by_alias: bool = False) -> str:
        return json.dumps(
            model.model_dump(mode="json", by_alias=by_alias),
            sort_keys=True,
            separators=(",", ":"),
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "LocalBackend":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def latest_observation(self, entity: str, source: str) -> Observation | None:
        row = self.connection.execute(
            """
            SELECT document FROM observations
            WHERE entity = ? AND source = ? AND status = 'ok'
            ORDER BY fetched_at DESC, rowid DESC
            LIMIT 1
            """,
            (entity, source),
        ).fetchone()
        return Observation.model_validate_json(row["document"]) if row else None

    def put_raw(
        self,
        entity: str,
        source: str,
        obs_id: str,
        payload: bytes,
        *,
        suffix: str = ".json",
    ) -> str:
        raw_root = self.settings.raw.resolve()
        path = (raw_root / entity / source / f"{obs_id}{suffix}").resolve()
        if raw_root not in path.parents:
            raise ValueError("raw payload path escaped the configured root")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return path.as_uri()

    def get_raw(self, content_ref: str) -> bytes:
        parsed = urlparse(content_ref)
        if parsed.scheme != "file":
            raise ValueError(f"unsupported local content reference: {content_ref}")
        path = Path(unquote(parsed.path)).resolve()
        raw_root = self.settings.raw.resolve()
        if raw_root not in path.parents:
            raise ValueError("raw content reference escaped the configured root")
        return path.read_bytes()

    def insert_observation(self, observation: Observation) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO observations
                    (obs_id, entity, source, fetched_at, status, content_hash, document)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.obs_id,
                    observation.entity,
                    observation.source,
                    observation.fetched_at.isoformat(),
                    observation.status.value,
                    observation.content_hash,
                    self._document(observation),
                ),
            )

    def insert_delta(self, delta: Delta, *, enqueue: bool = True) -> None:
        if delta.schema_version is not DeltaSchemaVersion.V2:
            raise ValueError("local canonical Delta writes accept only delta@2")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO deltas
                    (delta_id, entity, source, computed_at, triage, document)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    delta.delta_id,
                    delta.entity,
                    delta.source,
                    delta.computed_at.isoformat(),
                    delta.triage.value,
                    self._document(delta),
                ),
            )
            if enqueue and delta.triage is Triage.MEANINGFUL:
                self.connection.execute(
                    "INSERT INTO delta_outbox(delta_id) VALUES (?)",
                    (delta.delta_id,),
                )

    def publish_delta(self, delta: Delta) -> str:
        if delta.schema_version is not DeltaSchemaVersion.V2:
            raise ValueError("only canonical delta@2 rows may be published")
        if delta.triage is not Triage.MEANINGFUL:
            raise ValueError("only meaningful Deltas may be published")
        if os.getenv("TYCHO_ANALYST_MODE") == "shadow":
            try:
                from pipeline.gemini_analyst import run_shadow_sync

                run_shadow_sync(delta, self.config, self)
            except Exception as exc:
                # Shadow analysis cannot block the authoritative stub/outbox.
                print(f"shadow analyst failed for {delta.delta_id}: {exc}")
        try:
            process_delta(delta, self.config, self)
        except Exception as exc:
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE delta_outbox
                    SET attempts = attempts + 1, last_error = ?
                    WHERE delta_id = ?
                    """,
                    (str(exc), delta.delta_id),
                )
            raise
        with self.connection:
            self.connection.execute(
                """
                UPDATE delta_outbox
                SET state = 'published', attempts = attempts + 1,
                    last_error = NULL, published_at = ?
                WHERE delta_id = ?
                """,
                (datetime.now().astimezone().isoformat(), delta.delta_id),
            )
        return f"local:{delta.delta_id}"

    def process_pending(self) -> list[dict[str, str]]:
        rows = self.connection.execute(
            """
            SELECT d.document FROM delta_outbox o
            JOIN deltas d USING(delta_id)
            WHERE o.state = 'pending'
            ORDER BY d.computed_at
            """
        ).fetchall()
        results: list[dict[str, str]] = []
        for row in rows:
            delta = Delta.model_validate_json(row["document"])
            try:
                self.publish_delta(delta)
                results.append({"delta_id": delta.delta_id, "outcome": "published"})
            except Exception as exc:
                results.append(
                    {"delta_id": delta.delta_id, "outcome": "failed", "error": str(exc)}
                )
        return results

    def bump_verified(self, entity: str, scopes: list[str], verified_at: datetime) -> int:
        if not scopes:
            return 0
        placeholders = ",".join("?" for _ in scopes)
        rows = self.connection.execute(
            f"""
            SELECT document FROM claims
            WHERE entity = ? AND status = 'active' AND scope IN ({placeholders})
            """,
            (entity, *scopes),
        ).fetchall()
        with self.connection:
            for row in rows:
                claim = Claim.model_validate_json(row["document"])
                data = claim.model_dump(mode="python", by_alias=True)
                data["last_verified_at"] = verified_at
                updated = Claim.model_validate(data)
                self._update_claim_row(updated)
        return len(rows)

    def create_claim(self, claim: Claim) -> None:
        document = self._document(claim, by_alias=True)
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO claims
                    (claim_id, entity, scope, status, version, document)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    claim.claim_id,
                    claim.entity,
                    claim.scope,
                    claim.status.value,
                    claim.version,
                    document,
                ),
            )
        if cursor.rowcount == 0:
            existing = self.get_claim(claim.claim_id)
            if existing != claim:
                raise ValueError(f"claim ID collision with different content: {claim.claim_id}")

    def get_claim(self, claim_id: str) -> Claim | None:
        row = self.connection.execute(
            "SELECT document FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        return Claim.model_validate_json(row["document"]) if row else None

    def get_delta(self, delta_id: str) -> Delta | None:
        row = self.connection.execute(
            "SELECT document FROM deltas WHERE delta_id = ?", (delta_id,)
        ).fetchone()
        if not row:
            return None
        delta = Delta.model_validate_json(row["document"])
        return delta if delta.schema_version is DeltaSchemaVersion.V2 else None

    def get_observation(self, obs_id: str) -> Observation | None:
        row = self.connection.execute(
            "SELECT document FROM observations WHERE obs_id = ?", (obs_id,)
        ).fetchone()
        return Observation.model_validate_json(row["document"]) if row else None

    def find_delta_by_comparison_id(self, comparison_id: str) -> Delta | None:
        rows = self.connection.execute("SELECT document FROM deltas").fetchall()
        for row in rows:
            delta = Delta.model_validate_json(row["document"])
            if (
                delta.schema_version is DeltaSchemaVersion.V2
                and delta.comparison_id == comparison_id
            ):
                return delta
        return None

    @staticmethod
    def _generation_lease_id(
        obs_before: str, obs_after: str, generated_by: str, prompt_version: str
    ) -> str:
        return lease_document_id(
            f"{obs_before}:{obs_after}", generated_by, prompt_version
        )

    def acquire_delta_generation_lease(
        self,
        obs_before: str,
        obs_after: str,
        generated_by: str,
        prompt_version: str,
        run_id: str,
        started_at: datetime,
        lease_expires_at: datetime,
        traffic_type: str = "semantic",
    ) -> DeltaGenerationLeaseDecision:
        """Atomically acquire one semantic generation identity."""
        lease_id = self._generation_lease_id(
            obs_before, obs_after, generated_by, prompt_version
        )
        now_text = started_at.isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM delta_generation_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    """
                    INSERT INTO delta_generation_leases
                        (lease_id, obs_before, obs_after, generated_by, prompt_version,
                         state, run_id, attempt, started_at, lease_expires_at,
                         traffic_type)
                    VALUES (?, ?, ?, ?, ?, 'active', ?, 1, ?, ?, ?)
                    """,
                    (
                        lease_id,
                        obs_before,
                        obs_after,
                        generated_by,
                        prompt_version,
                        run_id,
                        now_text,
                        lease_expires_at.isoformat(),
                        traffic_type,
                    ),
                )
                self.connection.commit()
                return DeltaGenerationLeaseDecision("acquired", run_id, 1)

            if row["state"] == "completed" and not (
                traffic_type in {"semantic", "historical_backfill"}
                and row["traffic_type"] == "shadow"
            ):
                self.connection.commit()
                return DeltaGenerationLeaseDecision(
                    "completed",
                    row["run_id"],
                    int(row["attempt"]),
                    row["delta_id"],
                    row["outcome"],
                )
            expires_at = datetime.fromisoformat(row["lease_expires_at"])
            if row["state"] == "active" and lease_is_active(expires_at, started_at):
                self.connection.commit()
                return DeltaGenerationLeaseDecision(
                    "active", row["run_id"], int(row["attempt"])
                )

            attempt = int(row["attempt"]) + 1
            self.connection.execute(
                """
                UPDATE delta_generation_leases
                SET state = 'active', run_id = ?, attempt = ?, started_at = ?,
                    lease_expires_at = ?, traffic_type = ?, finished_at = NULL,
                    delta_id = NULL, outcome = NULL, error = NULL
                WHERE lease_id = ?
                """,
                (
                    run_id,
                    attempt,
                    now_text,
                    lease_expires_at.isoformat(),
                    traffic_type,
                    lease_id,
                ),
            )
            self.connection.commit()
            return DeltaGenerationLeaseDecision("acquired", run_id, attempt)
        except Exception:
            self.connection.rollback()
            raise

    def start_delta_generation_run(
        self,
        run_id: str,
        obs_before: str,
        obs_after: str,
        generated_by: str,
        prompt_version: str,
        model: str,
        attempt: int,
        started_at: datetime,
        input_bytes: int,
        estimated_input_tokens: int,
        traffic_type: str = "semantic",
        obs_before_hash: str | None = None,
        obs_after_hash: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO delta_generation_runs
                    (run_id, obs_before, obs_after, generated_by, prompt_version,
                     model, attempt, state, started_at, input_bytes,
                     estimated_input_tokens, traffic_type, obs_before_hash,
                     obs_after_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    obs_before,
                    obs_after,
                    generated_by,
                    prompt_version,
                    model,
                    attempt,
                    started_at.isoformat(),
                    input_bytes,
                    estimated_input_tokens,
                    traffic_type,
                    obs_before_hash,
                    obs_after_hash,
                ),
            )

    def finish_delta_generation_run(
        self,
        run_id: str,
        finished_at: datetime,
        *,
        outcome: str | None,
        validation: str,
        delta_id: str | None = None,
        usage: dict[str, int | float] | None = None,
        latency_ms: int | None = None,
        error_class: str | None = None,
        error_message: str | None = None,
    ) -> None:
        usage = usage or {}
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE delta_generation_runs
                SET state = ?, finished_at = ?, input_tokens = ?, output_tokens = ?,
                    thinking_tokens = ?, total_tokens = ?, estimated_cost_usd = ?,
                    latency_ms = ?, outcome = ?, validation = ?, delta_id = ?,
                    error_class = ?, error_message = ?
                WHERE run_id = ?
                """,
                (
                    "failed" if error_class else "completed",
                    finished_at.isoformat(),
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    usage.get("thinking_tokens"),
                    usage.get("total_tokens"),
                    usage.get("estimated_cost_usd"),
                    latency_ms,
                    outcome,
                    validation,
                    delta_id,
                    error_class,
                    error_message,
                    run_id,
                ),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown delta generation run: {run_id}")

    def complete_delta_generation_lease(
        self,
        obs_before: str,
        obs_after: str,
        generated_by: str,
        prompt_version: str,
        run_id: str,
        finished_at: datetime,
        *,
        delta_id: str | None,
        outcome: str,
    ) -> None:
        lease_id = self._generation_lease_id(
            obs_before, obs_after, generated_by, prompt_version
        )
        with self.connection:
            row = self.connection.execute(
                "SELECT state, run_id FROM delta_generation_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown delta generation lease: {lease_id}")
            if row["state"] == "completed" and row["run_id"] == run_id:
                return
            if row["state"] != "active" or row["run_id"] != run_id:
                raise RuntimeError("delta generation lease is no longer owned by this run")
            self.connection.execute(
                """
                UPDATE delta_generation_leases
                SET state = 'completed', finished_at = ?, delta_id = ?,
                    outcome = ?, error = NULL
                WHERE lease_id = ?
                """,
                (finished_at.isoformat(), delta_id, outcome, lease_id),
            )

    def fail_delta_generation_lease(
        self,
        obs_before: str,
        obs_after: str,
        generated_by: str,
        prompt_version: str,
        run_id: str,
        finished_at: datetime,
        error: str,
    ) -> None:
        lease_id = self._generation_lease_id(
            obs_before, obs_after, generated_by, prompt_version
        )
        with self.connection:
            row = self.connection.execute(
                "SELECT state, run_id FROM delta_generation_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if row is None or row["state"] != "active" or row["run_id"] != run_id:
                return
            self.connection.execute(
                """
                UPDATE delta_generation_leases
                SET state = 'failed', finished_at = ?, error = ?
                WHERE lease_id = ?
                """,
                (finished_at.isoformat(), error[:500], lease_id),
            )

    def retryable_delta_generation_pairs(self, now: datetime) -> list[GenerationPair]:
        rows = self.connection.execute(
            """
            SELECT * FROM delta_generation_leases
            WHERE state = 'failed'
               OR (state = 'active' AND lease_expires_at <= ?)
            ORDER BY started_at, rowid
            """,
            (now.isoformat(),),
        ).fetchall()
        pairs: list[GenerationPair] = []
        for row in rows:
            before = self.get_observation(row["obs_before"])
            after = self.get_observation(row["obs_after"])
            if before is not None and after is not None:
                pairs.append(
                    GenerationPair(
                        before,
                        after,
                        generated_by=row["generated_by"],
                        prompt_version=row["prompt_version"],
                    )
                )
        return pairs

    def generation_runs(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM delta_generation_runs ORDER BY started_at, rowid"
        ).fetchall()
        return [dict(row) for row in rows]

    def active_claims(self, entity: str, scopes: list[str]) -> list[Claim]:
        if not scopes:
            return []
        placeholders = ",".join("?" for _ in scopes)
        rows = self.connection.execute(
            f"""
            SELECT document FROM claims
            WHERE entity = ? AND status = 'active' AND scope IN ({placeholders})
            ORDER BY scope, rowid
            """,
            (entity, *scopes),
        ).fetchall()
        return [Claim.model_validate_json(row["document"]) for row in rows]

    def active_disputes(self, claim_id: str) -> list[Claim]:
        rows = self.connection.execute(
            "SELECT document FROM claims WHERE status = 'active' ORDER BY rowid"
        ).fetchall()
        claims = [Claim.model_validate_json(row["document"]) for row in rows]
        return [claim for claim in claims if claim.disputes == claim_id]

    def start_analyst_run(
        self,
        run_id: str,
        delta_id: str,
        mode: str,
        analyst_version: str,
        model: str,
        input_document: str,
        started_at: datetime,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO analyst_runs
                    (run_id, delta_id, mode, analyst_version, model, state,
                     started_at, input_document)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    run_id,
                    delta_id,
                    mode,
                    analyst_version,
                    model,
                    started_at.isoformat(),
                    input_document,
                ),
            )

    def finish_analyst_run(
        self,
        run_id: str,
        *,
        actions: list[dict[str, Any]],
        final_text: str,
        finished_at: datetime,
        error: str | None = None,
    ) -> None:
        state = "failed" if error else "completed"
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE analyst_runs
                SET state = ?, finished_at = ?, actions_document = ?,
                    final_text = ?, error = ?
                WHERE run_id = ?
                """,
                (
                    state,
                    finished_at.isoformat(),
                    json.dumps(actions, sort_keys=True, separators=(",", ":")),
                    final_text,
                    error,
                    run_id,
                ),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown analyst run: {run_id}")

    def acquire_analyst_lease(
        self,
        delta_id: str,
        mode: str,
        analyst_version: str,
        run_id: str,
        started_at: datetime,
        lease_expires_at: datetime,
        *,
        force: bool = False,
    ) -> AnalystLeaseDecision:
        lease_id = lease_document_id(delta_id, mode, analyst_version)
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM analyst_run_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    """
                    INSERT INTO analyst_run_leases
                        (lease_id, delta_id, mode, analyst_version, state, run_id,
                         attempt, started_at, lease_expires_at)
                    VALUES (?, ?, ?, ?, 'active', ?, 1, ?, ?)
                    """,
                    (
                        lease_id,
                        delta_id,
                        mode,
                        analyst_version,
                        run_id,
                        started_at.isoformat(),
                        lease_expires_at.isoformat(),
                    ),
                )
                return AnalystLeaseDecision("acquired", run_id, 1)

            if row["state"] == "completed" and not force:
                return AnalystLeaseDecision("completed", row["run_id"], row["attempt"])

            expires_at = datetime.fromisoformat(row["lease_expires_at"])
            if row["state"] == "active" and lease_is_active(expires_at, started_at):
                return AnalystLeaseDecision("active", row["run_id"], row["attempt"])

            attempt = int(row["attempt"]) + 1
            self.connection.execute(
                """
                UPDATE analyst_run_leases
                SET delta_id = ?, mode = ?, analyst_version = ?, state = 'active',
                    run_id = ?, attempt = ?, started_at = ?, lease_expires_at = ?,
                    finished_at = NULL, error = NULL
                WHERE lease_id = ?
                """,
                (
                    delta_id,
                    mode,
                    analyst_version,
                    run_id,
                    attempt,
                    started_at.isoformat(),
                    lease_expires_at.isoformat(),
                    lease_id,
                ),
            )
            return AnalystLeaseDecision("acquired", run_id, attempt)

    def complete_analyst_lease(
        self,
        delta_id: str,
        mode: str,
        analyst_version: str,
        run_id: str,
        finished_at: datetime,
    ) -> None:
        lease_id = lease_document_id(delta_id, mode, analyst_version)
        with self.connection:
            row = self.connection.execute(
                "SELECT state, run_id FROM analyst_run_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown analyst lease: {lease_id}")
            if row["state"] == "completed" and row["run_id"] == run_id:
                return
            if row["state"] != "active" or row["run_id"] != run_id:
                raise RuntimeError("analyst lease is no longer owned by this run")
            self.connection.execute(
                """
                UPDATE analyst_run_leases
                SET state = 'completed', finished_at = ?, error = NULL
                WHERE lease_id = ?
                """,
                (finished_at.isoformat(), lease_id),
            )

    def fail_analyst_lease(
        self,
        delta_id: str,
        mode: str,
        analyst_version: str,
        run_id: str,
        finished_at: datetime,
        error: str,
    ) -> None:
        lease_id = lease_document_id(delta_id, mode, analyst_version)
        with self.connection:
            row = self.connection.execute(
                "SELECT state, run_id FROM analyst_run_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if row is None or row["state"] != "active" or row["run_id"] != run_id:
                return
            self.connection.execute(
                """
                UPDATE analyst_run_leases
                SET state = 'failed', finished_at = ?, error = ?
                WHERE lease_id = ?
                """,
                (finished_at.isoformat(), error, lease_id),
            )

    def has_completed_analyst_run(
        self, delta_id: str, mode: str, analyst_version: str
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM analyst_runs
            WHERE delta_id = ? AND mode = ? AND analyst_version = ?
              AND state = 'completed'
            LIMIT 1
            """,
            (delta_id, mode, analyst_version),
        ).fetchone()
        return row is not None

    def analyst_runs(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM analyst_runs ORDER BY started_at, rowid"
        ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["actions"] = (
                json.loads(item.pop("actions_document"))
                if item.get("actions_document")
                else []
            )
            results.append(item)
        return results

    def _update_claim_row(self, claim: Claim) -> None:
        cursor = self.connection.execute(
            """
            UPDATE claims
            SET entity = ?, scope = ?, status = ?, version = ?, document = ?
            WHERE claim_id = ?
            """,
            (
                claim.entity,
                claim.scope,
                claim.status.value,
                claim.version,
                self._document(claim, by_alias=True),
                claim.claim_id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown claim: {claim.claim_id}")

    def update_claim(self, claim_id: str, fields: dict[str, Any]) -> None:
        current = self.get_claim(claim_id)
        if current is None:
            raise KeyError(f"unknown claim: {claim_id}")
        data = current.model_dump(mode="python", by_alias=True)
        data.update(fields)
        updated = Claim.model_validate(data)
        with self.connection:
            self._update_claim_row(updated)

    def record_alert(
        self,
        alert_id: str,
        claim_id: str,
        delta_id: str,
        severity: str,
        kind: str,
        message: str,
        created_at: datetime,
    ) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO alerts
                    (alert_id, claim_id, delta_id, severity, kind, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    claim_id,
                    delta_id,
                    severity,
                    kind,
                    message,
                    created_at.isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def alerts(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM alerts ORDER BY created_at, rowid"
        ).fetchall()
        return [dict(row) for row in rows]

    def create_receipt_once(self, dedup_key: str, receipt: DeliveryReceipt) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO receipts
                    (receipt_id, dedup_key, claim_id, claim_version, context_key, document)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    dedup_key,
                    receipt.claim_id,
                    receipt.claim_version,
                    receipt.context_key,
                    self._document(receipt),
                ),
            )
        return cursor.rowcount == 1

    # --- Strategy sessions, leases, and briefs ------------------------------

    def list_claims(self) -> list[Claim]:
        """Cloud-parity name for the full claim listing."""
        return self.claims()

    def list_canonical_deltas(self) -> list[Delta]:
        """Cloud-parity name for the canonical delta@2 listing."""
        return self.deltas()

    def acquire_strategy_lease(
        self,
        period_from: datetime,
        period_to: datetime,
        strategy_version: str,
        session_id: str,
        started_at: datetime,
        lease_expires_at: datetime,
    ) -> StrategyLeaseDecision:
        """Atomically own one (period, strategy_version) strategy identity."""
        lease_id = strategy_lease_document_id(period_from, period_to, strategy_version)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM strategy_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                self.connection.execute(
                    """
                    INSERT INTO strategy_leases
                        (lease_id, period_from, period_to, strategy_version, state,
                         session_id, attempt, started_at, lease_expires_at)
                    VALUES (?, ?, ?, ?, 'active', ?, 1, ?, ?)
                    """,
                    (
                        lease_id,
                        period_from.isoformat(),
                        period_to.isoformat(),
                        strategy_version,
                        session_id,
                        started_at.isoformat(),
                        lease_expires_at.isoformat(),
                    ),
                )
                self.connection.commit()
                return StrategyLeaseDecision("acquired", session_id, 1)

            if row["state"] == "completed":
                self.connection.commit()
                return StrategyLeaseDecision(
                    "completed", row["session_id"], int(row["attempt"])
                )
            expires_at = datetime.fromisoformat(row["lease_expires_at"])
            if row["state"] == "active" and strategy_lease_is_active(expires_at, started_at):
                self.connection.commit()
                return StrategyLeaseDecision(
                    "active", row["session_id"], int(row["attempt"])
                )

            attempt = int(row["attempt"]) + 1
            self.connection.execute(
                """
                UPDATE strategy_leases
                SET state = 'active', session_id = ?, attempt = ?, started_at = ?,
                    lease_expires_at = ?, finished_at = NULL, error = NULL
                WHERE lease_id = ?
                """,
                (
                    session_id,
                    attempt,
                    started_at.isoformat(),
                    lease_expires_at.isoformat(),
                    lease_id,
                ),
            )
            self.connection.commit()
            return StrategyLeaseDecision("acquired", session_id, attempt)
        except Exception:
            self.connection.rollback()
            raise

    def complete_strategy_lease(
        self,
        period_from: datetime,
        period_to: datetime,
        strategy_version: str,
        session_id: str,
        finished_at: datetime,
    ) -> None:
        lease_id = strategy_lease_document_id(period_from, period_to, strategy_version)
        with self.connection:
            self.connection.execute(
                """
                UPDATE strategy_leases
                SET state = 'completed', finished_at = ?, error = NULL
                WHERE lease_id = ? AND session_id = ?
                """,
                (finished_at.isoformat(), lease_id, session_id),
            )

    def fail_strategy_lease(
        self,
        period_from: datetime,
        period_to: datetime,
        strategy_version: str,
        session_id: str,
        finished_at: datetime,
        error: str,
    ) -> None:
        """Leave the identity retryable: a failed session is not a completed one."""
        lease_id = strategy_lease_document_id(period_from, period_to, strategy_version)
        with self.connection:
            self.connection.execute(
                """
                UPDATE strategy_leases
                SET state = 'failed', finished_at = ?, error = ?
                WHERE lease_id = ? AND session_id = ?
                """,
                (finished_at.isoformat(), error[:500], lease_id, session_id),
            )

    def create_strategy_session(self, session: StrategySession) -> None:
        """Write-once session creation; a rerun creates a new session ID."""
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO strategy_sessions
                    (session_id, period_from, period_to, strategy_version,
                     manifest_hash, state, created_at, updated_at, brief_id, document)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.period.from_.isoformat(),
                    session.period.to.isoformat(),
                    session.strategy_version,
                    session.manifest_hash,
                    session.state.value,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.brief_id,
                    self._document(session, by_alias=True),
                ),
            )

    def finalize_strategy_session(self, session: StrategySession) -> None:
        """Move a running session to its single terminal state, once."""
        if session.state is SessionState.RUNNING:
            raise ValueError("finalize_strategy_session requires a terminal state")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE strategy_sessions
                SET state = ?, updated_at = ?, brief_id = ?, document = ?
                WHERE session_id = ? AND state = 'running'
                """,
                (
                    session.state.value,
                    session.updated_at.isoformat(),
                    session.brief_id,
                    self._document(session, by_alias=True),
                    session.session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"strategy session {session.session_id} is not running; it is write-once"
                )

    def get_strategy_session(self, session_id: str) -> StrategySession | None:
        row = self.connection.execute(
            "SELECT document FROM strategy_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return None if row is None else StrategySession.model_validate_json(row["document"])

    def strategy_sessions(self) -> list[StrategySession]:
        rows = self.connection.execute(
            "SELECT document FROM strategy_sessions ORDER BY created_at, rowid"
        ).fetchall()
        return [StrategySession.model_validate_json(row["document"]) for row in rows]

    def create_brief_once(self, brief: Brief) -> bool:
        """Briefs are write-once renders; a second write is refused, not merged."""
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO briefs (brief_id, strategy_session_id, created_at, document)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        brief.brief_id,
                        brief.strategy_session_id,
                        brief.created_at.isoformat(),
                        self._document(brief, by_alias=True),
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_brief(self, brief_id: str) -> Brief | None:
        row = self.connection.execute(
            "SELECT document FROM briefs WHERE brief_id = ?", (brief_id,)
        ).fetchone()
        return None if row is None else Brief.model_validate_json(row["document"])

    def briefs(self) -> list[Brief]:
        rows = self.connection.execute(
            "SELECT document FROM briefs ORDER BY created_at, rowid"
        ).fetchall()
        return [Brief.model_validate_json(row["document"]) for row in rows]

    def observations(self) -> list[Observation]:
        rows = self.connection.execute(
            "SELECT document FROM observations ORDER BY fetched_at, rowid"
        ).fetchall()
        return [Observation.model_validate_json(row["document"]) for row in rows]

    def deltas(self) -> list[Delta]:
        rows = self.connection.execute(
            "SELECT document FROM deltas ORDER BY computed_at, rowid"
        ).fetchall()
        return [
            delta
            for row in rows
            if (delta := Delta.model_validate_json(row["document"])).schema_version
            is DeltaSchemaVersion.V2
        ]

    def claims(self) -> list[Claim]:
        rows = self.connection.execute(
            "SELECT document FROM claims ORDER BY rowid"
        ).fetchall()
        return [Claim.model_validate_json(row["document"]) for row in rows]

    def receipts(self) -> list[DeliveryReceipt]:
        rows = self.connection.execute(
            "SELECT document FROM receipts ORDER BY rowid"
        ).fetchall()
        return [DeliveryReceipt.model_validate_json(row["document"]) for row in rows]

    def pending_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM delta_outbox WHERE state = 'pending'"
        ).fetchone()
        return int(row["count"])

    def stats(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for table in (
            "observations",
            "deltas",
            "claims",
            "receipts",
            "analyst_runs",
            "alerts",
            "strategy_sessions",
            "briefs",
        ):
            row = self.connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
            result[table] = int(row["count"])
        result["outbox_pending"] = self.pending_count()
        return result
