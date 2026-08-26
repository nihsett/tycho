"""Run the Gemini analyst in shadow mode against a fixture or stored real delta."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from pipeline.gemini_analyst import run_shadow_sync
from pipeline.local_backend import LocalBackend, LocalSettings
from schemas.config import load_config
from schemas.delta import Delta


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--delta-id")
    source.add_argument(
        "--fixture", default="schemas/fixtures/delta.example.json"
    )
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--config", default="tycho.yaml")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)

    if args.delta_id:
        settings = LocalSettings(Path(args.data_dir).resolve())
        with LocalBackend(config, settings) as store:
            delta = store.get_delta(args.delta_id)
            if delta is None:
                raise SystemExit(f"unknown local delta: {args.delta_id}")
            result = run_shadow_sync(delta, config, store, force=args.force)
    else:
        delta = Delta.model_validate_json(Path(args.fixture).read_text())
        with tempfile.TemporaryDirectory(prefix="tycho-calibration-") as directory:
            settings = LocalSettings(Path(directory))
            with LocalBackend(config, settings) as store:
                store.insert_delta(delta)
                result = run_shadow_sync(delta, config, store, force=True)

    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "delta_id": result.delta_id,
                "mode": result.mode,
                "model": result.model,
                "actions": result.actions,
                "final_text": result.final_text,
                "skipped": result.skipped,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
