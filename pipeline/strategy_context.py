"""Deterministic bounded context for one strategy session.

Everything a strategy agent ever sees is assembled here, from governed claims
and canonical Deltas only.  There is no web access, no GCS read, and no raw
payload: new external evidence must enter through acquisition first.

Selection, metrics, and the input budget are all decided before any model call,
so an oversized or unsupportable session fails durably instead of quietly
dropping evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from pipeline.strategy_evidence import (
    claim_is_stale,
    source_family,
    staleness_threshold,
)
from schemas.claim import Claim, ClaimClass, ClaimStatus, Severity
from schemas.config import TychoConfig
from schemas.delta import (
    CANONICAL_GENERATED_BY,
    CANONICAL_PROMPT_VERSION,
    Delta,
    DeltaSchemaVersion,
    Triage,
)
from schemas.strategy import (
    STRATEGY_QUESTION,
    ManifestEntry,
    MetricEvidence,
    SessionPeriod,
    manifest_hash,
)

DEFAULT_PERIOD_DAYS = 7
MAX_CONTEXT_CLAIMS = 60
MAX_CONTEXT_BYTES = 200_000
MAX_CONTEXT_ESTIMATED_TOKENS = 50_000

_SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.NOTABLE: 1, Severity.CONTEXT: 2}


class StrategyContextTooLarge(ValueError):
    """The bounded context exceeds its budget; the session fails before Gemini."""


class StrategyContextStore(Protocol):
    """The read-only surface the context builder is allowed to use."""

    def list_claims(self) -> list[Claim]: ...

    def list_canonical_deltas(self) -> list[Delta]: ...

    def get_claim(self, claim_id: str) -> Claim | None: ...

    def get_delta(self, delta_id: str) -> Delta | None: ...


def default_period(now: datetime, days: int = DEFAULT_PERIOD_DAYS) -> SessionPeriod:
    """The bounded window a session reasons over, ending at ``now``."""
    if days < 1:
        raise ValueError("a strategy period must span at least one day")
    return SessionPeriod(**{"from": now - timedelta(days=days), "to": now})


def is_canonical(delta: Delta) -> bool:
    return (
        delta.schema_version is DeltaSchemaVersion.V2
        and delta.generated_by == CANONICAL_GENERATED_BY
        and delta.prompt_version == CANONICAL_PROMPT_VERSION
    )


@dataclass(frozen=True)
class StrategyContext:
    """The complete, bounded input to one strategy session."""

    period: SessionPeriod
    manifest: list[ManifestEntry]
    metrics: list[MetricEvidence]
    document: str
    manifest_hash: str
    input_bytes: int
    estimated_input_tokens: int
    admitted_deltas: dict[str, Delta]
    excluded_claim_ids: list[str]

    @property
    def is_empty(self) -> bool:
        return not self.manifest


def _delta_view(delta: Delta) -> dict[str, Any]:
    """Bounded governed-Delta metadata: no raw payload, no snapshot text."""
    return {
        "delta_id": delta.delta_id,
        "entity": delta.entity,
        "source": delta.source,
        "source_family": source_family(delta.entity, delta.source),
        "computed_at": delta.computed_at.isoformat(),
        "triage": delta.triage.value,
        "routed_to": list(delta.routed_to),
        "changes": [
            {
                "category": change.category.value if change.category else None,
                "scope": change.scope.value if change.scope else None,
                "statement": change.statement,
            }
            for change in delta.changes
        ],
    }


def _claim_view(claim: Claim, entry: ManifestEntry) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "claim_version": claim.version,
        "entity": claim.entity,
        "scope": claim.scope,
        "class": claim.class_.value,
        "inference_kind": claim.inference_kind.value if claim.inference_kind else None,
        "statement": claim.statement,
        "rationale": claim.rationale,
        "confidence": claim.confidence.value,
        "severity": claim.severity.value,
        "last_verified_at": claim.last_verified_at.isoformat(),
        "stale": entry.stale,
        "staleness_days": entry.staleness_days,
        "delta_ids": list(entry.delta_ids),
        "source_families": list(entry.source_families),
    }


def _admissible_claims(
    store: StrategyContextStore, config: TychoConfig, now: datetime
) -> tuple[list[tuple[Claim, ManifestEntry, list[Delta]]], list[str]]:
    """Keep only active, non-operational claims resting on canonical v2 rows."""
    admitted: list[tuple[Claim, ManifestEntry, list[Delta]]] = []
    excluded: list[str] = []
    for claim in store.list_claims():
        if claim.status is not ClaimStatus.ACTIVE or claim.class_ is ClaimClass.OPERATIONAL:
            continue
        deltas: list[Delta] = []
        canonical = True
        for item in claim.evidence:
            delta = store.get_delta(item.delta_id)
            if delta is None or not is_canonical(delta):
                canonical = False
                break
            deltas.append(delta)
        if not canonical or not deltas:
            excluded.append(claim.claim_id)
            continue
        entry = ManifestEntry(
            claim_id=claim.claim_id,
            claim_version=claim.version,
            entity=claim.entity,
            scope=claim.scope,
            confidence=claim.confidence.value,
            severity=claim.severity.value,
            delta_ids=[delta.delta_id for delta in deltas],
            source_families=sorted(
                {source_family(delta.entity, delta.source) for delta in deltas}
            ),
            last_verified_at=claim.last_verified_at,
            stale=claim_is_stale(claim, config, now),
            staleness_days=staleness_threshold(config, claim.scope),
        )
        admitted.append((claim, entry, deltas))
    return admitted, sorted(excluded)


def _selection_key(pair: tuple[Claim, ManifestEntry, list[Delta]]) -> tuple[Any, ...]:
    """Rank notable and recent claims first, then break ties by ID.

    This runs before any model call so the same store always yields the same
    manifest, and a truncated selection is never silent: the cap is explicit.
    """
    claim, _, _ = pair
    return (
        _SEVERITY_RANK[claim.severity],
        -claim.last_verified_at.timestamp(),
        claim.claim_id,
    )


def _period_metrics(
    deltas: list[Delta], period: SessionPeriod, now: datetime
) -> list[MetricEvidence]:
    """Compute every session metric from canonical Deltas, carrying their IDs."""
    in_period = [
        delta
        for delta in deltas
        if period.from_ <= delta.computed_at < period.to
    ]
    meaningful = [delta for delta in in_period if delta.triage is Triage.MEANINGFUL]
    noise = [delta for delta in in_period if delta.triage is Triage.NOISE]

    change_ids = [delta.delta_id for delta in meaningful for _ in delta.changes]
    categories: dict[str, list[str]] = {}
    scopes: dict[str, list[str]] = {}
    entities: dict[str, list[str]] = {}
    for delta in meaningful:
        entities.setdefault(delta.entity, []).append(delta.delta_id)
        for change in delta.changes:
            if change.category:
                categories.setdefault(change.category.value, []).append(delta.delta_id)
            if change.scope:
                scopes.setdefault(change.scope.value, []).append(delta.delta_id)

    latest = max((delta.computed_at for delta in in_period), default=None)
    recency_days = 0 if latest is None else max(0, (now - latest).days)
    recency_ids = (
        [delta.delta_id for delta in in_period if delta.computed_at == latest]
        if latest is not None
        else []
    )

    return [
        MetricEvidence(
            name="meaningful_change_count",
            value=len(change_ids),
            delta_ids=sorted(set(change_ids)),
        ),
        MetricEvidence(
            name="meaningful_delta_count",
            value=len(meaningful),
            delta_ids=sorted(delta.delta_id for delta in meaningful),
        ),
        MetricEvidence(
            name="noise_delta_count",
            value=len(noise),
            delta_ids=sorted(delta.delta_id for delta in noise),
        ),
        MetricEvidence(
            name="change_category_count",
            value=len(categories),
            delta_ids=sorted({value for ids in categories.values() for value in ids}),
        ),
        MetricEvidence(
            name="change_scope_count",
            value=len(scopes),
            delta_ids=sorted({value for ids in scopes.values() for value in ids}),
        ),
        MetricEvidence(
            name="entity_coverage",
            value=len(entities),
            delta_ids=sorted({value for ids in entities.values() for value in ids}),
        ),
        MetricEvidence(
            name="days_since_latest_change",
            value=recency_days,
            delta_ids=sorted(recency_ids),
        ),
    ]


def enforce_context_budget(document: str) -> tuple[int, int]:
    """Fail durably rather than truncate evidence a conclusion depends on."""
    input_bytes = len(document.encode("utf-8"))
    estimated_tokens = (input_bytes + 3) // 4
    if input_bytes > MAX_CONTEXT_BYTES or estimated_tokens > MAX_CONTEXT_ESTIMATED_TOKENS:
        raise StrategyContextTooLarge(
            "strategy context is "
            f"{input_bytes:,} bytes (~{estimated_tokens:,} estimated tokens); "
            f"limit is {MAX_CONTEXT_BYTES:,} bytes "
            f"(~{MAX_CONTEXT_ESTIMATED_TOKENS:,} tokens)"
        )
    return input_bytes, estimated_tokens


def build_strategy_context(
    store: StrategyContextStore,
    config: TychoConfig,
    *,
    period: SessionPeriod,
    now: datetime,
    max_claims: int = MAX_CONTEXT_CLAIMS,
) -> StrategyContext:
    """Assemble the only input a strategy session is permitted to reason over."""
    admitted, excluded = _admissible_claims(store, config, now)
    admitted.sort(key=_selection_key)
    selected = admitted[:max_claims]

    manifest = [entry for _, entry, _ in selected]
    admitted_deltas = {
        delta.delta_id: delta for _, _, deltas in selected for delta in deltas
    }

    canonical_deltas = [delta for delta in store.list_canonical_deltas() if is_canonical(delta)]
    metrics = _period_metrics(canonical_deltas, period, now)

    period_new = sorted(
        claim.claim_id
        for claim, _, _ in selected
        if period.from_ <= claim.created_at < period.to
    )
    period_superseded = sorted(
        claim.claim_id
        for claim in store.list_claims()
        if claim.status is ClaimStatus.SUPERSEDED
        and period.from_ <= claim.last_verified_at < period.to
    )

    payload = {
        "question": STRATEGY_QUESTION,
        "period": {
            "from": period.from_.isoformat(),
            "to": period.to.isoformat(),
        },
        "claims": [
            _claim_view(claim, entry) for claim, entry, _ in selected
        ],
        "deltas": [
            _delta_view(admitted_deltas[delta_id]) for delta_id in sorted(admitted_deltas)
        ],
        "metrics": [metric.model_dump(mode="json") for metric in metrics],
        "period_activity": {
            "new_claim_ids": period_new,
            "superseded_claim_ids": period_superseded,
        },
        "selection": {
            "admitted_claims": len(admitted),
            "selected_claims": len(selected),
            "max_claims": max_claims,
            "excluded_noncanonical_claims": len(excluded),
        },
    }
    document = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    input_bytes, estimated_tokens = enforce_context_budget(document)

    return StrategyContext(
        period=period,
        manifest=manifest,
        metrics=metrics,
        document=document,
        manifest_hash=manifest_hash(manifest),
        input_bytes=input_bytes,
        estimated_input_tokens=estimated_tokens,
        admitted_deltas=admitted_deltas,
        excluded_claim_ids=excluded,
    )
