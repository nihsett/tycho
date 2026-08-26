"""Reproducible weekly brief schema."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.common import AwareDatetime, BriefId, ClaimId, NonEmptyStr


class BriefPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime

    @model_validator(mode="after")
    def chronological(self) -> "BriefPeriod":
        if self.from_ >= self.to:
            raise ValueError("brief period 'from' must precede 'to'")
        return self


class ClaimReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: ClaimId
    version: int = Field(ge=1)


class BriefStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new: int = Field(ge=0)
    superseded: int = Field(ge=0)
    confidence_changes: int = Field(ge=0)
    stale_flagged: int = Field(ge=0)


class Brief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_id: BriefId
    period: BriefPeriod
    claims_referenced: list[ClaimReference]
    stats: BriefStats
    rendered_md: str
    delivered_to: list[NonEmptyStr]
    created_at: AwareDatetime
