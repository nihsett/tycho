"""Run one synthetic strategy-council session against a disposable local store.

This is the local end-to-end check for the strategy fleet.  It calls no model,
touches no Google Cloud resource, and refuses to run against the real local
fleet database: the whole point is that a session can be exercised end to end
without contaminating anything that matters.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from pipeline.local_backend import LocalBackend, LocalSettings
from pipeline.strategy_context import default_period
from schemas.config import load_config
from schemas.strategy import CardStatus
from strategy_agent.council import run_strategy_session
from strategy_agent.events import assert_event_is_safe
from strategy_agent.synthetic import (
    SYNTHETIC_NOW,
    build_synthetic_market,
    scripted_session,
    seed_market,
)

PROTECTED_DATABASES = frozenset({Path("data/tycho.sqlite3").resolve()})
DEFAULT_DATA_DIR = "data/strategy-local"
DEFAULT_OUTPUT = "data/strategy_local_session.json"


def _guard_disposable(settings: LocalSettings) -> None:
    """Never let a synthetic run write into the real local fleet store."""
    if settings.database.resolve() in PROTECTED_DATABASES:
        raise SystemExit(
            f"refusing to seed synthetic fixtures into {settings.database}; "
            "use a disposable --data-dir"
        )


def _card_summary(card) -> dict[str, object]:
    """Bounded card view: IDs, status, and counts. No statements or rationale."""
    return {
        "card_id": card.card_id,
        "status": card.status.value,
        "confidence": card.confidence.value,
        "entities": list(card.entities),
        "scopes": list(card.scopes),
        "source_families": list(card.source_families),
        "premises": [
            f"{premise.claim_id}@{premise.claim_version}" for premise in card.premises
        ],
        "rejection_reasons": list(card.rejection_reasons),
    }


async def _run(args: argparse.Namespace) -> dict[str, object]:
    config = load_config(args.config)
    settings = LocalSettings(root=Path(args.data_dir).resolve())
    _guard_disposable(settings)
    if args.reset and settings.root.exists():
        shutil.rmtree(settings.root)

    now = (
        SYNTHETIC_NOW
        if args.now == "synthetic"
        else datetime.fromisoformat(args.now).astimezone(UTC)
    )
    market = build_synthetic_market(now)
    period = default_period(now, args.period_days)

    with LocalBackend(config, settings) as store:
        seed_market(store, market)
        invoker = scripted_session(market)
        result = await run_strategy_session(
            store, config, invoker, period=period, now=now
        )
        for event in result.events:
            assert_event_is_safe(event.as_dict())

        session = result.session
        passed = [card for card in session.cards if card.status is CardStatus.PASSED]
        rejected = [card for card in session.cards if card.status is CardStatus.REJECTED]
        return {
            "run_at": datetime.now(UTC).isoformat(),
            "database": str(settings.database),
            "synthetic": True,
            "model_calls": {"provider": "none (scripted offline invoker)", "agents": invoker.calls},
            "session": {
                "session_id": session.session_id,
                "strategy_version": session.strategy_version,
                "state": session.state.value,
                "period": {
                    "from": session.period.from_.isoformat(),
                    "to": session.period.to.isoformat(),
                },
                "manifest_hash": session.manifest_hash,
                "manifest": [
                    f"{entry.claim_id}@{entry.claim_version}"
                    for entry in session.input_manifest
                ],
                "metrics": session.metrics.model_dump(mode="json"),
                "metrics_evidence": [
                    metric.model_dump(mode="json") for metric in session.metrics_evidence
                ],
                "brief_id": session.brief_id,
            },
            "cards": [_card_summary(card) for card in session.cards],
            "counts": {"passed": len(passed), "rejected": len(rejected)},
            "brief": {
                "brief_id": result.brief.brief_id if result.brief else None,
                "claims_referenced": [
                    f"{item.claim_id}@{item.version}"
                    for item in (result.brief.claims_referenced if result.brief else [])
                ],
                "strategy_card_ids": list(result.brief.strategy_card_ids)
                if result.brief
                else [],
                "stats": result.brief.stats.model_dump(mode="json") if result.brief else None,
                "rendered_bytes": len(result.brief.rendered_md.encode()) if result.brief else 0,
            },
            "events": [event.as_dict() for event in result.events],
            "stats": store.stats(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="tycho.yaml")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--period-days", type=int, default=7)
    parser.add_argument(
        "--now",
        default="synthetic",
        help="'synthetic' for the fixed fixture clock, or an ISO-8601 timestamp",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        default=True,
        help="delete the disposable store before seeding (default)",
    )
    parser.add_argument("--no-reset", dest="reset", action="store_false")
    parser.add_argument(
        "--print-brief",
        action="store_true",
        help="also print the rendered brief markdown",
    )
    args = parser.parse_args()

    summary = asyncio.run(_run(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))

    if args.print_brief:
        config = load_config(args.config)
        settings = LocalSettings(root=Path(args.data_dir).resolve())
        with LocalBackend(config, settings) as store:
            brief = store.get_brief(str(summary["brief"]["brief_id"]))
            if brief is not None:
                print("\n" + brief.rendered_md)


if __name__ == "__main__":
    main()
