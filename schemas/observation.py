"""Immutable acquisition observation schema."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from schemas.common import AwareDatetime, NonEmptyStr, ObservationId


class ObservationKind(StrEnum):
    STRUCTURED = "structured"
    TEXT = "text"
    IMAGE = "image"


class ObservationStatus(StrEnum):
    OK = "ok"
    FETCH_FAILED = "fetch_failed"
    QUARANTINED = "quarantined"


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    obs_id: ObservationId
    entity: NonEmptyStr
    source: NonEmptyStr
    kind: ObservationKind
    fetched_at: AwareDatetime
    content_ref: NonEmptyStr
    content_hash: NonEmptyStr
    adapter_ver: NonEmptyStr
    status: ObservationStatus
