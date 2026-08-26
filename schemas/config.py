"""Validated repository configuration loaded from tycho.yaml."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from schemas.common import NonEmptyStr
from schemas.delta import ONTOLOGY_BRANCHES

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SOURCE_NAMES = {
    "github_releases",
    "website_changelog",
    "website_pricing",
    "blog_rss",
    "x_account",
}


class GithubReleasesSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: NonEmptyStr

    @field_validator("repo")
    @classmethod
    def repository_name(cls, value: str) -> str:
        if not _REPOSITORY.fullmatch(value):
            raise ValueError("repo must be an owner/name GitHub repository")
        return value


class WebsitePricingSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: HttpUrl
    extract_hint: NonEmptyStr


class BlogRssSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: HttpUrl


class XAccountSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: NonEmptyStr
    optional: bool = False


class EntitySources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_releases: GithubReleasesSource | None = None
    website_changelog: WebsitePricingSource | None = None
    website_pricing: WebsitePricingSource | None = None
    blog_rss: BlogRssSource | None = None
    x_account: XAccountSource | None = None

    @model_validator(mode="after")
    def at_least_one_source(self) -> "EntitySources":
        if not any(getattr(self, name) is not None for name in _SOURCE_NAMES):
            raise ValueError("entity must configure at least one source")
        return self


class EntityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NonEmptyStr
    description: NonEmptyStr
    aliases: list[NonEmptyStr]
    sources: EntitySources


class TychoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entities: dict[NonEmptyStr, EntityConfig] = Field(min_length=1, max_length=4)
    ontology: list[NonEmptyStr]
    staleness_days: dict[NonEmptyStr, int]
    schedules: dict[NonEmptyStr, NonEmptyStr]

    @field_validator("ontology")
    @classmethod
    def fixed_ontology(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("ontology branches must be unique")
        unknown = set(value) - ONTOLOGY_BRANCHES
        if unknown:
            raise ValueError(f"unknown ontology branches: {sorted(unknown)}")
        if set(value) != ONTOLOGY_BRANCHES:
            raise ValueError("v1 config must include every fixed ontology branch")
        return value

    @field_validator("staleness_days")
    @classmethod
    def valid_staleness(cls, value: dict[str, int]) -> dict[str, int]:
        unknown = set(value) - (ONTOLOGY_BRANCHES | {"default"})
        if unknown:
            raise ValueError(f"unknown staleness scopes: {sorted(unknown)}")
        if "default" not in value or any(days <= 0 for days in value.values()):
            raise ValueError("staleness_days needs a positive default and positive values")
        return value

    @field_validator("schedules")
    @classmethod
    def valid_schedules(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - _SOURCE_NAMES
        if unknown:
            raise ValueError(f"unknown scheduled source types: {sorted(unknown)}")
        for source, expression in value.items():
            if len(expression.split()) != 5:
                raise ValueError(f"{source} schedule must be a five-field cron expression")
        return value


def load_config(path: str | Path = "tycho.yaml") -> TychoConfig:
    with Path(path).open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return TychoConfig.model_validate(raw)
