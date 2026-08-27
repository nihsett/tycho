"""Container entrypoint: ``python -m dashboard.api.serve``.

Uvicorn's own access log is deliberately off. It records the raw request line,
including the query string, and the dashboard's persisted log lines must stay
structural: route template, method, status, latency, and validated IDs only.
That record is emitted by ``StructuralLogMiddleware`` instead.
"""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    uvicorn.run(
        "dashboard.api.main:app",
        host="0.0.0.0",  # noqa: S104 - Cloud Run requires binding all interfaces
        port=int(os.getenv("PORT", "8080")),
        access_log=False,
        log_level=os.getenv("TYCHO_DASHBOARD_LOG_LEVEL", "info"),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
