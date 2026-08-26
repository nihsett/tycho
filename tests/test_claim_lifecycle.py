import json
from pathlib import Path
from typing import Any

from schemas.claim import (
    Claim,
    ClaimStatus,
    Confidence,
    Evidence,
    Severity,
    SupersessionPair,
)
from schemas.receipt import DeliveryReceipt
from pipeline.claims import (
    demote_or_flag_disputed,
    enforce_demotion_rule,
    publish_before_retire,
    record_delivery_once,
)

FIXTURES = Path("schemas/fixtures")


class MemoryStore:
    def __init__(self) -> None:
        self.operations: list[tuple[str, str]] = []
        self.claims: dict[str, Claim] = {}
        self.updates: dict[str, dict[str, Any]] = {}
        self.receipt_keys: set[str] = set()

    def create_claim(self, claim: Claim) -> None:
        self.operations.append(("create", claim.claim_id))
        self.claims[claim.claim_id] = claim

    def update_claim(self, claim_id: str, fields: dict[str, Any]) -> None:
        self.operations.append(("update", claim_id))
        self.updates[claim_id] = fields

    def create_receipt_once(self, dedup_key: str, receipt: DeliveryReceipt) -> bool:
        if dedup_key in self.receipt_keys:
            return False
        self.receipt_keys.add(dedup_key)
        return True


def load_json(filename: str) -> dict[str, Any]:
    return json.loads((FIXTURES / filename).read_text())


def test_supersession_publishes_before_retiring_old_claim():
    pair = SupersessionPair.model_validate(load_json("claim.superseded.example.json"))
    old_active = pair.old.model_copy(
        update={"status": ClaimStatus.ACTIVE, "superseded_by": None}
    )
    store = MemoryStore()

    publish_before_retire(store, old_active, pair.new)

    assert store.operations == [
        ("create", pair.new.claim_id),
        ("update", old_active.claim_id),
    ]
    assert store.updates[old_active.claim_id]["status"] == "superseded"


def test_non_primary_batch_evidence_flags_critical_claim_disputed():
    target = Claim.model_validate(load_json("claim.fact.example.json")).model_copy(
        update={"severity": Severity.CRITICAL}
    )
    evidence = [
        Evidence(
            delta_id="dlt_01ARZ3NDEKTSV4RRFFQ69G5FB9",
            source="search",
            note="Unverified third-party report.",
        )
    ]
    store = MemoryStore()

    allowed = demote_or_flag_disputed(
        store,
        target,
        proposed_confidence=Confidence.LIKELY,
        proposed_status=None,
        evidence=evidence,
        current_delta_ids={evidence[0].delta_id},
        primary_sources={"github_releases"},
    )

    assert allowed is False
    assert store.updates[target.claim_id]["history"][-1]["event"] == "disputed"


def test_primary_batch_evidence_can_demote_critical_claim():
    target = Claim.model_validate(load_json("claim.fact.example.json")).model_copy(
        update={"severity": Severity.CRITICAL}
    )
    evidence = [
        Evidence(
            delta_id="dlt_01ARZ3NDEKTSV4RRFFQ69G5FBA",
            source="github_releases",
            note="Official source correction.",
        )
    ]
    enforce_demotion_rule(
        target,
        proposed_confidence=Confidence.LIKELY,
        proposed_status=None,
        evidence=evidence,
        current_delta_ids={evidence[0].delta_id},
        primary_sources={"github_releases"},
    )


def test_delivery_is_once_per_claim_version_and_context():
    receipt = DeliveryReceipt.model_validate(load_json("receipt.example.json"))
    store = MemoryStore()

    assert record_delivery_once(store, receipt) is True
    assert record_delivery_once(store, receipt) is False
