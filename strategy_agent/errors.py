"""Bounded, content-free error metadata for strategy sessions.

``str(exception)`` is unsafe to persist.  Pydantic's ``ValidationError`` renders
``input_value``, so a malformed model response, a claim statement, or a grounded
quote can end up inside the message.  Every durable strategy record therefore
stores only a stage, an exception class name, and a short curated reason chosen
from this module -- never the exception's own text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MAX_ERROR_LENGTH = 200


class Stage(StrEnum):
    """Where in the bounded workflow the failure happened."""

    CONTEXT = "context"
    STRATEGIST = "strategist"
    PROPOSAL_VALIDATION = "proposal_validation"
    CHALLENGER = "challenger"
    CHALLENGE_GATE = "challenge_gate"
    BRIEF_WRITER = "brief_writer"
    CITATION = "citation"
    PERSISTENCE = "persistence"
    UNKNOWN = "unknown"


#: Curated reasons keyed by exception class name.  Anything not listed here
#: collapses to a generic reason, so a new exception type cannot leak text by
#: simply not having been considered yet.
_REASONS: dict[str, str] = {
    "StrategyContextTooLarge": "bounded context exceeded its byte or token budget",
    "ValidationError": "structured output failed schema validation",
    "StrategyModelError": "agent returned no usable structured output",
    "CitationError": "brief citation was unpinned, malformed, or a URL",
    "StrategyRequestError": "trigger payload is not a valid bounded request",
    "SessionPersistenceError": "session persistence failed",
    "KeyError": "a required record was missing",
    "ValueError": "a bounded invariant was violated",
    "RuntimeError": "the session could not complete",
    "TimeoutError": "an agent turn timed out",
}
_GENERIC = "an unexpected failure ended the session"


@dataclass(frozen=True)
class SafeError:
    """The only error shape a strategy record ever persists."""

    stage: Stage
    error_class: str
    reason: str

    def as_text(self) -> str:
        return f"{self.stage.value}:{self.error_class}: {self.reason}"[:MAX_ERROR_LENGTH]

    def as_dict(self) -> dict[str, str]:
        return {
            "stage": self.stage.value,
            "error_class": self.error_class,
            "reason": self.reason,
        }


def sanitize_error(exc: BaseException, stage: Stage = Stage.UNKNOWN) -> SafeError:
    """Reduce any exception to stage, class, and a curated reason.

    The exception's own message is deliberately never read.
    """
    error_class = type(exc).__name__
    # Only the class NAME is consulted; ``str(exc)`` is never touched.
    return SafeError(
        stage=stage,
        error_class=error_class[:64],
        reason=_REASONS.get(error_class, _GENERIC),
    )


def safe_error_text(exc: BaseException, stage: Stage = Stage.UNKNOWN) -> str:
    """Convenience wrapper returning the bounded text for durable storage."""
    return sanitize_error(exc, stage).as_text()
