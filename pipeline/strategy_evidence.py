"""Deterministic evidence rules for strategy cards.

The model may propose; this module decides.  Nothing here calls a model, and
nothing here trusts a model-supplied identifier, count, or metric: premise Delta
IDs, entities, scopes, and source families are all recomputed from the store.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from schemas.claim import Claim, ClaimClass, ClaimStatus, Confidence
from schemas.config import TychoConfig
from schemas.delta import (
    CANONICAL_GENERATED_BY,
    CANONICAL_PROMPT_VERSION,
    ChangeCategory,
    Delta,
    DeltaSchemaVersion,
)
from schemas.strategy import (
    MAX_PREMISES_PER_CARD,
    MIN_ENTITIES_PER_CARD,
    MIN_PREMISES_PER_CARD,
    MIN_SOURCE_FAMILIES_PER_CARD,
    CardLimitation,
    LimitationKind,
    StrategyCardDraft,
    StrategyConfidence,
)


class StrategyEvidenceStore(Protocol):
    """The read surface a strategy session is allowed to touch."""

    def get_claim(self, claim_id: str) -> Claim | None: ...

    def get_delta(self, delta_id: str) -> Delta | None: ...


# --- Source families -------------------------------------------------------
#
# A vendor that publishes the same release text to GitHub and to a mirrored
# changelog has spoken once, not twice.  Folding those channels into a single
# family is what stops duplicated official text from looking like independent
# corroboration.

_RELEASE_CHANNELS = frozenset({"github_releases", "website_changelog", "blog_rss"})
_FAMILY_BY_SOURCE = {
    "github_releases": "official_release",
    "website_changelog": "official_release",
    "blog_rss": "official_release",
    "website_pricing": "official_pricing",
    "x_account": "official_social",
}


def source_family(entity: str, source: str) -> str:
    """Fold one entity's mirrored publication channels into one family name."""
    family = _FAMILY_BY_SOURCE.get(source)
    if family is None:
        # Anything Tycho did not configure as the entity's own channel is an
        # independent observer of that entity and keeps its own identity.
        return f"{entity}/independent:{source}"
    return f"{entity}/{family}"


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def evidence_fingerprint(delta: Delta) -> str:
    """Hash a Delta's grounded quotes so republished text collapses to one unit."""
    quotes = sorted(
        _normalize(quote.quote)
        for change in delta.changes
        for quote in (change.evidence_before, change.evidence_after)
        if quote is not None
    )
    if not quotes:
        # Noise Deltas carry no quotes; their triage reason is the only text and
        # is not corroboration, so give them a per-Delta identity.
        return f"noise:{delta.delta_id}"
    return hashlib.sha256("\n".join(quotes).encode("utf-8")).hexdigest()


def corroboration_units(deltas: list[Delta]) -> set[str]:
    """Collapse mirrored evidence into the smallest honest set of witnesses.

    Two Deltas are the same witness when they come from one entity's own
    publication family, or when their grounded quotes are textually identical
    after normalization even across families.
    """
    fingerprint_to_family: dict[str, str] = {}
    units: set[str] = set()
    for delta in sorted(deltas, key=lambda item: item.delta_id):
        family = source_family(delta.entity, delta.source)
        fingerprint = evidence_fingerprint(delta)
        mirrored = fingerprint_to_family.get(fingerprint)
        if mirrored is not None:
            units.add(mirrored)
            continue
        fingerprint_to_family[fingerprint] = family
        units.add(family)
    return units


# --- Conclusion language policy -------------------------------------------
#
# V1 has release-channel evidence and nothing else.  Removal can be evidenced
# by a deprecation change; motive, causation, ranking, and the future cannot be
# evidenced at all, so those words are rejected outright rather than argued
# about in a prompt.

_LANGUAGE_POLICIES: dict[str, re.Pattern[str]] = {
    "removal": re.compile(
        r"\b(?:removed?|removing|dropped?|dropping|discontinued?|deprecat\w*|"
        r"eliminated?|sunset\w*|retired?|no longer (?:offers?|supports?|ships?))\b",
        re.IGNORECASE,
    ),
    "causation": re.compile(
        r"\b(?:because(?: of)?|caused by|causing|due to|as a result of|"
        r"driven by|drove|led to|leading to|resulted? in|resulting from|"
        r"in response to|triggered by)\b",
        re.IGNORECASE,
    ),
    "intent": re.compile(
        r"\b(?:plans? to|planning to|intends? to|intent(?:ion)?|aims? to|"
        r"aiming to|seeks? to|seeking to|wants? to|hopes? to|strategy is|"
        r"deliberately|betting on|in order to|so that it can|motivated by)\b",
        re.IGNORECASE,
    ),
    "leadership": re.compile(
        r"\b(?:market\s+lead\w*|lead(?:s|ing)?\s+the\s+(?:market|category|field|pack)|"
        r"leader in|leading the|dominant\w*|dominates?|"
        r"ahead of|outpac\w+|outperform\w*|best[- ]in[- ]class|"
        r"winning|winner|#1|number one|first place|beats?|beating)\b",
        re.IGNORECASE,
    ),
    "future_action": re.compile(
        r"\b(?:will\s+(?:\w+)|going to|about to|expected to|expects? to|"
        r"forecasts?|predicts?|poised to|set to|next quarter|next year|"
        r"upcoming|soon\b|roadmap indicates|by 20\d\d)\b",
        re.IGNORECASE,
    ),
}


def language_policy_hits(text: str) -> set[str]:
    """Return which prohibited conclusion families the text asserts."""
    return {name for name, pattern in _LANGUAGE_POLICIES.items() if pattern.search(text)}


def _removal_is_evidenced(deltas: list[Delta]) -> bool:
    return any(
        change.category is ChangeCategory.DEPRECATION
        for delta in deltas
        for change in delta.changes
    )


# --- Staleness and confidence ceilings -------------------------------------

_CONFIDENCE_RANK = {
    Confidence.SPECULATIVE: 0,
    Confidence.LIKELY: 1,
    Confidence.CONFIRMED: 2,
}
_STRATEGY_RANK = {
    StrategyConfidence.SPECULATIVE: 0,
    StrategyConfidence.LIKELY: 1,
}


def staleness_threshold(config: TychoConfig, scope: str) -> int:
    """Resolve the tycho.yaml staleness budget for one ontology scope."""
    branch = scope.split("/")[0] if scope.startswith("sources/") else scope
    return int(config.staleness_days.get(scope, config.staleness_days.get(branch, config.staleness_days["default"])))


def claim_is_stale(claim: Claim, config: TychoConfig, now: datetime) -> bool:
    threshold = staleness_threshold(config, claim.scope)
    return now - claim.last_verified_at > timedelta(days=threshold)


def confidence_ceiling(claims: list[Claim]) -> StrategyConfidence:
    """A conclusion is never stronger than its weakest required premise."""
    if not claims:
        return StrategyConfidence.SPECULATIVE
    weakest = min(_CONFIDENCE_RANK[claim.confidence] for claim in claims)
    # ``confirmed`` premises still only license ``likely`` synthesis: strategy
    # cards are inference and are never confirmed.
    return StrategyConfidence.LIKELY if weakest >= _CONFIDENCE_RANK[Confidence.LIKELY] else StrategyConfidence.SPECULATIVE


# --- Resolution ------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedPremise:
    claim: Claim
    claim_version: int
    deltas: list[Delta]

    @property
    def delta_ids(self) -> list[str]:
        return [delta.delta_id for delta in self.deltas]


@dataclass
class CardValidation:
    """The deterministic verdict on one drafted card."""

    violations: list[str] = field(default_factory=list)
    premises: list[ResolvedPremise] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    source_families: list[str] = field(default_factory=list)
    limitations: list[CardLimitation] = field(default_factory=list)
    confidence: StrategyConfidence = StrategyConfidence.SPECULATIVE
    stale_claim_ids: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations


def _resolve_premise(
    store: StrategyEvidenceStore,
    claim_id: str,
    claim_version: int,
    violations: list[str],
) -> ResolvedPremise | None:
    """Rule 1 and rule 2: the claim must exist, be active, match the exact
    version, and rest only on canonical Gemini delta@2 rows."""
    claim = store.get_claim(claim_id)
    if claim is None:
        violations.append(f"unknown premise claim {claim_id}")
        return None
    if claim.status is not ClaimStatus.ACTIVE:
        violations.append(f"premise claim {claim_id} is {claim.status.value}, not active")
        return None
    if claim.version != claim_version:
        violations.append(
            f"premise claim {claim_id} is at version {claim.version}, not {claim_version}"
        )
        return None
    if claim.class_ is ClaimClass.OPERATIONAL:
        violations.append(f"premise claim {claim_id} is operational, not market evidence")
        return None

    deltas: list[Delta] = []
    for item in claim.evidence:
        delta = store.get_delta(item.delta_id)
        if delta is None:
            violations.append(
                f"premise claim {claim_id} cites unresolvable evidence {item.delta_id}"
            )
            return None
        if (
            delta.schema_version is not DeltaSchemaVersion.V2
            or delta.generated_by != CANONICAL_GENERATED_BY
            or delta.prompt_version != CANONICAL_PROMPT_VERSION
        ):
            violations.append(
                f"premise claim {claim_id} cites noncanonical evidence {item.delta_id}"
            )
            return None
        deltas.append(delta)
    if not deltas:
        violations.append(f"premise claim {claim_id} has no resolvable evidence")
        return None
    return ResolvedPremise(claim=claim, claim_version=claim_version, deltas=deltas)


def validate_card_draft(
    draft: StrategyCardDraft,
    store: StrategyEvidenceStore,
    config: TychoConfig,
    now: datetime,
) -> CardValidation:
    """Run every hard evidence rule over one Strategist draft.

    The returned validation carries recomputed premises, entities, scopes, and
    source families, so the caller never has to take the model's word for any
    of them.
    """
    result = CardValidation()
    violations = result.violations

    if not MIN_PREMISES_PER_CARD <= len(draft.premises) <= MAX_PREMISES_PER_CARD:
        violations.append(
            f"a card needs {MIN_PREMISES_PER_CARD}-{MAX_PREMISES_PER_CARD} premises; "
            f"got {len(draft.premises)}"
        )

    for premise in draft.premises:
        resolved = _resolve_premise(store, premise.claim_id, premise.claim_version, violations)
        if resolved is not None:
            result.premises.append(resolved)

    claims = [premise.claim for premise in result.premises]
    deltas = [delta for premise in result.premises for delta in premise.deltas]

    result.entities = sorted({claim.entity for claim in claims})
    result.scopes = sorted({claim.scope for claim in claims})
    result.source_families = sorted(corroboration_units(deltas))

    # Rule 3: a market conclusion needs more than one company in it.
    if len(result.entities) < MIN_ENTITIES_PER_CARD:
        violations.append(
            f"a cross-entity conclusion needs {MIN_ENTITIES_PER_CARD} distinct entities; "
            f"got {len(result.entities)}"
        )
    # Rules 4 and 5: mirrored official text is one witness, not two.
    if len(result.source_families) < MIN_SOURCE_FAMILIES_PER_CARD:
        violations.append(
            f"a conclusion needs {MIN_SOURCE_FAMILIES_PER_CARD} independent source families; "
            f"got {len(result.source_families)} after mirrored-evidence normalization"
        )

    # Rule 7: prohibited conclusion language without evidence that supports it.
    conclusion_text = f"{draft.statement} {draft.rationale}"
    hits = language_policy_hits(conclusion_text)
    for name in sorted(hits):
        if name == "removal" and _removal_is_evidenced(deltas):
            continue
        violations.append(f"conclusion asserts unsupported {name}")

    # Rule 6: stale premises must be labelled, and force speculative.
    labelled = {
        item.claim_id
        for item in draft.limitations
        if item.kind is LimitationKind.STALE_PREMISE
    }
    for claim in claims:
        if not claim_is_stale(claim, config, now):
            continue
        result.stale_claim_ids.append(claim.claim_id)
        if claim.claim_id not in labelled:
            violations.append(
                f"premise claim {claim.claim_id} is stale and is not labelled as a limitation"
            )
    result.limitations = [
        item for item in draft.limitations if item.claim_id in set(result.stale_claim_ids)
    ]
    for item in draft.limitations:
        if item.claim_id not in set(result.stale_claim_ids):
            violations.append(
                f"limitation names {item.claim_id}, which is not a stale premise"
            )

    # Rule 8: confidence cannot exceed the weakest premise and stale evidence
    # forces speculative.
    ceiling = confidence_ceiling(claims)
    if result.stale_claim_ids:
        ceiling = StrategyConfidence.SPECULATIVE
    result.confidence = ceiling
    if _STRATEGY_RANK[draft.confidence] > _STRATEGY_RANK[ceiling]:
        violations.append(
            f"confidence {draft.confidence.value} exceeds the evidence ceiling {ceiling.value}"
        )

    return result
