"""Reproducible weekly brief schema."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.common import AwareDatetime, BriefId, ClaimId, NonEmptyStr
from schemas.strategy import StrategyCardId, StrategySessionId


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
    # A brief written by the strategy council names its session and the cards
    # that survived the Challenger.  Analyst-era briefs leave both unset.
    strategy_session_id: StrategySessionId | None = None
    strategy_card_ids: list[StrategyCardId] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def strategy_links_are_consistent(self) -> "Brief":
        if self.strategy_card_ids and self.strategy_session_id is None:
            raise ValueError("strategy card IDs require the owning strategy_session_id")
        if len(self.strategy_card_ids) != len(set(self.strategy_card_ids)):
            raise ValueError("strategy_card_ids must not contain duplicates")
        pinned = {reference.claim_id for reference in self.claims_referenced}
        if len(pinned) != len(self.claims_referenced):
            raise ValueError("claims_referenced must pin each claim once")
        return self
