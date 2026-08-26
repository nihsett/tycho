"""Versioned Delta contracts, including the grounded semantic Delta v2."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from schemas.common import AwareDatetime, DeltaId, NonEmptyStr, ObservationId

CANONICAL_GENERATED_BY = "gemini-3.7-flash@semantic-differ-1"
CANONICAL_PROMPT_VERSION = "semantic-delta@2"

ONTOLOGY_BRANCHES = frozenset(
    {
        "identity",
        "product/capabilities",
        "product/roadmap",
        "pricing",
        "gtm",
        "team",
        "traction",
        "sources",
    }
)

MAX_STATEMENT_LENGTH = 1_000
MAX_SUMMARY_LENGTH = 2_000
MAX_REASON_LENGTH = 2_000
MAX_QUOTE_LENGTH = 4_000
MAX_SEMANTIC_VALUE_LENGTH = 2_000
MAX_GENERATOR_LENGTH = 200
MAX_PROMPT_VERSION_LENGTH = 100

BoundedStatement = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MAX_STATEMENT_LENGTH
    ),
]
BoundedSummary = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SUMMARY_LENGTH),
]
BoundedReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REASON_LENGTH),
]
BoundedGenerator = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_GENERATOR_LENGTH),
]
BoundedPromptVersion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MAX_PROMPT_VERSION_LENGTH
    ),
]
ComparisonId = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]


class DeltaSchemaVersion(StrEnum):
    V1 = "delta@1"
    V2 = "delta@2"


class DiffKind(StrEnum):
    STRUCTURED = "structured"
    TEXT = "text"
    VISUAL = "visual"
    SEMANTIC = "semantic"


class Triage(StrEnum):
    MEANINGFUL = "meaningful"
    NOISE = "noise"
    PENDING = "pending"


class ChangeCategory(StrEnum):
    CAPABILITY = "capability"
    DEPRECATION = "deprecation"
    PRICING = "pricing"
    POLICY = "policy"
    INTEGRATION = "integration"
    AVAILABILITY = "availability"
    POSITIONING = "positioning"
    RELIABILITY = "reliability"
    OTHER = "other"


class ChangeScope(StrEnum):
    IDENTITY = "identity"
    PRODUCT_CAPABILITIES = "product/capabilities"
    PRODUCT_ROADMAP = "product/roadmap"
    PRICING = "pricing"
    GTM = "gtm"
    TEAM = "team"
    TRACTION = "traction"
    SOURCES = "sources"


class EvidenceQuote(BaseModel):
    """A quote tied to one immutable observation."""

    model_config = ConfigDict(extra="forbid")

    obs_id: ObservationId
    quote: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUOTE_LENGTH),
    ]


class Change(BaseModel):
    """A legacy path change or a semantic, source-grounded change."""

    model_config = ConfigDict(extra="forbid")

    # ``path`` remains for delta@1 compatibility only.  Semantic changes do not
    # manufacture a path merely to satisfy the old mechanical schema.
    path: NonEmptyStr | None = None
    before: Any = None
    after: Any = None
    category: ChangeCategory | None = None
    scope: ChangeScope | None = None
    statement: BoundedStatement | None = None
    evidence_before: EvidenceQuote | None = None
    evidence_after: EvidenceQuote | None = None


_VERSION_PUBLICATION_ONLY = re.compile(
    r"^\s*(?:[A-Za-z0-9][\w .'-]{0,80}\s+)?"
    r"(?:published|released|ships?|shipped|tagged|version(?:\s+release)?|"
    r"release)\s+(?:a\s+)?(?:new\s+)?(?:version\s+|release\s+|tag\s+)?"
    r"v?\d+(?:\.\d+){1,3}(?:[-+][\w.-]+)?"
    r"(?:\s+(?:on|published|released)\s+[^.!?]+)?[.!?]?\s*$",
    re.IGNORECASE,
)


def is_version_publication_only(statement: str) -> bool:
    """Conservatively reject a release diary entry with no product fact."""
    return bool(_VERSION_PUBLICATION_ONLY.fullmatch(statement.strip()))


def _duplicate_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _semantic_fields(change: Change) -> tuple[Any, ...]:
    return (
        change.category,
        change.scope,
        change.statement,
        change.evidence_before,
        change.evidence_after,
    )


class Delta(BaseModel):
    """Canonical Delta model with transparent loading of legacy delta@1 rows."""

    model_config = ConfigDict(extra="forbid")

    schema_version: DeltaSchemaVersion = DeltaSchemaVersion.V1
    delta_id: DeltaId
    comparison_id: ComparisonId | None = None
    entity: NonEmptyStr
    source: NonEmptyStr
    obs_before: ObservationId
    obs_after: ObservationId
    computed_at: AwareDatetime
    diff_kind: DiffKind
    generated_by: BoundedGenerator | None = None
    prompt_version: BoundedPromptVersion | None = None
    changes: list[Change] = Field(default_factory=list)
    summary: BoundedSummary
    triage: Triage
    triage_reason: BoundedReason | None = None
    triage_by: BoundedGenerator
    routed_to: list[NonEmptyStr] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def default_missing_schema_version(cls, value: Any) -> Any:
        if isinstance(value, dict) and not value.get("schema_version"):
            value = dict(value)
            value["schema_version"] = DeltaSchemaVersion.V1
        return value

    @model_validator(mode="after")
    def validate_version_contract(self) -> "Delta":
        scopes = [str(scope) for scope in self.routed_to]
        unknown = set(scopes) - ONTOLOGY_BRANCHES
        if unknown:
            raise ValueError(f"unknown ontology branches: {sorted(unknown)}")
        if len(scopes) != len(set(scopes)):
            raise ValueError("routed_to must not contain duplicates")

        if self.schema_version is DeltaSchemaVersion.V1:
            if not self.changes:
                raise ValueError("delta@1 requires at least one legacy change")
            if not scopes:
                raise ValueError("delta@1 requires at least one routed scope")
            if self.diff_kind is DiffKind.SEMANTIC:
                raise ValueError("delta@1 cannot use semantic diff_kind")
            for change in self.changes:
                if change.path is None:
                    raise ValueError("delta@1 changes require path")
                if any(field is not None for field in _semantic_fields(change)):
                    raise ValueError("delta@1 changes cannot contain semantic fields")
            return self

        if self.schema_version is not DeltaSchemaVersion.V2:
            raise ValueError(f"unsupported schema version: {self.schema_version}")
        if self.diff_kind is not DiffKind.SEMANTIC:
            raise ValueError("delta@2 requires diff_kind=semantic")
        if not self.comparison_id:
            raise ValueError("delta@2 requires comparison_id")
        if self.generated_by != CANONICAL_GENERATED_BY:
            raise ValueError(
                "delta@2 generated_by must be " + CANONICAL_GENERATED_BY
            )
        if self.prompt_version != CANONICAL_PROMPT_VERSION:
            raise ValueError(
                "delta@2 prompt_version must be " + CANONICAL_PROMPT_VERSION
            )
        if self.triage_by != self.generated_by:
            raise ValueError("delta@2 triage_by must equal generated_by")
        if not self.triage_reason:
            raise ValueError("delta@2 requires triage_reason")

        if self.triage is Triage.MEANINGFUL:
            if not 1 <= len(self.changes) <= 8:
                raise ValueError("meaningful delta@2 requires 1-8 changes")
            if not scopes:
                raise ValueError("meaningful delta@2 requires a routed scope")
            derived_scopes: list[str] = []
            for change in self.changes:
                if change.path is not None:
                    raise ValueError("delta@2 changes must not use path")
                if change.category is None or change.scope is None:
                    raise ValueError("semantic changes require category and scope")
                if change.statement is None:
                    raise ValueError("semantic changes require statement")
                for field_name, value in (("before", change.before), ("after", change.after)):
                    if value is not None and not isinstance(value, str):
                        raise ValueError(f"semantic change {field_name} must be a string or null")
                    if isinstance(value, str) and len(value) > MAX_SEMANTIC_VALUE_LENGTH:
                        raise ValueError(
                            f"semantic change {field_name} exceeds the length limit"
                        )
                if is_version_publication_only(change.statement):
                    raise ValueError("version-publication-only statements are not allowed")
                if change.evidence_after is None:
                    raise ValueError("meaningful changes require evidence_after")
                if change.evidence_after.obs_id != self.obs_after:
                    raise ValueError("evidence_after.obs_id must equal obs_after")
                if change.before is not None and change.evidence_before is None:
                    raise ValueError(
                        "asserted before state requires evidence_before"
                    )
                if change.evidence_before is not None:
                    if change.evidence_before.obs_id != self.obs_before:
                        raise ValueError("evidence_before.obs_id must equal obs_before")
                scope = change.scope.value
                if scope not in derived_scopes:
                    derived_scopes.append(scope)
            if set(scopes) != set(derived_scopes):
                raise ValueError("routed_to must be the union of accepted change scopes")
            statements = [
                _duplicate_key(change.statement)
                for change in self.changes
                if change.statement
            ]
            if len(statements) != len(set(statements)):
                raise ValueError("semantic change statements must be unique")
            quotes = [
                _duplicate_key(quote.quote)
                for change in self.changes
                for quote in (change.evidence_before, change.evidence_after)
                if quote is not None
            ]
            if len(quotes) != len(set(quotes)):
                raise ValueError("semantic evidence quotes must be unique")
        elif self.triage is Triage.NOISE:
            if self.changes:
                raise ValueError("noise delta@2 must contain zero changes")
            if scopes:
                raise ValueError("noise delta@2 must contain zero routed scopes")
        else:
            raise ValueError("delta@2 triage must be meaningful or noise")
        return self