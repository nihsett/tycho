"""Hard enforcement for claim lifecycle rules; prompts cannot bypass this layer."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol

from google.cloud import firestore

from schemas.claim import Claim, ClaimClass, ClaimStatus, Confidence, Evidence, Severity
from schemas.receipt import DeliveryReceipt


class ClaimRuleViolation(ValueError):
    """A proposed lifecycle operation violates the Tycho contract."""


class DemotionBlocked(ClaimRuleViolation):
    """Unsafe evidence cannot demote a confirmed critical claim."""


class ClaimStore(Protocol):
    def create_claim(self, claim: Claim) -> None: ...

    def update_claim(self, claim_id: str, fields: dict[str, Any]) -> None: ...

    def create_receipt_once(self, dedup_key: str, receipt: DeliveryReceipt) -> bool: ...


class FirestoreClaimStore:
    """Minimal Firestore persistence used by lifecycle functions and the tracer."""

    def __init__(self, client: firestore.Client | None = None) -> None:
        self.client = client or firestore.Client()

    def create_claim(self, claim: Claim) -> None:
        self.client.collection("claims").document(claim.claim_id).create(
            claim.model_dump(mode="json", by_alias=True)
        )

    def update_claim(self, claim_id: str, fields: dict[str, Any]) -> None:
        self.client.collection("claims").document(claim_id).update(fields)

    def create_receipt_once(self, dedup_key: str, receipt: DeliveryReceipt) -> bool:
        dedup_ref = self.client.collection("delivery_dedup").document(dedup_key)
        receipt_ref = self.client.collection("receipts").document(receipt.receipt_id)
        transaction = self.client.transaction()

        @firestore.transactional
        def commit(txn: firestore.Transaction) -> bool:
            if dedup_ref.get(transaction=txn).exists:
                return False
            txn.create(receipt_ref, receipt.model_dump(mode="json"))
            txn.create(
                dedup_ref,
                {
                    "claim_id": receipt.claim_id,
                    "claim_version": receipt.claim_version,
                    "context_key": receipt.context_key,
                    "receipt_id": receipt.receipt_id,
                },
            )
            return True

        return commit(transaction)


def validate_evidence_context(claim: Claim, primary_sources: set[str]) -> None:
    """Enforce evidence bars that require entity configuration context."""
    if claim.class_ is ClaimClass.FACT and not any(
        item.source in primary_sources for item in claim.evidence
    ):
        raise ClaimRuleViolation("fact evidence must include the entity's own primary source")


def publish_before_retire(store: ClaimStore, old: Claim, replacement: Claim) -> None:
    """Apply: "Supersession is publish-before-retire ... silent loss impossible."""
    if old.status is not ClaimStatus.ACTIVE:
        raise ClaimRuleViolation("only an active claim can be superseded")
    if replacement.status is not ClaimStatus.ACTIVE:
        raise ClaimRuleViolation("replacement must be active when published")
    if replacement.entity != old.entity or replacement.scope != old.scope:
        raise ClaimRuleViolation("replacement must preserve entity and scope")
    if replacement.supersedes != old.claim_id:
        raise ClaimRuleViolation("replacement.supersedes must point to the old claim")
    if replacement.version != old.version + 1:
        raise ClaimRuleViolation("replacement version must increment by one")

    # Deliberately separate writes in this order. A crash may leave two active
    # claims, which is visible and repairable; it can never leave no claim.
    store.create_claim(replacement)
    store.update_claim(
        old.claim_id,
        {
            "status": ClaimStatus.SUPERSEDED.value,
            "superseded_by": replacement.claim_id,
        },
    )


def enforce_demotion_rule(
    target: Claim,
    *,
    proposed_confidence: Confidence | None,
    proposed_status: ClaimStatus | None,
    evidence: list[Evidence],
    current_delta_ids: set[str],
    primary_sources: set[str],
) -> None:
    """Apply: "the analyst cannot retire or lower confidence of a confirmed+critical claim using evidence from the same delta batch ... unless ... primary source."""
    confidence_rank = {
        Confidence.SPECULATIVE: 0,
        Confidence.LIKELY: 1,
        Confidence.CONFIRMED: 2,
    }
    lowers_confidence = (
        proposed_confidence is not None
        and confidence_rank[proposed_confidence] < confidence_rank[target.confidence]
    )
    retires = proposed_status in {ClaimStatus.RETIRED, ClaimStatus.SUPERSEDED}
    protected = (
        target.confidence is Confidence.CONFIRMED
        and target.severity is Severity.CRITICAL
        and (lowers_confidence or retires)
    )
    if not protected:
        return

    batch_evidence = [item for item in evidence if item.delta_id in current_delta_ids]
    if batch_evidence and not any(item.source in primary_sources for item in batch_evidence):
        raise DemotionBlocked(
            "confirmed critical claim must be flagged disputed; current-batch evidence is not primary"
        )


def flag_disputed(
    store: ClaimStore,
    target: Claim,
    evidence: list[Evidence],
    reason: str,
) -> None:
    """Keep a protected claim active and record an auditable dispute in history."""
    event = {
        "event": "disputed",
        "at": datetime.now(UTC).isoformat(),
        "reason": reason,
        "evidence": [item.model_dump(mode="json") for item in evidence],
    }
    history = [*target.history, event][-20:]
    store.update_claim(target.claim_id, {"history": history})


def demote_or_flag_disputed(
    store: ClaimStore,
    target: Claim,
    *,
    proposed_confidence: Confidence | None,
    proposed_status: ClaimStatus | None,
    evidence: list[Evidence],
    current_delta_ids: set[str],
    primary_sources: set[str],
) -> bool:
    """Return False after flagging a blocked demotion; return True when allowed."""
    try:
        enforce_demotion_rule(
            target,
            proposed_confidence=proposed_confidence,
            proposed_status=proposed_status,
            evidence=evidence,
            current_delta_ids=current_delta_ids,
            primary_sources=primary_sources,
        )
    except DemotionBlocked as exc:
        flag_disputed(store, target, evidence, str(exc))
        return False
    return True


def record_delivery_once(store: ClaimStore, receipt: DeliveryReceipt) -> bool:
    """Apply: "a given (claim_id, version) delivers once per context_key."""
    identity = f"{receipt.claim_id}\0{receipt.claim_version}\0{receipt.context_key}"
    dedup_key = hashlib.sha256(identity.encode()).hexdigest()
    return store.create_receipt_once(dedup_key, receipt)
