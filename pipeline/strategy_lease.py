"""Transactional strategy-session lease contracts shared by local and cloud stores.

The lease identity is ``(period_from, period_to, strategy_version)``.  A weekly
Scheduler trigger and a dashboard "Run Strategy Session" click therefore land on
the same identity: the second one returns the existing session instead of
starting a second model run.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

StrategyLeaseState = Literal["acquired", "completed", "active"]


class SessionPersistenceError(RuntimeError):
    """A write-once session/brief/lease commit could not be applied."""


@dataclass(frozen=True)
class StrategyLeaseDecision:
    """Result of the atomic acquire operation for one strategy period."""

    state: StrategyLeaseState
    session_id: str | None = None
    attempt: int = 0


def strategy_lease_document_id(
    period_from: datetime, period_to: datetime, strategy_version: str
) -> str:
    """Return a stable, Firestore-safe key for one strategy session identity."""
    identity = f"{period_from.isoformat()}\0{period_to.isoformat()}\0{strategy_version}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def strategy_lease_is_active(expires_at: datetime | None, now: datetime) -> bool:
    """Treat a lease as active only while its expiration is in the future."""
    return expires_at is not None and expires_at > now
