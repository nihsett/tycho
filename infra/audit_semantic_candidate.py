"""Read-only audit of the strict Delta@2 candidate table.

The command reads only.  It loads every candidate Delta row through the strict
Pydantic model, re-reads the immutable observations and write-once raw
snapshots, rebuilds the normalized before/after comparison bundles, and reruns
the *current* grounding and policy validation over them.  It then checks that
every active claim resolves to a meaningful candidate Delta with matching
entity/source and intact lifecycle links.

Nothing is written: no BigQuery insert or rename, no GCS object, no Firestore
document, no Pub/Sub publication, and no Scheduler change.  No model provider is
called; the audit only replays the deterministic Python validators over stored
evidence.

The emitted JSON is deliberately bounded to identifiers, counts, and failure
classes.  Source snapshots, evidence quotes, statements, summaries, triage
reasons, model text, and claim text never enter the report.  Mirrored evidence
is reported as a short fingerprint of the normalized quote, never the quote.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from pydantic import ValidationError

from pipeline.semantic_differ import (
    SemanticDeltaProposal,
    SemanticDifferError,
    build_comparison_bundle,
    comparison_id_for,
    normalize_evidence_text,
    quote_is_grounded,
    validate_grounding,
)
from schemas.claim import Claim, ClaimClass, ClaimStatus
from schemas.delta import (
    CANONICAL_GENERATED_BY,
    CANONICAL_PROMPT_VERSION,
    Delta,
    DeltaSchemaVersion,
    DiffKind,
    Triage,
)
from schemas.observation import Observation, ObservationStatus

AUDIT_VERSION = "semantic-candidate-audit@1"
DEFAULT_PROJECT = "gen-lang-client-0110801105"
DEFAULT_DATASET = "tycho"
DEFAULT_TABLE = "deltas_v2_candidate"
MAX_DETAIL_LENGTH = 300
MAX_LISTED_ITEMS = 200
FINGERPRINT_LENGTH = 16
MARKET_ENTITY = "market"

CANDIDATE_COLUMNS = (
    "schema_version",
    "delta_id",
    "comparison_id",
    "entity",
    "source",
    "obs_before",
    "obs_after",
    "computed_at",
    "diff_kind",
    "generated_by",
    "prompt_version",
    "changes",
    "summary",
    "triage",
    "triage_reason",
    "triage_by",
    "routed_to",
)

OBSERVATION_COLUMNS = (
    "obs_id",
    "entity",
    "source",
    "kind",
    "fetched_at",
    "content_ref",
    "content_hash",
    "adapter_ver",
    "status",
)

# A hard failure means a candidate row or an active claim violates the
# canonical contract, so the audit must exit nonzero.
HARD_FAILURE_CLASSES = frozenset(
    {
        "delta_row_not_loadable",
        "noncanonical_metadata",
        "comparison_id_mismatch",
        "duplicate_delta_id",
        "duplicate_comparison_id",
        "duplicate_observation_pair",
        "self_comparison_pair",
        "observation_document_not_loadable",
        "observation_missing",
        "observation_identity_mismatch",
        "observation_not_clean",
        "chronology_out_of_order",
        "computed_at_precedes_observation",
        "raw_payload_unavailable",
        "raw_payload_hash_mismatch",
        "normalization_failed",
        "proposal_policy_failed",
        "grounding_failed",
        "routed_scope_mismatch",
        "claim_document_not_loadable",
        "claim_evidence_unresolved",
        "claim_evidence_not_meaningful",
        "claim_entity_mismatch",
        "claim_source_mismatch",
        "active_claim_has_superseded_by",
        "claim_supersession_target_missing",
        "claim_supersession_link_broken",
        "claim_dispute_target_missing",
        "claim_dispute_target_not_active",
        "claim_inference_source_diversity_unmet",
    }
)

# An advisory is a true observation about the candidate set that is not a Delta
# contract violation.  Advisories never change the exit code.
ADVISORY_CLASSES = frozenset(
    {
        "evidence_after_present_in_obs_before",
        "cross_source_mirrored_evidence",
    }
)


class CandidateAuditError(RuntimeError):
    """The audit cannot read the inputs it needs."""


@dataclass(frozen=True)
class Finding:
    """One bounded, content-free audit finding."""

    failure_class: str
    subject_kind: str
    subject: str
    detail: str
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value = {
            "class": self.failure_class,
            "subject_kind": self.subject_kind,
            "subject": self.subject,
            "detail": self.detail,
        }
        if self.context:
            value.update(self.context)
        return value


class CandidateAuditReader(Protocol):
    """Read-only access to the four immutable inputs the audit needs."""

    def candidate_rows(self) -> Iterable[Mapping[str, Any]]: ...

    def observation_documents(self) -> Iterable[Mapping[str, Any]]: ...

    def raw_payload(self, content_ref: str) -> bytes: ...

    def claim_documents(self) -> Iterable[Mapping[str, Any]]: ...


def _bounded(value: object) -> str:
    return " ".join(str(value).split())[:MAX_DETAIL_LENGTH]


def _validation_detail(exc: ValidationError) -> str:
    """Summarize a Pydantic failure by location and error type only.

    Pydantic renders the offending input value into ``str(exc)``, and that value
    can be model text or a source quote, so only structural fields are kept.
    """
    parts = sorted(
        {
            f"{'.'.join(str(item) for item in error.get('loc', ()))}:{error.get('type')}"
            for error in exc.errors()
        }
    )
    return _bounded("; ".join(parts) or "validation failed")


def quote_fingerprint(quote: str) -> str:
    """Fingerprint a normalized quote so duplication is reportable, not readable."""
    normalized = normalize_evidence_text(quote).casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:FINGERPRINT_LENGTH]}"


def payload_hash(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def decode_delta_row(row: Mapping[str, Any]) -> Delta:
    """Load one candidate row through the strict Delta model.

    BigQuery stores semantic ``before``/``after`` values as JSON, so the stored
    JSON text is decoded before validation.  Nothing else is normalized: an
    out-of-contract row must fail here rather than be quietly repaired.
    """
    data = dict(row)
    decoded_changes: list[dict[str, Any]] = []
    for raw_change in data.get("changes") or []:
        change = dict(raw_change)
        for name in ("before", "after"):
            value = change.get(name)
            if isinstance(value, str):
                try:
                    change[name] = json.loads(value)
                except json.JSONDecodeError:
                    pass
        decoded_changes.append(change)
    data["changes"] = decoded_changes
    data["routed_to"] = list(data.get("routed_to") or [])
    return Delta.model_validate(data)


def proposal_from_delta(delta: Delta) -> SemanticDeltaProposal:
    """Rebuild the model-owned proposal that produced one stored Delta.

    Observation IDs stay attached through ``EvidenceQuote`` so the rerun also
    re-checks the observation side of every quote.
    """
    return SemanticDeltaProposal.model_validate(
        {
            "status": delta.triage.value,
            "summary": delta.summary,
            "reason": delta.triage_reason or "",
            "changes": [
                {
                    "category": change.category,
                    "scope": change.scope,
                    "statement": change.statement,
                    "before": change.before or "",
                    "after": change.after or "",
                    "evidence_before": change.evidence_before or "",
                    "evidence_after": change.evidence_after or "",
                }
                for change in delta.changes
            ],
        }
    )


def _listed(values: Iterable[str]) -> dict[str, Any]:
    items = list(values)
    return {
        "count": len(items),
        "ids": items[:MAX_LISTED_ITEMS],
        "truncated": len(items) > MAX_LISTED_ITEMS,
    }


def _canonical_metadata_problems(delta: Delta) -> list[str]:
    problems: list[str] = []
    if delta.schema_version is not DeltaSchemaVersion.V2:
        problems.append("schema_version")
    if delta.diff_kind is not DiffKind.SEMANTIC:
        problems.append("diff_kind")
    if delta.generated_by != CANONICAL_GENERATED_BY:
        problems.append("generated_by")
    if delta.prompt_version != CANONICAL_PROMPT_VERSION:
        problems.append("prompt_version")
    if delta.triage_by != delta.generated_by:
        problems.append("triage_by")
    if not (delta.triage_reason or "").strip():
        problems.append("triage_reason")
    if delta.triage not in {Triage.MEANINGFUL, Triage.NOISE}:
        problems.append("triage")
    return problems


def _duplicates(values: Iterable[str]) -> list[str]:
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _load_observations(
    documents: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Observation], list[Finding]]:
    index: dict[str, Observation] = {}
    findings: list[Finding] = []
    for document in documents:
        data = dict(document)
        try:
            observation = Observation.model_validate(data)
        except ValidationError as exc:
            findings.append(
                Finding(
                    "observation_document_not_loadable",
                    "observation",
                    str(data.get("obs_id") or "<unknown>"),
                    _validation_detail(exc),
                )
            )
            continue
        index[observation.obs_id] = observation
    return index, findings


def _load_claims(
    documents: Iterable[Mapping[str, Any]],
) -> tuple[list[Claim], list[Finding]]:
    claims: list[Claim] = []
    findings: list[Finding] = []
    for document in documents:
        data = dict(document)
        try:
            claims.append(Claim.model_validate(data))
        except ValidationError as exc:
            findings.append(
                Finding(
                    "claim_document_not_loadable",
                    "claim",
                    str(data.get("claim_id") or "<unknown>"),
                    _validation_detail(exc),
                )
            )
    return claims, findings


def _audit_claims(
    claims: list[Claim],
    deltas_by_id: Mapping[str, Delta],
) -> tuple[list[Finding], dict[str, Any]]:
    """Check that active claims rest on meaningful candidate evidence."""
    findings: list[Finding] = []
    by_id = {claim.claim_id: claim for claim in claims}
    active = [claim for claim in claims if claim.status is ClaimStatus.ACTIVE]
    evidence_ok: list[str] = []
    lifecycle_ok: list[str] = []

    for claim in active:
        claim_evidence_ok = True
        claim_lifecycle_ok = True
        resolved_sources: set[str] = set()

        for evidence in claim.evidence:
            delta = deltas_by_id.get(evidence.delta_id)
            if delta is None:
                findings.append(
                    Finding(
                        "claim_evidence_unresolved",
                        "claim",
                        claim.claim_id,
                        "evidence delta is absent from the audited candidate table",
                        {"delta_id": evidence.delta_id},
                    )
                )
                claim_evidence_ok = False
                continue
            if delta.triage is not Triage.MEANINGFUL:
                findings.append(
                    Finding(
                        "claim_evidence_not_meaningful",
                        "claim",
                        claim.claim_id,
                        f"evidence delta triage is {delta.triage.value}",
                        {"delta_id": delta.delta_id},
                    )
                )
                claim_evidence_ok = False
            if claim.entity != MARKET_ENTITY and delta.entity != claim.entity:
                findings.append(
                    Finding(
                        "claim_entity_mismatch",
                        "claim",
                        claim.claim_id,
                        "claim entity differs from its evidence Delta entity",
                        {"delta_id": delta.delta_id},
                    )
                )
                claim_evidence_ok = False
            if evidence.source != delta.source:
                findings.append(
                    Finding(
                        "claim_source_mismatch",
                        "claim",
                        claim.claim_id,
                        "recorded evidence source differs from the Delta source",
                        {"delta_id": delta.delta_id},
                    )
                )
                claim_evidence_ok = False
            resolved_sources.add(delta.source)

        if (
            claim.class_ is ClaimClass.INFERENCE
            and claim.disputes is None
            and len(resolved_sources) < 2
        ):
            findings.append(
                Finding(
                    "claim_inference_source_diversity_unmet",
                    "claim",
                    claim.claim_id,
                    "non-dispute inference resolves to fewer than two distinct Delta sources",
                    {"resolved_source_count": len(resolved_sources)},
                )
            )
            claim_evidence_ok = False

        if claim.superseded_by is not None:
            findings.append(
                Finding(
                    "active_claim_has_superseded_by",
                    "claim",
                    claim.claim_id,
                    "an active claim must not carry superseded_by",
                    {"superseded_by": claim.superseded_by},
                )
            )
            claim_lifecycle_ok = False
        if claim.supersedes is not None:
            target = by_id.get(claim.supersedes)
            if target is None:
                findings.append(
                    Finding(
                        "claim_supersession_target_missing",
                        "claim",
                        claim.claim_id,
                        "supersedes points at a claim that is not in the store",
                        {"supersedes": claim.supersedes},
                    )
                )
                claim_lifecycle_ok = False
            elif (
                target.status is not ClaimStatus.SUPERSEDED
                or target.superseded_by != claim.claim_id
            ):
                findings.append(
                    Finding(
                        "claim_supersession_link_broken",
                        "claim",
                        claim.claim_id,
                        "supersession chain is not doubly linked",
                        {"supersedes": claim.supersedes},
                    )
                )
                claim_lifecycle_ok = False
        if claim.disputes is not None:
            target = by_id.get(claim.disputes)
            if target is None:
                findings.append(
                    Finding(
                        "claim_dispute_target_missing",
                        "claim",
                        claim.claim_id,
                        "disputes points at a claim that is not in the store",
                        {"disputes": claim.disputes},
                    )
                )
                claim_lifecycle_ok = False
            elif target.status is not ClaimStatus.ACTIVE:
                findings.append(
                    Finding(
                        "claim_dispute_target_not_active",
                        "claim",
                        claim.claim_id,
                        "a dispute must leave its target claim active",
                        {"disputes": claim.disputes},
                    )
                )
                claim_lifecycle_ok = False

        if claim_evidence_ok:
            evidence_ok.append(claim.claim_id)
        if claim_lifecycle_ok:
            lifecycle_ok.append(claim.claim_id)

    summary = {
        "documents": len(claims),
        "by_status": dict(sorted(Counter(claim.status.value for claim in claims).items())),
        "active": len(active),
        "active_by_class": dict(
            sorted(Counter(claim.class_.value for claim in active).items())
        ),
        "active_by_confidence": dict(
            sorted(Counter(claim.confidence.value for claim in active).items())
        ),
        "active_evidence_entries": sum(len(claim.evidence) for claim in active),
        "active_resolving_to_meaningful_candidate": _listed(sorted(evidence_ok)),
        "active_with_valid_lifecycle_links": _listed(sorted(lifecycle_ok)),
    }
    return findings, summary


def audit_candidate(
    reader: CandidateAuditReader,
    *,
    project: str | None = None,
    dataset: str | None = None,
    table: str | None = None,
) -> dict[str, Any]:
    """Run the complete read-only candidate audit and return a bounded report."""
    findings: list[Finding] = []
    advisories: list[Finding] = []

    observations, observation_findings = _load_observations(reader.observation_documents())
    findings.extend(observation_findings)

    rows = list(reader.candidate_rows())
    deltas: list[Delta] = []
    for index, raw_row in enumerate(rows):
        row = dict(raw_row)
        subject = str(row.get("delta_id") or f"row[{index}]")
        try:
            deltas.append(decode_delta_row(row))
        except ValidationError as exc:
            findings.append(
                Finding("delta_row_not_loadable", "delta", subject, _validation_detail(exc))
            )
        except (TypeError, ValueError) as exc:
            findings.append(
                Finding(
                    "delta_row_not_loadable", "delta", subject, _bounded(type(exc).__name__)
                )
            )

    for duplicate in _duplicates(delta.delta_id for delta in deltas):
        findings.append(
            Finding("duplicate_delta_id", "delta", duplicate, "delta_id appears more than once")
        )
    for duplicate in _duplicates(
        delta.comparison_id for delta in deltas if delta.comparison_id
    ):
        findings.append(
            Finding(
                "duplicate_comparison_id",
                "comparison",
                duplicate,
                "comparison_id appears more than once",
            )
        )
    for duplicate in _duplicates(
        f"{delta.obs_before}|{delta.obs_after}" for delta in deltas
    ):
        findings.append(
            Finding(
                "duplicate_observation_pair",
                "observation_pair",
                duplicate,
                "one observation pair has more than one candidate Delta",
            )
        )

    payload_cache: dict[str, tuple[bytes | None, str | None]] = {}

    def load_payload(content_ref: str) -> tuple[bytes | None, str | None]:
        if content_ref not in payload_cache:
            try:
                payload_cache[content_ref] = (reader.raw_payload(content_ref), None)
            except Exception as exc:  # storage/transport errors become failure classes
                payload_cache[content_ref] = (None, _bounded(type(exc).__name__))
        return payload_cache[content_ref]

    meaningful_changes = 0
    revalidated = 0
    pairs_hash_verified = 0
    referenced_observations: set[str] = set()
    normalized_identical_noise: list[str] = []
    normalized_changed_noise: list[str] = []
    evidence_index: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"sources": set(), "delta_ids": set(), "occurrences": 0}
    )
    entity_source_counts: Counter[tuple[str, str]] = Counter()
    entity_source_meaningful: Counter[tuple[str, str]] = Counter()

    for delta in deltas:
        entity_source_counts[(delta.entity, delta.source)] += 1
        if delta.triage is Triage.MEANINGFUL:
            entity_source_meaningful[(delta.entity, delta.source)] += 1
        referenced_observations.update({delta.obs_before, delta.obs_after})

        problems = _canonical_metadata_problems(delta)
        if problems:
            findings.append(
                Finding(
                    "noncanonical_metadata",
                    "delta",
                    delta.delta_id,
                    "non-canonical fields: " + ", ".join(problems),
                )
            )
        expected_comparison = comparison_id_for(
            delta.obs_before,
            delta.obs_after,
            generated_by=delta.generated_by or CANONICAL_GENERATED_BY,
            prompt_version=delta.prompt_version or CANONICAL_PROMPT_VERSION,
        )
        if delta.comparison_id != expected_comparison:
            findings.append(
                Finding(
                    "comparison_id_mismatch",
                    "delta",
                    delta.delta_id,
                    "comparison_id does not restate the observation pair and model identity",
                )
            )
        if delta.obs_before == delta.obs_after:
            findings.append(
                Finding(
                    "self_comparison_pair",
                    "delta",
                    delta.delta_id,
                    "obs_before and obs_after are the same observation",
                )
            )

        before = observations.get(delta.obs_before)
        after = observations.get(delta.obs_after)
        absent = [
            obs_id
            for obs_id, observation in (
                (delta.obs_before, before),
                (delta.obs_after, after),
            )
            if observation is None
        ]
        if absent or before is None or after is None:
            findings.append(
                Finding(
                    "observation_missing",
                    "delta",
                    delta.delta_id,
                    "referenced observation is absent from the observation log",
                    {"observation_ids": absent},
                )
            )
            continue

        mismatched = [
            f"{role}.{name}"
            for role, observation in (("before", before), ("after", after))
            for name, value in (("entity", observation.entity), ("source", observation.source))
            if value != getattr(delta, name)
        ]
        if mismatched:
            findings.append(
                Finding(
                    "observation_identity_mismatch",
                    "delta",
                    delta.delta_id,
                    "observation identity differs: " + ", ".join(mismatched),
                )
            )
        unclean = [
            observation.obs_id
            for observation in (before, after)
            if observation.status is not ObservationStatus.OK
        ]
        if unclean:
            findings.append(
                Finding(
                    "observation_not_clean",
                    "delta",
                    delta.delta_id,
                    "a compared observation is not status=ok",
                    {"observation_ids": unclean},
                )
            )
        if before.fetched_at >= after.fetched_at:
            findings.append(
                Finding(
                    "chronology_out_of_order",
                    "delta",
                    delta.delta_id,
                    "obs_before was not fetched before obs_after",
                )
            )
        if delta.computed_at < after.fetched_at:
            findings.append(
                Finding(
                    "computed_at_precedes_observation",
                    "delta",
                    delta.delta_id,
                    "computed_at precedes the after observation it compares",
                )
            )

        payloads: dict[str, bytes] = {}
        payload_failed = False
        for role, observation in (("before", before), ("after", after)):
            payload, error = load_payload(observation.content_ref)
            if payload is None:
                findings.append(
                    Finding(
                        "raw_payload_unavailable",
                        "delta",
                        delta.delta_id,
                        error or "raw payload could not be read",
                        {"obs_id": observation.obs_id},
                    )
                )
                payload_failed = True
                continue
            if payload_hash(payload) != observation.content_hash:
                findings.append(
                    Finding(
                        "raw_payload_hash_mismatch",
                        "delta",
                        delta.delta_id,
                        "stored raw payload does not match the observation content hash",
                        {"obs_id": observation.obs_id},
                    )
                )
                payload_failed = True
                continue
            payloads[role] = payload
        if payload_failed:
            continue
        pairs_hash_verified += 1

        try:
            bundle = build_comparison_bundle(
                delta.entity,
                delta.source,
                payloads["before"],
                payloads["after"],
                obs_before=delta.obs_before,
                obs_after=delta.obs_after,
            )
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            findings.append(
                Finding(
                    "normalization_failed",
                    "delta",
                    delta.delta_id,
                    _bounded(type(exc).__name__),
                )
            )
            continue

        try:
            proposal = proposal_from_delta(delta)
        except ValidationError as exc:
            findings.append(
                Finding(
                    "proposal_policy_failed", "delta", delta.delta_id, _validation_detail(exc)
                )
            )
            continue
        except SemanticDifferError as exc:
            # These messages are code-authored constants, never model text.
            findings.append(
                Finding("proposal_policy_failed", "delta", delta.delta_id, _bounded(exc))
            )
            continue

        try:
            grounding = validate_grounding(
                proposal,
                before=bundle.before,
                after=bundle.after,
                obs_before=delta.obs_before,
                obs_after=delta.obs_after,
            )
        except SemanticDifferError as exc:
            findings.append(
                Finding("grounding_failed", "delta", delta.delta_id, _bounded(exc))
            )
            continue
        if set(grounding.routed_to) != {str(scope) for scope in delta.routed_to}:
            findings.append(
                Finding(
                    "routed_scope_mismatch",
                    "delta",
                    delta.delta_id,
                    "routed_to is not the union of accepted change scopes",
                )
            )
        revalidated += 1

        if delta.triage is Triage.NOISE:
            # A changed raw hash does not imply a changed normalized pair:
            # volatile source metadata alone produces an identical bundle.
            if bundle.before == bundle.after:
                normalized_identical_noise.append(delta.delta_id)
            else:
                normalized_changed_noise.append(delta.delta_id)
            continue

        meaningful_changes += len(delta.changes)
        for index, change in enumerate(delta.changes):
            if change.evidence_after is None:
                continue
            quote = change.evidence_after.quote
            fingerprint = quote_fingerprint(quote)
            if quote_is_grounded(quote, bundle.before):
                advisories.append(
                    Finding(
                        "evidence_after_present_in_obs_before",
                        "delta",
                        delta.delta_id,
                        "evidence_after quote also occurs in the before snapshot",
                        {
                            "change_index": index,
                            "quote_fingerprint": fingerprint,
                            "obs_before": delta.obs_before,
                        },
                    )
                )
            bucket = evidence_index[(delta.entity, fingerprint)]
            bucket["sources"].add(delta.source)
            bucket["delta_ids"].add(delta.delta_id)
            bucket["occurrences"] += 1

    for (entity, fingerprint), bucket in sorted(
        evidence_index.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        if len(bucket["sources"]) > 1:
            advisories.append(
                Finding(
                    "cross_source_mirrored_evidence",
                    "entity",
                    entity,
                    "one entity mirrors the same evidence text across its source family",
                    {
                        "quote_fingerprint": fingerprint,
                        "sources": sorted(bucket["sources"]),
                        "delta_ids": sorted(bucket["delta_ids"]),
                        "occurrences": bucket["occurrences"],
                    },
                )
            )

    deltas_by_id = {delta.delta_id: delta for delta in deltas}
    claims, claim_load_findings = _load_claims(reader.claim_documents())
    findings.extend(claim_load_findings)
    claim_findings, claim_summary = _audit_claims(claims, deltas_by_id)
    findings.extend(claim_findings)

    unexpected = sorted(
        {finding.failure_class for finding in findings} - HARD_FAILURE_CLASSES
    )
    if unexpected:
        raise CandidateAuditError(
            f"audit produced unclassified failure classes: {unexpected}"
        )

    triage_counts = Counter(delta.triage.value for delta in deltas)
    report: dict[str, Any] = {
        "audit_version": AUDIT_VERSION,
        "captured_at": datetime.now(UTC).isoformat(),
        "project": project,
        "dataset": dataset,
        "table": table,
        "read_only": True,
        "writes_performed": 0,
        "model_provider_calls": 0,
        "counts": {
            "candidate_rows": len(rows),
            "loaded_deltas": len(deltas),
            "meaningful": triage_counts.get(Triage.MEANINGFUL.value, 0),
            "noise": triage_counts.get(Triage.NOISE.value, 0),
            "meaningful_changes": meaningful_changes,
            "distinct_delta_ids": len({delta.delta_id for delta in deltas}),
            "distinct_comparison_ids": len(
                {delta.comparison_id for delta in deltas if delta.comparison_id}
            ),
            "distinct_observation_pairs": len(
                {(delta.obs_before, delta.obs_after) for delta in deltas}
            ),
            "observations_referenced": len(referenced_observations),
            "observations_loaded": len(observations),
            "raw_payloads_read": len(payload_cache),
            "observation_pairs_hash_verified": pairs_hash_verified,
            "revalidated_deltas": revalidated,
            "hard_failures": len(findings),
            "advisories": len(advisories),
        },
        "by_entity_source": [
            {
                "entity": entity,
                "source": source,
                "rows": count,
                "meaningful": entity_source_meaningful[(entity, source)],
                "noise": count - entity_source_meaningful[(entity, source)],
            }
            for (entity, source), count in sorted(entity_source_counts.items())
        ],
        "noise_pairs": {
            "normalized_identical": _listed(sorted(normalized_identical_noise)),
            "normalized_changed": _listed(sorted(normalized_changed_noise)),
        },
        "claims": claim_summary,
        "advisories": {
            "total": len(advisories),
            "by_class": dict(
                sorted(Counter(item.failure_class for item in advisories).items())
            ),
            "items": [item.as_dict() for item in advisories[:MAX_LISTED_ITEMS]],
            "truncated": len(advisories) > MAX_LISTED_ITEMS,
        },
        "hard_failures": {
            "total": len(findings),
            "by_class": dict(
                sorted(Counter(item.failure_class for item in findings).items())
            ),
            "items": [item.as_dict() for item in findings[:MAX_LISTED_ITEMS]],
            "truncated": len(findings) > MAX_LISTED_ITEMS,
        },
        "ok": not findings,
    }
    return report


class CloudCandidateReader:
    """Read-only BigQuery/GCS/Firestore access for the audit.

    The class deliberately exposes no insert, update, delete, rename, publish,
    or pause operation.
    """

    def __init__(
        self,
        *,
        project: str,
        dataset: str,
        table: str,
        claims_collection: str = "claims",
    ) -> None:
        from google.cloud import bigquery, firestore, storage

        self.project = project
        self.dataset = dataset
        self.table = table
        self.claims_collection = claims_collection
        self._bigquery = bigquery.Client(project=project)
        self._storage = storage.Client(project=project)
        self._firestore = firestore.Client(project=project)

    @property
    def candidate_table(self) -> str:
        return f"{self.project}.{self.dataset}.{self.table}"

    @property
    def observations_table(self) -> str:
        return f"{self.project}.{self.dataset}.observations"

    def _rows(self, table: str, columns: tuple[str, ...], order_by: str) -> list[dict[str, Any]]:
        from google.api_core.exceptions import GoogleAPIError

        query = (
            f"SELECT {', '.join(columns)} FROM `{table}` ORDER BY {order_by}"  # noqa: S608
        )
        try:
            return [dict(row) for row in self._bigquery.query(query).result()]
        except GoogleAPIError as exc:
            raise CandidateAuditError(f"cannot read `{table}`: {_bounded(exc)}") from exc

    def candidate_rows(self) -> list[dict[str, Any]]:
        return self._rows(
            self.candidate_table, CANDIDATE_COLUMNS, "computed_at, delta_id"
        )

    def observation_documents(self) -> list[dict[str, Any]]:
        return self._rows(self.observations_table, OBSERVATION_COLUMNS, "fetched_at, obs_id")

    def raw_payload(self, content_ref: str) -> bytes:
        if not content_ref.startswith("gs://"):
            raise CandidateAuditError("audit requires immutable gs:// snapshots")
        bucket, _, object_name = content_ref[5:].partition("/")
        if not bucket or not object_name:
            raise CandidateAuditError("invalid immutable GCS content reference")
        return self._storage.bucket(bucket).blob(object_name).download_as_bytes()

    def claim_documents(self) -> list[dict[str, Any]]:
        return [
            snapshot.to_dict()
            for snapshot in self._firestore.collection(self.claims_collection).stream()
        ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument(
        "--table",
        default=DEFAULT_TABLE,
        help="Delta table to audit; defaults to the strict v2 candidate table.",
    )
    parser.add_argument("--claims-collection", default="claims")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional local path for the bounded JSON report.",
    )
    args = parser.parse_args(argv)

    try:
        reader = CloudCandidateReader(
            project=args.project,
            dataset=args.dataset,
            table=args.table,
            claims_collection=args.claims_collection,
        )
        report = audit_candidate(
            reader,
            project=args.project,
            dataset=args.dataset,
            table=reader.candidate_table,
        )
    except CandidateAuditError as exc:
        print(json.dumps({"audit_version": AUDIT_VERSION, "error": _bounded(exc)}))
        return 2

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(text + "\n")
        temporary.replace(args.output)
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
