"""Run the five worked analyst scenarios through real Gemini in disposable stores."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

from pipeline.gemini_analyst import AnalystToolbox, run_analyst
from pipeline.local_backend import LocalBackend, LocalSettings
from schemas.claim import Claim, ClaimStatus
from schemas.config import load_config
from schemas.delta import Delta


class CalibrationScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    expected_action: str
    repeat_expected_action: str | None
    delta: Delta
    prior_deltas: list[Delta]
    prior_claims: list[Claim]


class CalibrationSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenarios: list[CalibrationScenario]


class RatePacer:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.last_started: float | None = None

    def wait(self) -> None:
        if self.last_started is not None:
            remaining = self.delay_seconds - (time.monotonic() - self.last_started)
            if remaining > 0:
                time.sleep(remaining)
        self.last_started = time.monotonic()


def assert_action(
    scenario: CalibrationScenario,
    action: dict[str, Any],
    store: LocalBackend,
) -> None:
    actual = action.get("action")
    if actual != scenario.expected_action:
        raise AssertionError(f"expected {scenario.expected_action}, got {actual}")

    if scenario.name == "price_change_then_redundancy":
        claim = action["claim"]
        if (claim["class"], claim["confidence"], claim["severity"]) != (
            "fact",
            "confirmed",
            "critical",
        ):
            raise AssertionError("price fact must be fact/confirmed/critical")
        statement = claim["statement"]
        if not all(token in statement for token in ("49", "79", "2026")):
            raise AssertionError("price statement must be dated and quantify before/after")

    elif scenario.name == "price_reversal_supersession":
        old_id = scenario.prior_claims[0].claim_id
        old = store.get_claim(old_id)
        replacement = store.get_claim(action["claim"]["claim_id"])
        if old is None or old.status is not ClaimStatus.SUPERSEDED:
            raise AssertionError("old pricing claim was not superseded")
        if replacement is None or replacement.supersedes != old_id:
            raise AssertionError("replacement does not link back to old pricing claim")
        if not all(
            token in replacement.statement for token in ("79", "59", "2026")
        ):
            raise AssertionError("replacement must date and quantify the reversal")

    elif scenario.name == "cross_source_fusion":
        claim = action["claim"]
        if (
            claim["class"] != "inference"
            or claim["inference_kind"] != "intent_or_future"
            or claim["confidence"] != "speculative"
        ):
            raise AssertionError(
                "fusion must be intent_or_future inference clamped speculative"
            )
        sources = {item["source"] for item in claim["evidence"]}
        if len(sources) < 2 or scenario.delta.source not in sources:
            raise AssertionError("fusion evidence must include current and distinct sources")

    elif scenario.name == "boring_tutorial_no_action":
        if store.claims():
            raise AssertionError("no_action scenario wrote a claim")

    elif scenario.name == "third_party_dispute":
        claim = action["claim"]
        target_id = scenario.prior_claims[0].claim_id
        if (
            claim["class"] != "inference"
            or claim["confidence"] != "speculative"
            or claim["severity"] != scenario.prior_claims[0].severity.value
            or claim["disputes"] != target_id
        ):
            raise AssertionError("third-party contradiction did not create a valid dispute")
        if [item["delta_id"] for item in claim["evidence"]] != [
            scenario.delta.delta_id
        ]:
            raise AssertionError("dispute must cite only the conflicting signal")
        target = store.get_claim(target_id)
        if target is None or target.status is not ClaimStatus.ACTIVE:
            raise AssertionError("disputed established claim must remain active")
        if len(store.active_disputes(target_id)) != 1:
            raise AssertionError("active inbound dispute badge relationship is missing")
        alerts = store.alerts()
        if len(alerts) != 1 or alerts[0]["kind"] != "speculative_critical_claim":
            raise AssertionError("speculative critical dispute alert was not emitted")

    elif scenario.name == "primary_source_dispute_resolution":
        target_id = scenario.prior_claims[0].claim_id
        dispute_id = scenario.prior_claims[1].claim_id
        target = store.get_claim(target_id)
        dispute = store.get_claim(dispute_id)
        replacement = store.get_claim(action["claim"]["claim_id"])
        if target is None or target.status is not ClaimStatus.ACTIVE:
            raise AssertionError("established claim changed during dispute resolution")
        if dispute is None or dispute.status is not ClaimStatus.SUPERSEDED:
            raise AssertionError("primary source did not supersede the dispute")
        if replacement is None or replacement.supersedes != dispute_id:
            raise AssertionError("resolution fact does not link to superseded dispute")
        if replacement.class_.value != "fact" or replacement.disputes is not None:
            raise AssertionError("resolution replacement must be a non-dispute fact")
        if store.active_disputes(target_id):
            raise AssertionError("established claim's disputed badge did not clear")
        alerts = store.alerts()
        if len(alerts) != 1 or alerts[0]["kind"] != "dispute_resolved":
            raise AssertionError("dispute resolution event was not emitted")


def invalid_inference_probe(
    scenario: CalibrationScenario, store: LocalBackend
) -> dict[str, Any]:
    tools = AnalystToolbox(scenario.delta, load_config("tycho.yaml"), store, mode="shadow")
    result = tools.create_claim(
        delta_id=scenario.delta.delta_id,
        scope="product/roadmap",
        claim_class="inference",
        statement="Codex is likely rewriting its runtime in Rust.",
        rationale="This deliberately provides only one evidence source.",
        confidence="speculative",
        severity="notable",
        evidence_delta_ids=[scenario.delta.delta_id],
        evidence_notes=["Three Rust roles."],
        inference_kind="intent_or_future",
    )
    if result.get("status") != "rejected" or "distinct sources" not in result.get(
        "error", ""
    ):
        raise AssertionError("one-source inference was not rejected")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixtures", default="schemas/fixtures/analyst.scenarios.json"
    )
    parser.add_argument("--config", default="tycho.yaml")
    parser.add_argument("--model")
    parser.add_argument("--delay-seconds", type=float, default=35.0)
    parser.add_argument("--output-dir", default="data/calibration")
    parser.add_argument("--only")
    args = parser.parse_args()

    load_dotenv()
    config = load_config(args.config)
    suite = CalibrationSuite.model_validate_json(Path(args.fixtures).read_text())
    scenarios = [
        item for item in suite.scenarios if args.only is None or item.name == args.only
    ]
    if not scenarios:
        raise SystemExit(f"no scenario matched: {args.only}")

    pacer = RatePacer(args.delay_seconds)
    results: list[dict[str, Any]] = []
    failed = False
    with TemporaryDirectory(prefix="tycho-worked-examples-") as temp_root:
        for index, scenario in enumerate(scenarios):
            scenario_result: dict[str, Any] = {"name": scenario.name, "status": "failed"}
            try:
                settings = LocalSettings(Path(temp_root) / str(index))
                with LocalBackend(config, settings) as store:
                    for delta in scenario.prior_deltas:
                        store.insert_delta(delta)
                    for claim in scenario.prior_claims:
                        store.create_claim(claim)
                    store.insert_delta(scenario.delta)

                    if scenario.name == "cross_source_fusion":
                        scenario_result["invalid_inference_probe"] = invalid_inference_probe(
                            scenario, store
                        )

                    pacer.wait()
                    first = asyncio.run(
                        run_analyst(
                            scenario.delta,
                            config,
                            store,
                            mode="live",
                            model=args.model,
                            force=True,
                        )
                    )
                    if len(first.actions) != 1:
                        raise AssertionError("analyst must accept exactly one action")
                    scenario_result["first_action"] = first.actions[0]
                    assert_action(scenario, first.actions[0], store)

                    if scenario.repeat_expected_action:
                        pacer.wait()
                        repeated = asyncio.run(
                            run_analyst(
                                scenario.delta,
                                config,
                                store,
                                mode="live",
                                model=args.model,
                                force=True,
                            )
                        )
                        repeat_action = repeated.actions[0]
                        scenario_result["repeat_action"] = repeat_action
                        if repeat_action.get("action") != scenario.repeat_expected_action:
                            raise AssertionError(
                                f"repeat expected {scenario.repeat_expected_action}, "
                                f"got {repeat_action.get('action')}"
                            )

                    scenario_result["store"] = store.stats()
                    scenario_result["status"] = "passed"
            except Exception as exc:
                failed = True
                scenario_result["error"] = f"{type(exc).__name__}: {exc}"
            results.append(scenario_result)
            print(json.dumps(scenario_result, indent=2, sort_keys=True))

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = output_dir / f"worked-examples-{stamp}.json"
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "model": args.model or "default",
        "delay_seconds": args.delay_seconds,
        "results": results,
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"report: {output}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
