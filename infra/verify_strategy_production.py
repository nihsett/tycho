"""Read-only validation of one production strategy session and its telemetry.

    uv run python -m infra.verify_strategy_production session
    uv run python -m infra.verify_strategy_production telemetry
    uv run python -m infra.verify_strategy_production duplicate --session-id sts_...

Nothing here writes to Firestore, BigQuery, Cloud Storage, or Pub/Sub, and
nothing here calls a model.  It reads the session the council already persisted
and re-derives, in Python, every claim the deployment evidence makes:

- each passed card pins an exact active claim version resting only on canonical
  meaningful Gemini ``delta@2`` rows;
- no rejected card reached the brief;
- the brief cites only pinned claim versions and no URL;
- traces and dispatcher logs carry structure and safe IDs, and none of the
  governed prose the session actually read appears anywhere in them.

That last check is the decisive one.  Scanning for forbidden *field names* only
catches leaks somebody already thought of, so this module also pulls the real
claim statements, rationales, Delta change statements, grounded quotes, and the
brief's own prose out of the store and proves none of that text occurs in any
exported payload.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pipeline.strategy_context import is_canonical
from pipeline.strategy_evidence import evidence_defect, source_family
from schemas.brief import Brief
from schemas.claim import Claim, ClaimStatus
from schemas.delta import Triage
from schemas.strategy import CardStatus, SessionState, StrategySession, manifest_hash
from strategy_agent.citations import find_citations

DEFAULT_PROJECT = "gen-lang-client-0110801105"
DEFAULT_LOCATION = "us-central1"
DEFAULT_OUTPUT = Path("data/strategy_production_session.json")
DISPATCHER_SERVICE = "tycho-strategy-dispatcher"

#: Gemini 3.7 Flash promotional rates, used here only as a documented UPPER
#: bound: the council runs 3.5 Flash-Lite, which is cheaper.  Price constants
#: deliberately live outside domain validation.
UPPER_BOUND_INPUT_USD_PER_MTOK = 0.75
UPPER_BOUND_OUTPUT_USD_PER_MTOK = 3.75

#: Field names that would mean the redacting span processor failed.
FORBIDDEN_KEY_SUBSTRINGS = (
    "llm_request",
    "llm_response",
    "system_instruction",
    "system_instructions",
    "user_messages",
    "tool_definitions",
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.response.text",
    "message_content",
    "input_value",
    "output_value",
    "input.value",
    "output.value",
    "prompt",
    "completion",
    "statement",
    "rationale",
    "quote",
    "rendered_md",
    "model_output",
    "request_content",
)

#: A citation as it survives into a persisted brief: the write-once render
#: has already replaced ``<claim id=... version=.../>`` with its dashboard link.
RENDERED_CITATION = re.compile(
    r"\(/claims/(?P<claim_id>clm_[0-7][0-9A-HJKMNP-TV-Z]{25})\?version=(?P<version>\d{1,6})\)"
)

#: The smallest run of governed prose worth searching for.  Short enough to
#: catch a truncated leak, long enough not to fire on an ordinary word.
LEAK_WINDOW = 32


class VerificationError(RuntimeError):
    """The production evidence does not hold; do not record it as passing."""


@dataclass(frozen=True)
class Corpus:
    """Governed prose the session read, plus the prose it wrote."""

    snippets: tuple[tuple[str, str], ...]

    def leaks_in(self, payload: Any) -> list[str]:
        haystack = _normalize(json.dumps(payload, default=str))
        return sorted(
            {label for label, snippet in self.snippets if snippet and snippet in haystack}
        )


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _windows(text: str | None) -> list[str]:
    normalized = _normalize(text or "")
    return [normalized[:LEAK_WINDOW]] if len(normalized) >= LEAK_WINDOW else []


# --- Store reads ------------------------------------------------------------


def open_store(project: str) -> Any:
    from pipeline.cloud import CloudBackend, CloudSettings

    return CloudBackend(
        CloudSettings(project=project, bucket=f"{project}-tycho-raw")
    )


def latest_session(store: Any, period_from: datetime, period_to: datetime) -> StrategySession:
    matches = [
        session
        for session in store.strategy_sessions()
        if session.period.from_ == period_from and session.period.to == period_to
    ]
    if not matches:
        raise VerificationError(
            f"no strategy session exists for {period_from.isoformat()}..{period_to.isoformat()}"
        )
    return max(matches, key=lambda session: session.created_at)


def governed_corpus(store: Any, session: StrategySession, brief: Brief | None) -> Corpus:
    """Every piece of prose that must never appear in a trace or a log line."""
    snippets: list[tuple[str, str]] = []
    for entry in session.input_manifest:
        claim = store.get_claim(entry.claim_id)
        if claim is None:
            continue
        for label, text in (("claim_statement", claim.statement), ("claim_rationale", claim.rationale)):
            snippets.extend((f"{label}:{claim.claim_id}", window) for window in _windows(text))
        for item in claim.evidence:
            delta = store.get_delta(item.delta_id)
            if delta is None:
                continue
            for change in delta.changes:
                snippets.extend(
                    (f"delta_change:{delta.delta_id}", window)
                    for window in _windows(change.statement)
                )
                for grounded in (change.evidence_before, change.evidence_after):
                    if grounded is not None:
                        snippets.extend(
                            (f"evidence_quote:{delta.delta_id}", window)
                            for window in _windows(grounded.quote)
                        )
    for card in session.cards:
        for label, text in (
            ("card_statement", card.statement),
            ("card_rationale", card.rationale),
            ("card_competing_explanation", card.competing_explanation),
            ("card_falsifier", card.falsifier),
        ):
            snippets.extend((f"{label}:{card.card_id}", window) for window in _windows(text))
    if brief is not None:
        snippets.extend(("brief_prose", window) for window in _windows(brief.rendered_md))
    return Corpus(snippets=tuple(dict.fromkeys(snippets)))


# --- Session evidence -------------------------------------------------------


def premise_evidence(store: Any, claim_id: str, claim_version: int) -> dict[str, Any]:
    """Re-resolve one pinned premise from the store; never trust the record."""
    claim: Claim | None = store.get_claim(claim_id)
    if claim is None:
        raise VerificationError(f"pinned premise {claim_id} no longer exists")
    if claim.status is not ClaimStatus.ACTIVE:
        raise VerificationError(f"pinned premise {claim_id} is {claim.status.value}, not active")
    if claim.version != claim_version:
        raise VerificationError(
            f"pinned premise {claim_id} is at version {claim.version}, not {claim_version}"
        )
    deltas = []
    for item in claim.evidence:
        delta = store.get_delta(item.delta_id)
        defect = evidence_defect(claim, item, delta)
        if defect is not None or delta is None:
            raise VerificationError(f"pinned premise {claim_id} rests on {defect}")
        if not is_canonical(delta) or delta.triage is not Triage.MEANINGFUL:
            raise VerificationError(f"pinned premise {claim_id} rests on a noncanonical Delta")
        deltas.append(delta)
    return {
        "claim_id": claim_id,
        "claim_version": claim_version,
        "entity": claim.entity,
        "scope": claim.scope,
        "confidence": claim.confidence.value,
        "delta_ids": sorted(delta.delta_id for delta in deltas),
        "source_families": sorted(
            {source_family(delta.entity, delta.source) for delta in deltas}
        ),
    }


def session_evidence(store: Any, session: StrategySession, brief: Brief | None) -> dict[str, Any]:
    """Bounded, recomputed evidence for one persisted session."""
    passed = session.passed_cards()
    rejected = session.rejected_cards()
    cards: list[dict[str, Any]] = []
    for card in session.cards:
        record = {
            "card_id": card.card_id,
            "status": card.status.value,
            "confidence": card.confidence.value,
            "entities": list(card.entities),
            "scopes": list(card.scopes),
            "source_families": list(card.source_families),
            "premises": [
                f"{premise.claim_id}@{premise.claim_version}" for premise in card.premises
            ],
            "rejection_reason_count": len(card.rejection_reasons),
        }
        if card.status is CardStatus.PASSED:
            record["verified_premises"] = [
                premise_evidence(store, premise.claim_id, premise.claim_version)
                for premise in card.premises
            ]
            entities = {item["entity"] for item in record["verified_premises"]}
            families = {
                family
                for item in record["verified_premises"]
                for family in item["source_families"]
            }
            if len(entities) < 2 or len(families) < 2:
                raise VerificationError(
                    f"passed card {card.card_id} does not span two entities and two families"
                )
        cards.append(record)

    brief_record: dict[str, Any] | None = None
    if brief is not None:
        pinned = {
            (premise.claim_id, premise.claim_version)
            for card in passed
            for premise in card.premises
        }
        cited = {
            (match.group("claim_id"), int(match.group("version")))
            for match in RENDERED_CITATION.finditer(brief.rendered_md)
        }
        unpinned = sorted(cited - pinned)
        if unpinned:
            raise VerificationError(f"brief cites unpinned claim versions: {unpinned}")
        if find_citations(brief.rendered_md):
            raise VerificationError("brief still holds an unreplaced citation marker")
        if re.search(r"https?://", brief.rendered_md):
            raise VerificationError("brief cites a URL; Tycho citations are claim versions")
        recorded = {(item.claim_id, item.version) for item in brief.claims_referenced}
        if recorded != pinned:
            raise VerificationError(
                "brief's pinned claim versions do not match its passed cards' premises"
            )
        rejected_ids = {card.card_id for card in rejected}
        if rejected_ids & set(brief.strategy_card_ids):
            raise VerificationError("a rejected card reached the brief")
        brief_record = {
            "brief_id": brief.brief_id,
            "strategy_session_id": brief.strategy_session_id,
            "strategy_card_ids": list(brief.strategy_card_ids),
            "claims_referenced": [
                f"{item.claim_id}@{item.version}" for item in brief.claims_referenced
            ],
            "citation_markers": sorted(f"{claim}@{version}" for claim, version in cited),
            "stats": brief.stats.model_dump(mode="json"),
            "rendered_bytes": len(brief.rendered_md.encode("utf-8")),
            "cites_a_url": False,
        }

    metrics = session.metrics
    return {
        "session_id": session.session_id,
        "strategy_version": session.strategy_version,
        "state": session.state.value,
        "period": {
            "from": session.period.from_.isoformat(),
            "to": session.period.to.isoformat(),
        },
        "manifest_hash": session.manifest_hash,
        "manifest_hash_recomputed": manifest_hash(session.input_manifest),
        "manifest_claim_versions": [
            f"{entry.claim_id}@{entry.claim_version}" for entry in session.input_manifest
        ],
        "agent_versions": session.agent_versions.model_dump(mode="json"),
        "model_versions": session.model_versions.model_dump(mode="json"),
        "run_ids": list(session.run_ids),
        "metrics_evidence": [metric.model_dump(mode="json") for metric in session.metrics_evidence],
        "cards": cards,
        "counts": {
            "proposed": metrics.cards_proposed,
            "passed": metrics.cards_passed,
            "rejected": metrics.cards_rejected,
            "zero_card_result": not passed,
        },
        "usage": {
            "input_bytes": metrics.input_bytes,
            "estimated_input_tokens": metrics.estimated_input_tokens,
            "input_tokens": metrics.input_tokens,
            "output_tokens": metrics.output_tokens,
            "total_tokens": metrics.total_tokens,
            "latency_ms": metrics.latency_ms,
            "model_calls": len(session.run_ids),
            "estimated_cost_usd_upper_bound": round(
                metrics.input_tokens / 1_000_000 * UPPER_BOUND_INPUT_USD_PER_MTOK
                + metrics.output_tokens / 1_000_000 * UPPER_BOUND_OUTPUT_USD_PER_MTOK,
                6,
            ),
            "cost_basis": "gemini-3.7-flash promotional rate as an upper bound",
        },
        "brief": brief_record,
        "error": session.error,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
    }


# --- Telemetry --------------------------------------------------------------


def unsafe_fields(payload: Any) -> list[str]:
    """Return every key path whose name would mean content leaked."""
    found: list[str] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                current = f"{path}.{key}" if path else str(key)
                lowered = str(key).casefold()
                if any(part in lowered for part in FORBIDDEN_KEY_SUBSTRINGS):
                    found.append(current)
                walk(child, current)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(payload)
    return sorted(set(found))


def fetch_traces(
    project: str, *, since: datetime, page_size: int = 200, max_pages: int = 50
) -> list[dict[str, Any]]:
    """Every trace in the window, following pagination to exhaustion.

    ``pageSize`` on Cloud Trace v1 is a scan budget over the project's traces,
    not a result count: a single page silently returns a SUBSET of the matching
    traces.  Stopping at one page would make "every persisted trace was
    inspected" false, so this follows ``nextPageToken`` and fails closed rather
    than truncate.
    """
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    session = AuthorizedSession(credentials)
    url = f"https://cloudtrace.googleapis.com/v1/projects/{quote(project)}/traces"
    params = {
        "view": "COMPLETE",
        "pageSize": str(page_size),
        "startTime": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    traces: dict[str, dict[str, Any]] = {}
    token: str | None = None
    for _ in range(max_pages):
        page_params = dict(params)
        if token:
            page_params["pageToken"] = token
        response = session.get(url, params=page_params, timeout=60)
        response.raise_for_status()
        body = response.json()
        for trace in body.get("traces", []):
            traces[trace["traceId"]] = trace
        token = body.get("nextPageToken")
        if not token:
            return list(traces.values())
    raise VerificationError(
        f"Cloud Trace paging did not terminate after {max_pages} pages; "
        "the inspection would be incomplete"
    )


def fetch_dispatcher_logs(
    project: str, *, since: datetime, limit: int = 200
) -> list[dict[str, Any]]:
    filter_expr = (
        'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{DISPATCHER_SERVICE}" '
        f'AND timestamp>="{since.strftime("%Y-%m-%dT%H:%M:%SZ")}"'
    )
    result = subprocess.run(
        (
            "gcloud",
            "logging",
            "read",
            filter_expr,
            f"--project={project}",
            f"--limit={limit}",
            "--format=json",
        ),
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout or "[]")


def _dispatcher_result_line(entry: dict[str, Any]) -> dict[str, Any] | None:
    """One bounded dispatcher result, however Cloud Logging chose to store it.

    Cloud Run parses a JSON stdout line into ``jsonPayload`` and leaves anything
    else in ``textPayload``; read both so the evidence does not depend on that.
    """
    payload = entry.get("jsonPayload")
    if isinstance(payload, dict) and "request_id" in payload:
        return payload
    text = entry.get("textPayload")
    if isinstance(text, str) and text.lstrip().startswith("{"):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(decoded, dict) and "request_id" in decoded:
            return decoded
    return None


def telemetry_evidence(
    project: str, *, since: datetime, corpus: Corpus, runtime_resource: str | None
) -> dict[str, Any]:
    traces = fetch_traces(project, since=since)
    logs = fetch_dispatcher_logs(project, since=since)

    trace_records = []
    for trace in traces:
        spans = trace.get("spans", [])
        trace_records.append(
            {
                "trace_id": trace.get("traceId"),
                "span_count": len(spans),
                "span_names": sorted({span.get("name", "") for span in spans}),
                "unsafe_fields": unsafe_fields(trace),
                "governed_prose_leaks": corpus.leaks_in(trace),
            }
        )
    log_records = [
        {
            "timestamp": entry.get("timestamp"),
            "severity": entry.get("severity"),
            "unsafe_fields": unsafe_fields(entry),
            "governed_prose_leaks": corpus.leaks_in(entry),
        }
        for entry in logs
    ]
    structural_lines = [line for entry in logs if (line := _dispatcher_result_line(entry))]
    return {
        "window_start_utc": since.isoformat(),
        "runtime_resource": runtime_resource,
        "traces_inspected": len(trace_records),
        "trace_ids": [record["trace_id"] for record in trace_records],
        "traces": trace_records,
        "log_entries_inspected": len(log_records),
        "dispatcher_result_lines": structural_lines,
        "unsafe_field_hits": sorted(
            {field for record in [*trace_records, *log_records] for field in record["unsafe_fields"]}
        ),
        "governed_prose_leaks": sorted(
            {
                leak
                for record in [*trace_records, *log_records]
                for leak in record["governed_prose_leaks"]
            }
        ),
        "searched_prose_snippets": len(corpus.snippets),
        "leakage_found": bool(
            any(record["unsafe_fields"] or record["governed_prose_leaks"] for record in trace_records)
            or any(record["unsafe_fields"] or record["governed_prose_leaks"] for record in log_records)
        ),
    }


# --- Actions ----------------------------------------------------------------


def _period(args: argparse.Namespace) -> tuple[datetime, datetime]:
    from pipeline.strategy_dispatcher import previous_complete_week

    if args.period_from and args.period_to:
        return (
            datetime.fromisoformat(args.period_from),
            datetime.fromisoformat(args.period_to),
        )
    return previous_complete_week(datetime.now(UTC))


def _merge(path: Path, key: str, record: dict[str, Any]) -> None:
    existing: dict[str, Any] = {}
    if path.exists():
        existing = json.loads(path.read_text())
    existing[key] = record
    existing["updated_at"] = datetime.now(UTC).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")


def session_action(args: argparse.Namespace) -> None:
    store = open_store(args.project)
    period_from, period_to = _period(args)
    session = latest_session(store, period_from, period_to)
    brief = store.get_brief(session.brief_id) if session.brief_id else None
    if session.state is SessionState.RUNNING:
        raise VerificationError(f"session {session.session_id} is still running")
    record = session_evidence(store, session, brief)
    record["sessions_for_period"] = sum(
        1
        for candidate in store.strategy_sessions()
        if candidate.period.from_ == period_from and candidate.period.to == period_to
    )
    _merge(args.output, "production_session", record)
    print(json.dumps(record, indent=2, sort_keys=True))


def telemetry_action(args: argparse.Namespace) -> None:
    store = open_store(args.project)
    period_from, period_to = _period(args)
    session = latest_session(store, period_from, period_to)
    brief = store.get_brief(session.brief_id) if session.brief_id else None
    corpus = governed_corpus(store, session, brief)
    since = session.created_at - timedelta(minutes=args.lookback_minutes)
    record = telemetry_evidence(
        args.project, since=since, corpus=corpus, runtime_resource=args.runtime_resource
    )
    _merge(args.output, "telemetry_inspection", record)
    print(json.dumps(record, indent=2, sort_keys=True))
    if record["leakage_found"]:
        raise VerificationError(f"unsafe telemetry: {record['unsafe_field_hits']}")


def duplicate_action(args: argparse.Namespace) -> None:
    store = open_store(args.project)
    period_from, period_to = _period(args)
    sessions = [
        candidate
        for candidate in store.strategy_sessions()
        if candidate.period.from_ == period_from and candidate.period.to == period_to
    ]
    # A failed attempt is a legitimately retryable session for the same period,
    # so the assertion is about COMPLETED sessions: a duplicate trigger must not
    # produce a second one, a second brief, or another model call.
    completed = sorted(
        candidate.session_id
        for candidate in sessions
        if candidate.state is SessionState.COMPLETED
    )
    failed = sorted(
        candidate.session_id
        for candidate in sessions
        if candidate.state is SessionState.FAILED
    )
    record = {
        "period": {"from": period_from.isoformat(), "to": period_to.isoformat()},
        "sessions_for_period": len(sessions),
        "session_ids": sorted(candidate.session_id for candidate in sessions),
        "completed_session_ids": completed,
        "failed_retryable_session_ids": failed,
        "briefs_for_period": sorted(
            candidate.brief_id for candidate in sessions if candidate.brief_id
        ),
        "total_model_calls": sum(len(candidate.run_ids) for candidate in sessions),
        "total_tokens": sum(candidate.metrics.total_tokens for candidate in sessions),
    }
    if args.session_id:
        record["expected_session_id"] = args.session_id
        record["duplicate_returned_existing_session"] = completed == [args.session_id]
        record["exactly_one_completed_session"] = len(completed) == 1
        record["exactly_one_brief"] = len(record["briefs_for_period"]) == 1
    _merge(args.output, "duplicate_trigger", record)
    print(json.dumps(record, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["session", "telemetry", "duplicate"])
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--period-from")
    parser.add_argument("--period-to")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--runtime-resource")
    parser.add_argument("--session-id")
    parser.add_argument("--lookback-minutes", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    {
        "session": session_action,
        "telemetry": telemetry_action,
        "duplicate": duplicate_action,
    }[args.action](args)


if __name__ == "__main__":
    main()
