"""Transactional analyst-run lease contracts shared by local and cloud stores."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

LeaseState = Literal["acquired", "completed", "active"]


@dataclass(frozen=True)
class AnalystLeaseDecision:
    """Result of the atomic acquire operation."""

    state: LeaseState
    run_id: str | None = None
    attempt: int = 0


def lease_document_id(delta_id: str, mode: str, analyst_version: str) -> str:
    """Return a stable, Firestore-safe key for one analyst delivery identity."""
    identity = f"{delta_id}\0{mode}\0{analyst_version}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def lease_is_active(expires_at: datetime | None, now: datetime) -> bool:
    """Treat a lease as active only while its expiration is in the future."""
    return expires_at is not None and expires_at > now
