"""The local synthetic session runner and its safety guards."""

import asyncio
import json
from argparse import Namespace
from pathlib import Path

import pytest

import pipeline.run_strategy_local as runner
from pipeline.local_backend import LocalSettings
from pipeline.strategy_context import MAX_CONTEXT_BYTES, default_period
from schemas.strategy import SessionState
from strategy_agent.council import run_strategy_session
from strategy_agent.synthetic import build_synthetic_market, scripted_session
from tests.strategy_helpers import NOW, config, seeded_store


def args_for(tmp_path: Path) -> Namespace:
    return Namespace(
        config="tycho.yaml",
        data_dir=str(tmp_path / "strategy-local"),
        output=str(tmp_path / "session.json"),
        period_days=7,
        now="synthetic",
        reset=True,
        print_brief=False,
    )


def test_local_run_reports_one_passed_and_one_rejected_card(tmp_path):
    summary = asyncio.run(runner._run(args_for(tmp_path)))

    assert summary["synthetic"] is True
    assert summary["counts"] == {"passed": 1, "rejected": 1}
    assert summary["session"]["state"] == "completed"
    assert summary["model_calls"]["agents"] == [
        "tycho_strategist",
        "tycho_challenger",
        "tycho_brief_writer",
    ]
    assert summary["brief"]["brief_id"].startswith("brf_2026w35-")
    assert len(summary["brief"]["strategy_card_ids"]) == 1
    assert summary["stats"]["strategy_sessions"] == 1
    assert summary["stats"]["briefs"] == 1


def test_local_run_summary_carries_no_conclusion_text(tmp_path):
    summary = asyncio.run(runner._run(args_for(tmp_path)))
    serialized = json.dumps(summary).lower()
    for leaked in ("sandbox", "isolation", "rationale", "rendered_md", "statement"):
        assert leaked not in serialized


def test_local_run_refuses_the_real_fleet_database():
    with pytest.raises(SystemExit, match="refusing to seed"):
        runner._guard_disposable(LocalSettings(Path("data").resolve()))


def test_local_run_is_repeatable_and_resets_its_store(tmp_path):
    first = asyncio.run(runner._run(args_for(tmp_path)))
    second = asyncio.run(runner._run(args_for(tmp_path)))
    assert first["session"]["manifest_hash"] == second["session"]["manifest_hash"]
    assert second["stats"]["strategy_sessions"] == 1


def test_input_budget_failure_happens_before_any_agent_call(tmp_path, monkeypatch):
    market = build_synthetic_market(NOW)
    invoker = scripted_session(market)
    monkeypatch.setattr("pipeline.strategy_context.MAX_CONTEXT_BYTES", 10)

    with seeded_store(tmp_path, market) as store:
        with pytest.raises(Exception, match="strategy context is"):
            asyncio.run(
                run_strategy_session(
                    store,
                    config(),
                    invoker,
                    period=default_period(NOW, 7),
                    now=NOW,
                )
            )
        # No agent was called. The placeholder session is durably failed with
        # sanitized metadata, so the attempt is visible and retryable.
        assert invoker.calls == []
        assert store.briefs() == []
        failed = store.strategy_sessions()
        assert len(failed) == 1
        assert failed[0].state is SessionState.FAILED
        assert failed[0].error == (
            "context:StrategyContextTooLarge: "
            "bounded context exceeded its byte or token budget"
        )
        assert failed[0].input_manifest == []

        # Restore the real budget: the failed lease must still be retryable.
        monkeypatch.undo()
        assert MAX_CONTEXT_BYTES == 200_000
        retry = asyncio.run(
            run_strategy_session(
                store, config(), scripted_session(market), period=default_period(NOW, 7), now=NOW
            )
        )
        assert retry.session.state is SessionState.COMPLETED
        assert retry.brief is not None
        assert len(store.strategy_sessions()) == 2
