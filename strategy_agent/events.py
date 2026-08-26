"""Safe strategy events.

Persisted traces and activity events carry structure, never content.  Prompts,
model responses, claim statements, rationales, quotes, and brief prose are all
excluded by construction: this module builds events from an allowlist, so a new
field cannot leak by being added somewhere upstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SAFE_EVENT_FIELDS = frozenset(
    {
        "session_id",
        "agent",
        "state",
        "card_count",
        "passed_count",
        "rejection_count",
        "claim_versions",
        "run_id",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "latency_ms",
    }
)

UNSAFE_EVENT_FIELDS = frozenset(
    {
        "statement",
        "rationale",
        "summary",
        "quote",
        "prompt",
        "response",
        "instruction",
        "text",
        "rendered_md",
        "document",
        "competing_explanation",
        "falsifier",
        "evidence",
    }
)


@dataclass(frozen=True)
class StrategyEvent:
    """One structural step of a session, safe to persist and to export."""

    session_id: str
    agent: str
    state: str
    card_count: int = 0
    passed_count: int = 0
    rejection_count: int = 0
    claim_versions: list[str] = field(default_factory=list)
    run_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "session_id": self.session_id,
            "agent": self.agent,
            "state": self.state,
            "card_count": self.card_count,
            "passed_count": self.passed_count,
            "rejection_count": self.rejection_count,
            "claim_versions": list(self.claim_versions),
            "run_id": self.run_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
        }
        return {key: value for key, value in payload.items() if key in SAFE_EVENT_FIELDS}


def assert_event_is_safe(payload: dict[str, Any]) -> None:
    """Fail loudly if an event ever grows a content-bearing field."""
    unknown = set(payload) - SAFE_EVENT_FIELDS
    if unknown:
        raise ValueError(f"strategy event contains unsafe fields: {sorted(unknown)}")
