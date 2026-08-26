import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from adapters.github import GithubFetch
from pipeline.acquire import acquire_github_releases
from pipeline.claims import record_delivery_once
from pipeline.local_backend import LocalBackend, LocalSettings
from pipeline.local_tracer import StaticAdapter
from schemas.config import load_config
from schemas.delta import Delta
from schemas.receipt import DeliveryReceipt
from tests.semantic_test_helpers import FakeSemanticDiffer

FIXTURES = Path("schemas/fixtures")


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def release(tag: str, published_at: str) -> dict:
    return {
        "tag_name": tag,
        "name": tag,
        "body": "Real release notes",
        "draft": False,
        "prerelease": False,
        "published_at": published_at,
        "target_commitish": "main",
        "html_url": f"https://github.com/earendil-works/pi/releases/tag/{tag}",
    }


def adapter_for(releases: list[dict]) -> StaticAdapter:
    return StaticAdapter(
        GithubFetch(
            repository="earendil-works/pi",
            payload=canonical(releases),
            releases=releases,
        )
    )


def test_local_fleet_persists_hash_gate_claims_and_verification(tmp_path):
    config = load_config("tycho.yaml")
    entity = config.entities["pi"]
    settings = LocalSettings(tmp_path / "tycho-data")
    first_at = datetime(2026, 8, 20, 2, tzinfo=UTC)
    initial = [release("v0.84.1", "2026-08-13T10:00:00Z")]
    differ = FakeSemanticDiffer()

    with LocalBackend(config, settings) as backend:
        first = acquire_github_releases(
            "pi", entity, backend, adapter_for(initial), now=first_at, differ=differ
        )
        assert first.outcome == "bootstrapped"
        raw_ref = backend.observations()[0].content_ref
        assert Path(raw_ref.removeprefix("file://")).exists()

    with LocalBackend(config, settings) as backend:
        unchanged = acquire_github_releases(
            "pi",
            entity,
            backend,
            adapter_for(initial),
            now=first_at + timedelta(hours=1),
            differ=differ,
        )
        assert unchanged.outcome == "unchanged"
        assert backend.stats() == {
            "observations": 2,
            "deltas": 0,
            "claims": 0,
            "receipts": 0,
            "analyst_runs": 0,
            "alerts": 0,
            "outbox_pending": 0,
        }

        changed_releases = [
            release("v0.84.2", "2026-08-14T10:14:32Z"),
            *initial,
        ]
        changed = acquire_github_releases(
            "pi",
            entity,
            backend,
            adapter_for(changed_releases),
            now=first_at + timedelta(days=1),
            differ=differ,
        )
        assert changed.outcome == "meaningful"
        assert len(backend.deltas()) == 1
        assert len(backend.claims()) == 1
        assert backend.pending_count() == 0
        claim = backend.claims()[0]
        assert "durable user-facing capability" in claim.statement

        verified_at = first_at + timedelta(days=1, hours=1)
        repeated = acquire_github_releases(
            "pi",
            entity,
            backend,
            adapter_for(changed_releases),
            now=verified_at,
            differ=differ,
        )
        assert repeated.outcome == "unchanged"
        assert backend.get_claim(claim.claim_id).last_verified_at == verified_at

    with LocalBackend(config, settings) as backend:
        assert backend.stats()["observations"] == 4
        assert backend.stats()["claims"] == 1
        assert backend.get_claim(claim.claim_id).last_verified_at == verified_at


def test_pending_delta_is_replayed_idempotently_after_restart(tmp_path):
    config = load_config("tycho.yaml")
    settings = LocalSettings(tmp_path / "tycho-data")
    delta = Delta.model_validate_json(
        (FIXTURES / "delta.example.json").read_text()
    )

    with LocalBackend(config, settings) as backend:
        backend.insert_delta(delta)
        assert backend.pending_count() == 1

    with LocalBackend(config, settings) as backend:
        assert backend.process_pending() == [
            {"delta_id": delta.delta_id, "outcome": "published"}
        ]
        assert backend.pending_count() == 0
        assert backend.get_claim(delta.delta_id.replace("dlt_", "clm_", 1)) is not None

        # Replaying the same deterministic delivery cannot duplicate the claim.
        backend.publish_delta(delta)
        assert len(backend.claims()) == 1


def test_local_receipt_dedup_survives_restart(tmp_path):
    config = load_config("tycho.yaml")
    settings = LocalSettings(tmp_path / "tycho-data")
    receipt = DeliveryReceipt.model_validate_json(
        (FIXTURES / "receipt.example.json").read_text()
    )

    with LocalBackend(config, settings) as backend:
        assert record_delivery_once(backend, receipt) is True

    with LocalBackend(config, settings) as backend:
        assert record_delivery_once(backend, receipt) is False
        assert backend.receipts() == [receipt]
