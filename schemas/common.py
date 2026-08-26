"""Shared strict scalar types and ID helpers for Tycho schemas."""

from __future__ import annotations

import secrets
import time
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, StringConstraints

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return value


AwareDatetime = Annotated[datetime, AfterValidator(_aware)]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ObservationId = Annotated[str, StringConstraints(pattern=rf"^obs_{_ULID}$")]
DeltaId = Annotated[str, StringConstraints(pattern=rf"^dlt_{_ULID}$")]
ClaimId = Annotated[str, StringConstraints(pattern=rf"^clm_{_ULID}$")]
ReceiptId = Annotated[str, StringConstraints(pattern=rf"^rcp_{_ULID}$")]
BriefId = Annotated[
    str,
    StringConstraints(pattern=r"^brf_[0-9]{4}w(?:0[1-9]|[1-4][0-9]|5[0-3])$"),
]


def new_prefixed_id(prefix: str) -> str:
    """Return a monotonic-time-shaped ULID with a Tycho document prefix."""
    value = ((time.time_ns() // 1_000_000) << 80) | secrets.randbits(80)
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        encoded[index] = _CROCKFORD[value & 31]
        value >>= 5
    return f"{prefix}_{''.join(encoded)}"
