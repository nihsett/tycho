"""Shared synthetic setup for strategy tests. Disposable stores only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pipeline.local_backend import LocalBackend, LocalSettings
from schemas.config import TychoConfig, load_config
from strategy_agent.synthetic import (
    SYNTHETIC_NOW,
    SyntheticMarket,
    build_synthetic_market,
    seed_market,
)

NOW = SYNTHETIC_NOW


def config() -> TychoConfig:
    return load_config("tycho.yaml")


def seeded_store(tmp_path, market: SyntheticMarket | None = None) -> LocalBackend:
    """A disposable LocalBackend preloaded with the synthetic market."""
    store = LocalBackend(config(), LocalSettings(tmp_path / "strategy-data"))
    seed_market(store, market or build_synthetic_market(NOW))
    return store


def draft(
    market: SyntheticMarket,
    *,
    premises: list[tuple[str, int]],
    statement: str = (
        "Both Claude Code and Codex now ship workspace execution isolation as a "
        "standard control rather than an advanced option."
    ),
    rationale: str = (
        "Two vendors converged on comparable isolation defaults within one week, "
        "which changes what a buyer treats as table stakes."
    ),
    confidence: str = "likely",
    limitations: list[dict] | None = None,
) -> dict:
    """A Strategist draft payload with overridable premises and wording."""
    del market
    return {
        "statement": statement,
        "rationale": rationale,
        "confidence": confidence,
        "competing_explanation": (
            "Both teams may be answering the same enterprise procurement "
            "checklist rather than responding to each other."
        ),
        "falsifier": (
            "A subsequent release from either vendor that returns execution "
            "isolation to an opt-in advanced setting."
        ),
        "premises": [
            {"claim_id": claim_id, "claim_version": version}
            for claim_id, version in premises
        ],
        "limitations": limitations or [],
    }


def brief_payload(claim_ids: list[str]) -> dict:
    citations = "".join(f'<claim id="{claim_id}" version="1"/>' for claim_id in claim_ids)
    return {
        "what_changed": f"Two vendors shipped comparable isolation controls. {citations}",
        "what_tycho_concludes": f"Execution isolation is now a standard control. {citations}",
        "counter_signals": "Both may be answering the same procurement checklist.",
        "what_would_change_our_mind": "A release returning isolation to opt-in.",
    }


def stale_market(days: int = 400) -> SyntheticMarket:
    """The same market with every claim verified long enough ago to be stale."""
    market = build_synthetic_market(NOW)
    old = NOW - timedelta(days=days)
    claims = [
        claim.model_copy(update={"last_verified_at": old}) for claim in market.claims
    ]
    return SyntheticMarket(deltas=market.deltas, claims=claims)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)
