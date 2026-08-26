"""Run one persistent local acquisition cycle for every configured watcher."""

from __future__ import annotations

import argparse
import fcntl
import json
from datetime import UTC, datetime
from pathlib import Path

from adapters.github import GithubReleasesAdapter
from adapters.webpage import WebpageAdapter
from pipeline.acquire import acquire_github_releases, configured_differ_mode
from pipeline.acquire_webpage import acquire_website_changelog
from pipeline.local_backend import LocalBackend, LocalSettings
from pipeline.semantic_differ import SemanticDiffer, retry_incomplete_generation_pairs
from schemas.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="tycho.yaml")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument(
        "--source",
        choices=("all", "github_releases", "website_changelog"),
        default="all",
    )
    args = parser.parse_args()

    differ_mode = configured_differ_mode()
    # Local acquisition is also Gemini-only. The semantic differ uses Vertex/
    # Agent Platform ADC and deliberately never loads a local AI Studio key.
    config = load_config(args.config)
    settings = LocalSettings(root=Path(args.data_dir).resolve())
    settings.root.mkdir(parents=True, exist_ok=True)
    lock_path = settings.root / "fleet.lock"

    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another local Tycho fleet cycle is already running")

        adapter = GithubReleasesAdapter()
        with LocalBackend(config, settings) as backend:
            pending_before = backend.process_pending()
            semantic_differ = SemanticDiffer()
            retry_results = retry_incomplete_generation_pairs(
                backend,
                semantic_differ,
                mode="semantic",
            )
            results = []
            webpage_adapter = WebpageAdapter()
            for entity_key, entity in config.entities.items():
                watchers = []
                if args.source in {"all", "github_releases"} and entity.sources.github_releases:
                    watchers.append(
                        (
                            "github_releases",
                            lambda: acquire_github_releases(
                                entity_key,
                                entity,
                                backend,
                                adapter,
                                differ=semantic_differ,
                                mode=differ_mode,
                                retry_pending=False,
                            ),
                        )
                    )
                if args.source in {"all", "website_changelog"} and entity.sources.website_changelog:
                    watchers.append(
                        (
                            "website_changelog",
                            lambda: acquire_website_changelog(
                                entity_key,
                                entity,
                                backend,
                                webpage_adapter,
                                differ=semantic_differ,
                                mode=differ_mode,
                                retry_pending=False,
                            ),
                        )
                    )
                for source, watcher in watchers:
                    try:
                        result = watcher()
                        results.append({"source": source, **result.__dict__})
                    except Exception as exc:
                        results.append(
                            {
                                "entity": entity_key,
                                "source": source,
                                "outcome": "pipeline_failed",
                                "error": str(exc),
                            }
                        )
            pending_after = backend.process_pending()
            output = {
                "run_at": datetime.now(UTC).isoformat(),
                "database": str(settings.database),
                "differ_mode": differ_mode,
                "pending_before": pending_before,
                "retried_generation_pairs": [
                    {
                        "state": item.state,
                        "outcome": item.outcome,
                        "run_id": item.run_id,
                        "delta_id": item.delta.delta_id if item.delta else None,
                    }
                    for item in retry_results
                ],
                "watchers": results,
                "pending_after": pending_after,
                "stats": backend.stats(),
            }
            print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
