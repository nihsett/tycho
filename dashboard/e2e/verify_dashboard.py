"""Verify the deployed public read-only dashboard end to end.

    uv run python -m dashboard.e2e.verify_dashboard

What this proves, against the real deployed service and real production data:

1. the public read surface answers without a Google credential;
2. every read endpoint sets the required security headers and no CORS header;
3. one bounded Strategy Session trigger reaches the existing private Strategy
   dispatcher, and the shared lease makes it duplicate-safe;
4. the safe SSE stream carries structure only;
5. the stored brief, the rejected cards, and one claim -> Delta -> Observation
   provenance chain all resolve;
6. no claim, Delta, Observation, session, or brief changed as a result.

The complete verification still uses ``gcloud run services proxy`` so it can
read the exact deployed service selected by project and region. The public probe
itself goes directly to the Cloud Run URL with no key material or identity token.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_PROJECT = "gen-lang-client-0110801105"
DEFAULT_REGION = "us-central1"
SERVICE = "tycho-dashboard"
DEFAULT_OUTPUT = Path("data/dashboard_production_verification.json")
PROXY_PORT = 8711
PROXY_READY_SECONDS = 60

#: Field names and payload markers that must never appear in an API response.
FORBIDDEN_MARKERS = (
    "gs://",
    "content_ref",
    "content_hash",
    "llm_request",
    "llm_response",
    "system_instruction",
    "gen_ai.prompt",
    "gen_ai.completion",
    "input_value",
    "output_value",
    "rendered_prompt",
    "tool_definitions",
)

REQUIRED_HEADERS = (
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
)


def gcloud_json(*args: str) -> Any:
    result = subprocess.run(
        ("gcloud", *args, "--format=json"), check=True, text=True, capture_output=True
    )
    return json.loads(result.stdout or "null")


def service_url(project: str, region: str) -> str:
    service = gcloud_json(
        "run", "services", "describe", SERVICE, f"--region={region}", "--project", project
    )
    url = (service.get("status") or {}).get("url")
    if not url:
        raise SystemExit("the dashboard has no Cloud Run URL")
    return str(url)


def data_snapshot(project: str) -> dict[str, Any]:
    from infra.deploy_dashboard import data_snapshot as snapshot

    return snapshot(project)


class Proxy:
    """``gcloud run services proxy`` for the length of the verification."""

    def __init__(self, project: str, region: str, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self._process = subprocess.Popen(
            (
                "gcloud",
                "run",
                "services",
                "proxy",
                SERVICE,
                f"--region={region}",
                f"--port={port}",
                "--project",
                project,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def __enter__(self) -> "Proxy":
        import httpx

        deadline = time.monotonic() + PROXY_READY_SECONDS
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise SystemExit("gcloud run services proxy exited; check your access")
            try:
                httpx.get(f"{self.base}/api/meta", timeout=10.0)
                return self
            except Exception:
                time.sleep(1.0)
        raise SystemExit("the authenticated proxy did not become ready")

    def __exit__(self, *_: object) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - shutdown path
            self._process.kill()


def check_headers(headers: Any) -> dict[str, Any]:
    lowered = {key.lower(): value for key, value in headers.items()}
    return {
        "present": sorted(name for name in REQUIRED_HEADERS if name in lowered),
        "missing": sorted(name for name in REQUIRED_HEADERS if name not in lowered),
        "cors_header_present": "access-control-allow-origin" in lowered,
        "csp": lowered.get("content-security-policy", ""),
    }


def scan(payload: str) -> list[str]:
    lowered = payload.casefold()
    return sorted({marker for marker in FORBIDDEN_MARKERS if marker.casefold() in lowered})


def run_verification(args: argparse.Namespace) -> dict[str, Any]:
    import httpx

    url = service_url(args.project, args.region)
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "project": args.project,
        "region": args.region,
        "service": SERVICE,
        "url": url,
        "checks": {},
    }

    public = httpx.get(f"{url}/api/meta", timeout=30.0, follow_redirects=False)
    report["checks"]["public_request"] = {
        "status": public.status_code,
        "available": public.status_code == 200,
        "headers": check_headers(public.headers),
    }

    report["data_before"] = data_snapshot(args.project)

    with Proxy(args.project, args.region, args.port) as proxy:
        client = httpx.Client(base_url=proxy.base, timeout=180.0)

        reads: dict[str, Any] = {}
        forbidden: dict[str, list[str]] = {}

        def read(name: str, path: str) -> Any:
            response = client.get(path)
            body = response.text
            hits = scan(body)
            if hits:
                forbidden[name] = hits
            reads[name] = {
                "path": path,
                "status": response.status_code,
                "bytes": len(body),
                "headers": check_headers(response.headers),
            }
            return response.json() if response.status_code == 200 else None

        # The page itself, and the bundle it loads. A published bundle must
        # contain no credential, no project secret, and no database endpoint.
        index = client.get("/")
        script = ""
        bundle: dict[str, Any] = {"status": index.status_code}
        if index.status_code == 200:
            import re as _re

            match = _re.search(r'src="(/assets/[A-Za-z0-9._-]+\.js)"', index.text)
            if match:
                asset = client.get(match.group(1))
                script = asset.text
                bundle = {
                    "status": index.status_code,
                    "content_type": index.headers.get("content-type", ""),
                    "asset": match.group(1),
                    "asset_status": asset.status_code,
                    "asset_bytes": len(script),
                    "secret_markers": sorted(
                        marker
                        for marker in (
                            "BEGIN PRIVATE KEY",
                            "service_account",
                            "GOOGLE_API_KEY",
                            "AIza",
                            "client_secret",
                            "Bearer ",
                            "firestore.googleapis.com",
                            "bigquery.googleapis.com",
                            "gen-lang-client-",
                        )
                        if marker in script
                    ),
                }
        report["checks"]["bundle"] = bundle

        meta = read("meta", "/api/meta")
        health = read("health", "/api/health")
        overview = read("overview", "/api/overview")

        entities = (meta or {}).get("entities", [])
        timelines: dict[str, Any] = {}
        first_claim: tuple[str, int] | None = None
        for entity in entities:
            page = read(f"timeline:{entity}", f"/api/entities/{entity}/timeline?limit=50")
            if page:
                timelines[entity] = {
                    "total": page["total"],
                    "returned": len(page["events"]),
                    "kinds": sorted({event["kind"] for event in page["events"]}),
                }
                for event in page["events"]:
                    if first_claim is None and event["claim"]["status"] == "active":
                        first_claim = (event["claim"]["claim_id"], event["claim"]["version"])

        provenance = None
        if first_claim is not None:
            provenance = read(
                "provenance",
                f"/api/claims/{first_claim[0]}/versions/{first_claim[1]}/provenance",
            )

        latest = read("strategy_latest", "/api/strategy/sessions/latest")
        session_id = ((latest or {}).get("session") or {}).get("session_id")
        events = read("strategy_events", f"/api/strategy/sessions/{session_id}/events") if session_id else None

        rejections = [
            {
                "card_id": card["card_id"],
                "reasons": card["rejection_reasons"],
                "entities": card["entities"],
                "source_families": card["source_families"],
            }
            for card in (latest or {}).get("rejected_cards", [])
        ]

        errors = {
            "unknown_entity": client.get("/api/entities/not_real/timeline").status_code,
            "unknown_scope": client.get(
                "/api/entities/codex/timeline?scope=not_a_scope"
            ).status_code,
            "malformed_claim_id": client.get(
                "/api/claims/not-an-id/versions/1/provenance"
            ).status_code,
            "limit_over_cap": client.get(
                "/api/entities/codex/timeline?limit=100000"
            ).status_code,
            "unknown_session": client.get(
                "/api/strategy/sessions/sts_01M0000000000000000000000/events"
            ).status_code,
        }

        # Layer 1: a second click while the first run is still in flight must
        # return the SAME run without a second dispatcher call.
        trigger = client.post("/api/strategy/sessions")
        trigger_body = trigger.json() if trigger.status_code == 202 else {"error": trigger.text[:200]}
        inflight = client.post("/api/strategy/sessions")
        inflight_body = inflight.json() if inflight.status_code == 202 else {"error": "refused"}

        stream_events, stream_closed = follow(client, trigger_body.get("stream_path"))

        # Layer 2: a fresh trigger after the run finished normalizes to the same
        # bounded period, so the shared lease returns the existing session and
        # the Runtime makes no model call.
        second = client.post("/api/strategy/sessions")
        second_body = second.json() if second.status_code == 202 else {"error": "refused"}
        second_events, second_closed = follow(client, second_body.get("stream_path"))

        client.close()

    report["checks"].update(
        {
            "reads": reads,
            "forbidden_markers": forbidden,
            "health": health,
            "overview_totals": (overview or {}).get("totals"),
            "timelines": timelines,
            "provenance": summarize_provenance(provenance),
            "strategy_latest": summarize_session(latest),
            "rejected_cards": rejections,
            "derived_events": [
                {
                    "seq": event["seq"],
                    "event": event["event"],
                    "agent": event["agent"],
                    "state": event["state"],
                }
                for event in (events or {}).get("events", [])
            ],
            "bounded_errors": errors,
            "trigger": {
                "status": trigger.status_code,
                "run_id": trigger_body.get("run_id"),
                "session_id": trigger_body.get("session_id"),
                "period_from": trigger_body.get("period_from"),
                "period_to": trigger_body.get("period_to"),
                "duplicate": trigger_body.get("duplicate"),
            },
            "stream": {
                "run_id": trigger_body.get("run_id"),
                "events": [
                    {
                        "seq": event["seq"],
                        "event": event["event"],
                        "agent": event["agent"],
                        "state": event["state"],
                        "derived": event["derived"],
                    }
                    for event in stream_events
                ],
                "closed": stream_closed,
                "forbidden_markers": scan(json.dumps(stream_events)),
            },
            "inflight_duplicate": {
                "status": inflight.status_code,
                "run_id": inflight_body.get("run_id"),
                "same_run": inflight_body.get("run_id") == trigger_body.get("run_id"),
                "duplicate": inflight_body.get("duplicate"),
            },
            "duplicate_trigger": {
                "status": second.status_code,
                "run_id": second_body.get("run_id"),
                "closed": second_closed,
                "events": [
                    {"seq": event["seq"], "event": event["event"], "agent": event["agent"]}
                    for event in second_events
                ],
            },
        }
    )

    report["data_after"] = data_snapshot(args.project)
    report["mutation"] = compare(report["data_before"], report["data_after"])
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["passed"] = verdict(report)
    return report


def follow(client: Any, stream_path: str | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Read one safe SSE stream to its terminal ``stream_closed`` event."""
    if not stream_path:
        return [], None
    events: list[dict[str, Any]] = []
    closed: dict[str, Any] | None = None
    with client.stream("GET", stream_path) as stream:
        kind = None
        for line in stream.iter_lines():
            if line.startswith("event: "):
                kind = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                payload = json.loads(line.removeprefix("data: "))
                if kind == "stream_closed":
                    closed = payload
                    break
                events.append(payload)
    return events, closed


def summarize_provenance(provenance: dict[str, Any] | None) -> dict[str, Any] | None:
    if not provenance:
        return None
    evidence = provenance.get("evidence", [])
    return {
        "claim_id": provenance["claim"]["claim_id"],
        "version": provenance["requested_version"],
        "exact_version": provenance["exact_version"],
        "evidence_deltas": [item["delta_id"] for item in evidence],
        "admissible": all(item["admissible"] for item in evidence),
        "grounded_quotes": sum(
            1
            for item in evidence
            for change in item["changes"]
            if change.get("quote_after") or change.get("quote_before")
        ),
        "observations": [
            {"obs_id": ref["obs_id"], "role": ref["role"], "resolved": ref["resolved"]}
            for item in evidence
            for ref in item["observations"]
        ],
        "source_refs": [
            item["source_ref"]["target"] for item in evidence if item.get("source_ref")
        ],
    }


def summarize_session(latest: dict[str, Any] | None) -> dict[str, Any] | None:
    if not latest or not latest.get("session"):
        return None
    session = latest["session"]
    brief = latest.get("brief")
    return {
        "session_id": session["session_id"],
        "state": session["state"],
        "metrics": session["metrics"],
        "manifest_hash": session["manifest_hash"],
        "brief_id": (brief or {}).get("brief_id"),
        "brief_empty": (brief or {}).get("empty"),
        "brief_bytes": len((brief or {}).get("rendered_md", "")),
        "passed_cards": len(latest.get("passed_cards", [])),
        "rejected_cards": len(latest.get("rejected_cards", [])),
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    differences = {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }
    return {"unchanged": not differences, "differences": differences}


def verdict(report: dict[str, Any]) -> dict[str, bool]:
    checks = report["checks"]
    reads = checks["reads"]
    return {
        "public_read_surface_is_available": bool(
            checks["public_request"]["available"]
            and not checks["public_request"]["headers"]["missing"]
        ),
        "every_read_succeeded": all(item["status"] == 200 for item in reads.values()),
        "security_headers_present": all(
            not item["headers"]["missing"] for item in reads.values()
        ),
        "no_cors_header": not any(
            item["headers"]["cors_header_present"] for item in reads.values()
        ),
        "no_forbidden_markers": not checks["forbidden_markers"]
        and not checks["stream"]["forbidden_markers"],
        "bounded_errors": set(checks["bounded_errors"].values()) <= {400, 404},
        "trigger_accepted": checks["trigger"]["status"] == 202,
        "an_inflight_duplicate_click_reuses_the_same_run": bool(
            checks["inflight_duplicate"]["same_run"]
            and checks["inflight_duplicate"]["duplicate"]
        ),
        "the_lease_returns_the_existing_session_without_a_model_call": bool(
            (checks["stream"]["closed"] or {}).get("duplicate")
            and (checks["duplicate_trigger"]["closed"] or {}).get("duplicate")
            and (checks["stream"]["closed"] or {}).get("session_id")
            == (checks["duplicate_trigger"]["closed"] or {}).get("session_id")
            == (checks["strategy_latest"] or {}).get("session_id")
        ),
        "stream_events_are_structural": all(
            event["event"]
            in {
                "run_started",
                "agent_started",
                "agent_completed",
                "card_rejected",
                "brief_completed",
                "run_failed",
                "heartbeat",
            }
            for event in checks["stream"]["events"]
        ),
        "provenance_chain_resolves": bool(
            checks["provenance"]
            and checks["provenance"]["evidence_deltas"]
            and checks["provenance"]["grounded_quotes"]
            and all(ref["resolved"] for ref in checks["provenance"]["observations"])
        ),
        "no_production_mutation": report["mutation"]["unchanged"],
        "the_page_and_its_bundle_are_served": checks["bundle"].get("asset_status") == 200,
        # Note: the bundle is not the place to check for unsafe HTML injection.
        # React's own runtime names ``dangerouslySetInnerHTML`` internally, so
        # the string is always present once React is bundled. Whether the *app*
        # uses it is a source-level question, asserted in the frontend suite.
        "no_secret_or_database_endpoint_in_the_bundle": (
            checks["bundle"].get("secret_markers") == []
        ),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--port", type=int, default=PROXY_PORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    report = run_verification(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps(report["passed"], indent=2, sort_keys=True))
    print(f"\nfull report: {args.output}")
    if not all(report["passed"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
