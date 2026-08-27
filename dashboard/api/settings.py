"""Environment settings for the dashboard service.

Nothing here is a secret.  The browser never receives a credential: the API
uses its own Cloud Run service account through ADC, and the only outbound call
it can make is an authenticated POST to the private Strategy dispatcher.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Overview and health may be served from a short bounded cache.  An
#: in-progress strategy stream is never cached, and provenance and timelines are
#: always read fresh so a claim version can never be reported stale.
DEFAULT_CACHE_SECONDS = 60.0
MAX_REQUEST_BYTES = 4_096
DEFAULT_DISPATCH_TIMEOUT_SECONDS = 870.0


@dataclass(frozen=True)
class DashboardSettings:
    project: str
    dataset: str = "tycho"
    config_path: str = "tycho.yaml"
    static_dir: Path | None = None
    strategy_dispatcher_url: str | None = None
    cache_seconds: float = DEFAULT_CACHE_SECONDS
    dispatch_timeout_seconds: float = DEFAULT_DISPATCH_TIMEOUT_SECONDS
    service_name: str = "tycho-dashboard"
    revision: str = "local"

    @classmethod
    def from_env(cls) -> "DashboardSettings":
        project = os.getenv("TYCHO_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
        if not project:
            raise RuntimeError("TYCHO_PROJECT or GOOGLE_CLOUD_PROJECT is required")
        static = os.getenv("TYCHO_DASHBOARD_STATIC", "dashboard/frontend/dist")
        static_path = Path(static) if static else None
        return cls(
            project=project,
            dataset=os.getenv("TYCHO_DATASET", "tycho"),
            config_path=os.getenv("TYCHO_CONFIG", "tycho.yaml"),
            static_dir=static_path if static_path and static_path.is_dir() else None,
            strategy_dispatcher_url=os.getenv("TYCHO_STRATEGY_DISPATCHER_URL") or None,
            cache_seconds=float(os.getenv("TYCHO_DASHBOARD_CACHE_SECONDS", DEFAULT_CACHE_SECONDS)),
            dispatch_timeout_seconds=float(
                os.getenv("TYCHO_DASHBOARD_DISPATCH_TIMEOUT", DEFAULT_DISPATCH_TIMEOUT_SECONDS)
            ),
            service_name=os.getenv("K_SERVICE", "tycho-dashboard"),
            revision=os.getenv("K_REVISION", "local"),
        )
