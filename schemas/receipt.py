"""Append-only claim delivery receipt schema."""

import re

from pydantic import BaseModel, ConfigDict, field_validator

from schemas.common import AwareDatetime, ClaimId, NonEmptyStr, ReceiptId

_CONSUMER = re.compile(r"^(?:analyst|qa_agent|brief_writer|watcher:[^/\s]+/[^\s]+)$")


class DeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: ReceiptId
    claim_id: ClaimId
    claim_version: int
    consumer: NonEmptyStr
    context_key: NonEmptyStr
    delivered_at: AwareDatetime

    @field_validator("claim_version")
    @classmethod
    def positive_version(cls, value: int) -> int:
        if value < 1:
            raise ValueError("claim_version must be positive")
        return value

    @field_validator("consumer")
    @classmethod
    def known_consumer(cls, value: str) -> str:
        if not _CONSUMER.fullmatch(value):
            raise ValueError("unknown receipt consumer")
        return value
