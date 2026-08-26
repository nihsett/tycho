"""Synthetic strategy fixtures and a scripted, offline council.

Everything here is invented.  It exists so the whole workflow - context,
validation, challenge gating, citations, leases, write-once persistence - can be
exercised end to end in a disposable local store with no provider call and no
contact with production data.

Nothing in this module is importable from a production code path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field as dataclasses_field
from datetime import UTC, datetime, timedelta
from typing import Any

from google.adk.agents import LlmAgent

from schemas.claim import Claim, ClaimClass, ClaimStatus, Confidence, Evidence, Severity
from schemas.delta import (
    CANONICAL_GENERATED_BY,
    CANONICAL_PROMPT_VERSION,
    Change,
    ChangeCategory,
    ChangeScope,
    Delta,
    DeltaSchemaVersion,
    DiffKind,
    EvidenceQuote,
    Triage,
)
from strategy_agent.agents import BRIEF_WRITER_NAME, CHALLENGER_NAME, STRATEGIST_NAME
from strategy_agent.invoker import AgentInvocation

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

SYNTHETIC_NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def synthetic_id(prefix: str, seed: int) -> str:
    """A deterministic, schema-valid ULID-shaped ID for fixtures."""
    value = (seed * 0x9E3779B97F4A7C15) & ((1 << 130) - 1)
    encoded = []
    for _ in range(26):
        encoded.append(_CROCKFORD[value & 31])
        value >>= 5
    encoded.reverse()
    encoded[0] = _CROCKFORD[seed % 8]
    return f"{prefix}_{''.join(encoded)}"


def comparison_id(seed: str) -> str:
    return f"sha256:{hashlib.sha256(seed.encode()).hexdigest()}"


def make_delta(
    *,
    seed: int,
    entity: str,
    source: str,
    computed_at: datetime,
    statement: str,
    quote: str,
    category: ChangeCategory = ChangeCategory.CAPABILITY,
    scope: ChangeScope = ChangeScope.PRODUCT_CAPABILITIES,
) -> Delta:
    """One canonical Gemini delta@2 row with a single grounded change."""
    obs_before = synthetic_id("obs", seed * 2)
    obs_after = synthetic_id("obs", seed * 2 + 1)
    return Delta(
        schema_version=DeltaSchemaVersion.V2,
        delta_id=synthetic_id("dlt", seed),
        comparison_id=comparison_id(f"{entity}:{source}:{seed}"),
        entity=entity,
        source=source,
        obs_before=obs_before,
        obs_after=obs_after,
        computed_at=computed_at,
        diff_kind=DiffKind.SEMANTIC,
        generated_by=CANONICAL_GENERATED_BY,
        prompt_version=CANONICAL_PROMPT_VERSION,
        changes=[
            Change(
                category=category,
                scope=scope,
                statement=statement,
                after=quote,
                evidence_after=EvidenceQuote(obs_id=obs_after, quote=quote),
            )
        ],
        summary=statement,
        triage=Triage.MEANINGFUL,
        triage_reason="The release introduces a durable user-facing capability.",
        triage_by=CANONICAL_GENERATED_BY,
        routed_to=[scope.value],
    )


def make_claim(
    *,
    seed: int,
    entity: str,
    scope: str,
    statement: str,
    rationale: str,
    deltas: list[Delta],
    created_at: datetime,
    last_verified_at: datetime | None = None,
    severity: Severity = Severity.NOTABLE,
) -> Claim:
    """One active, confirmed fact claim resting on canonical evidence."""
    return Claim(
        claim_id=synthetic_id("clm", seed),
        entity=entity,
        scope=scope,
        class_=ClaimClass.FACT,
        statement=statement,
        rationale=rationale,
        confidence=Confidence.CONFIRMED,
        severity=severity,
        evidence=[
            Evidence(
                delta_id=delta.delta_id,
                source=delta.source,
                note="Canonical Gemini semantic Delta for this transition.",
            )
            for delta in deltas
        ],
        status=ClaimStatus.ACTIVE,
        version=1,
        created_at=created_at,
        last_verified_at=last_verified_at or created_at,
        created_by="gemini-analyst@1",
    )


@dataclass(frozen=True)
class SyntheticMarket:
    """A tiny, fully synthetic market with one defensible cross-entity pattern."""

    deltas: list[Delta]
    claims: list[Claim]

    @property
    def claim_by_key(self) -> dict[str, Claim]:
        return {claim.claim_id: claim for claim in self.claims}


def build_synthetic_market(now: datetime = SYNTHETIC_NOW) -> SyntheticMarket:
    """Two vendors shipping comparable sandboxing work, plus mirrored noise.

    The mirrored pair exists on purpose: one vendor republishing the same
    release text through two channels must not read as two witnesses.
    """
    recent = now - timedelta(days=2)
    older = now - timedelta(days=4)
    mirrored_quote = "Sandboxed shell execution is enabled by default for all workspaces."

    claude_sandbox = make_delta(
        seed=101,
        entity="claude_code",
        source="github_releases",
        computed_at=recent,
        statement="Claude Code enables sandboxed shell execution by default.",
        quote=mirrored_quote,
    )
    # The same official text, republished on the vendor's own changelog.
    claude_mirror = make_delta(
        seed=102,
        entity="claude_code",
        source="website_changelog",
        computed_at=recent,
        statement="Claude Code documents sandboxed shell execution as the default.",
        quote=mirrored_quote,
    )
    codex_sandbox = make_delta(
        seed=103,
        entity="codex",
        source="website_changelog",
        computed_at=older,
        statement="Codex added a per-workspace execution sandbox with an opt-out.",
        quote="Codex now runs workspace commands inside a per-workspace sandbox.",
    )
    gemini_pricing = make_delta(
        seed=104,
        entity="gemini_cli",
        source="github_releases",
        computed_at=older,
        statement="Gemini CLI published a per-seat usage tier for team workspaces.",
        quote="Team workspaces move to a per-seat usage tier.",
        category=ChangeCategory.PRICING,
        scope=ChangeScope.PRICING,
    )

    deltas = [claude_sandbox, claude_mirror, codex_sandbox, gemini_pricing]
    claims = [
        make_claim(
            seed=201,
            entity="claude_code",
            scope="product/capabilities",
            statement="Claude Code ships sandboxed shell execution as the default on 2026-08-24.",
            rationale="Default-on isolation changes what a team must review before adoption.",
            deltas=[claude_sandbox],
            created_at=recent,
        ),
        make_claim(
            seed=202,
            entity="claude_code",
            scope="product/capabilities",
            statement="Claude Code's changelog documents default sandboxed execution on 2026-08-24.",
            rationale="The documented default is what an evaluator will read first.",
            deltas=[claude_mirror],
            created_at=recent,
        ),
        make_claim(
            seed=203,
            entity="codex",
            scope="product/capabilities",
            statement="Codex ships a per-workspace execution sandbox with an opt-out on 2026-08-22.",
            rationale="Comparable isolation controls change how the two tools are evaluated.",
            deltas=[codex_sandbox],
            created_at=older,
        ),
        make_claim(
            seed=204,
            entity="gemini_cli",
            scope="pricing",
            statement="Gemini CLI moved team workspaces to a per-seat usage tier on 2026-08-22.",
            rationale="A per-seat tier changes the cost model a buyer compares.",
            deltas=[gemini_pricing],
            created_at=older,
            severity=Severity.CRITICAL,
        ),
    ]
    return SyntheticMarket(deltas=deltas, claims=claims)


def seed_market(store: Any, market: SyntheticMarket) -> None:
    """Load the synthetic market into a disposable store without publishing."""
    for delta in market.deltas:
        store.insert_delta(delta, enqueue=False)
    for claim in market.claims:
        store.create_claim(claim)


@dataclass
class ScriptedInvoker:
    """A deterministic stand-in for the three agents.

    It returns pre-written structured payloads keyed by agent name, so a session
    exercises every Python gate without a model call.  Challenger responses are
    consumed in order, one per surviving card.
    """

    strategist: dict[str, Any]
    challenger: list[dict[str, Any]]
    brief_writer: dict[str, Any] | None = None
    calls: list[str] = dataclasses_field(default_factory=list)
    requests: dict[str, list[str]] = dataclasses_field(default_factory=dict)

    def __post_init__(self) -> None:
        self._challenges = list(self.challenger)

    async def invoke(self, agent: LlmAgent, request: str, *, run_id: str) -> AgentInvocation:
        self.calls.append(agent.name)
        self.requests.setdefault(agent.name, []).append(request)
        if agent.name == STRATEGIST_NAME:
            payload = self.strategist
        elif agent.name == CHALLENGER_NAME:
            if not self._challenges:
                raise AssertionError("scripted invoker ran out of challenge responses")
            payload = self._challenges.pop(0)
        elif agent.name == BRIEF_WRITER_NAME:
            if self.brief_writer is None:
                raise AssertionError("scripted invoker has no brief-writer response")
            payload = self.brief_writer
        else:
            raise AssertionError(f"unexpected agent: {agent.name}")
        return AgentInvocation(
            payload=payload,
            run_id=run_id,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            latency_ms=1,
            model="scripted-offline",
        )


def scripted_session(market: SyntheticMarket) -> ScriptedInvoker:
    """The synthetic end-to-end script: one accepted and one rejected conclusion.

    The accepted card spans two vendors and two source families.  The rejected
    one leans on one vendor's mirrored release text and asserts causation, so
    deterministic validation rejects it before the Challenger is even consulted.
    """
    claude_primary, claude_mirror, codex, _pricing = market.claims
    accepted = {
        "statement": (
            "Both Claude Code and Codex now ship workspace execution isolation as a "
            "standard control rather than an advanced option."
        ),
        "rationale": (
            "Two independent vendors converged on comparable isolation defaults "
            "within one week, which changes what a buyer treats as table stakes "
            "when evaluating a coding agent."
        ),
        "confidence": "likely",
        "competing_explanation": (
            "Both teams may be responding to the same enterprise procurement "
            "checklist rather than to each other, in which case the convergence "
            "says more about buyers than about product direction."
        ),
        "falsifier": (
            "A subsequent release from either vendor that returns execution "
            "isolation to an opt-in advanced setting."
        ),
        "premises": [
            {"claim_id": claude_primary.claim_id, "claim_version": 1},
            {"claim_id": codex.claim_id, "claim_version": 1},
        ],
        "limitations": [],
    }
    rejected = {
        "statement": (
            "Claude Code hardened its execution model because rivals were "
            "outpacing it on enterprise security."
        ),
        "rationale": (
            "The release notes and the changelog both describe the same default "
            "sandbox, which shows the change was driven by competitive pressure."
        ),
        "confidence": "likely",
        "competing_explanation": "The team may simply have finished planned work.",
        "falsifier": "A later release that reverts the default.",
        "premises": [
            {"claim_id": claude_primary.claim_id, "claim_version": 1},
            {"claim_id": claude_mirror.claim_id, "claim_version": 1},
        ],
        "limitations": [],
    }
    brief = {
        "what_changed": (
            "Claude Code made sandboxed shell execution the default "
            f'<claim id="{claude_primary.claim_id}" version="1"/> and Codex shipped a '
            f'per-workspace execution sandbox <claim id="{codex.claim_id}" version="1"/>.'
        ),
        "what_tycho_concludes": (
            "Workspace execution isolation is now a standard control across both "
            "monitored vendors rather than an advanced option (likely) "
            f'<claim id="{claude_primary.claim_id}" version="1"/>'
            f'<claim id="{codex.claim_id}" version="1"/>.'
        ),
        "counter_signals": (
            "Both vendors may be answering the same procurement checklist rather "
            "than each other. Tycho rejected a second conclusion this period that "
            "rested on one vendor's mirrored release text."
        ),
        "what_would_change_our_mind": (
            "A subsequent release from either vendor returning execution "
            "isolation to an opt-in advanced setting."
        ),
    }
    return ScriptedInvoker(
        strategist={"cards": [accepted, rejected]},
        challenger=[{"card_id": "unused", "verdict": "pass"}],
        brief_writer=brief,
    )
