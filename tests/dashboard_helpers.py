"""Synthetic fixtures for the dashboard tests.

Everything here is invented and lives in memory.  No dashboard test opens a
Google Cloud client, reads production, or calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from schemas.brief import Brief, BriefPeriod, BriefStats, ClaimReference
from schemas.claim import (
    Claim,
    ClaimClass,
    ClaimStatus,
    Confidence,
    Evidence,
    InferenceKind,
    Severity,
)
from schemas.config import TychoConfig, load_config
from schemas.delta import Delta, Triage
from schemas.observation import Observation, ObservationKind, ObservationStatus
from schemas.strategy import (
    STRATEGY_QUESTION,
    AgentVersions,
    CardStatus,
    ChallengeResult,
    ChallengeVerdict,
    ManifestEntry,
    ModelVersions,
    PremiseReference,
    SessionMetrics,
    SessionPeriod,
    SessionState,
    StrategyCard,
    StrategyConfidence,
    StrategySession,
    manifest_hash,
)
from strategy_agent.synthetic import make_claim, make_delta, synthetic_id

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)

#: Method names a dashboard read must never reach.  ``RecordingSource`` raises
#: on any of them, so a forbidden call is a test failure, not a code review.
FORBIDDEN_METHODS = frozenset(
    {
        "create_claim",
        "update_claim",
        "insert_delta",
        "insert_observation",
        "publish_delta",
        "bump_verified",
        "put_raw",
        "get_raw",
        "get_audit_delta",
        "list_audit_deltas",
        "find_audit_delta_by_comparison_id",
        "create_strategy_session",
        "finalize_strategy_session",
        "commit_strategy_session",
        "begin_strategy_session",
        "acquire_strategy_lease",
        "create_brief_once",
        "record_alert",
        "create_receipt_once",
    }
)


def config() -> TychoConfig:
    return load_config("tycho.yaml")


def observation_for(obs_id: str, entity: str, source: str, at: datetime) -> Observation:
    return Observation(
        obs_id=obs_id,
        entity=entity,
        source=source,
        kind=ObservationKind.STRUCTURED,
        fetched_at=at,
        content_ref=f"gs://tycho-raw/{entity}/{source}/{obs_id}.json",
        content_hash="sha256:" + "a" * 64,
        adapter_ver="github@1",
        status=ObservationStatus.OK,
    )


@dataclass(frozen=True)
class DashboardMarket:
    """A synthetic market with every lifecycle state the dashboard renders."""

    deltas: list[Delta]
    claims: list[Claim]
    observations: list[Observation]
    sessions: list[StrategySession]
    briefs: list[Brief]
    watchers: list[dict[str, Any]]

    def claim(self, index: int) -> Claim:
        return self.claims[index]


def build_dashboard_market(now: datetime = NOW) -> DashboardMarket:
    """Two lively entities, one entity with only history, one with nothing."""
    recent = now - timedelta(days=2)
    older = now - timedelta(days=5)
    ancient = now - timedelta(days=400)

    claude_release = make_delta(
        seed=301,
        entity="claude_code",
        source="github_releases",
        computed_at=recent,
        statement="Claude Code enables sandboxed shell execution by default.",
        quote="Sandboxed shell execution is enabled by default for all workspaces.",
    )
    codex_release = make_delta(
        seed=302,
        entity="codex",
        source="website_changelog",
        computed_at=older,
        statement="Codex added a per-workspace execution sandbox with an opt-out.",
        quote="Codex now runs workspace commands inside a per-workspace sandbox.",
    )
    codex_pricing_old = make_delta(
        seed=303,
        entity="codex",
        source="website_changelog",
        computed_at=ancient,
        statement="Codex listed its team plan at $30 per seat per month.",
        quote="Team plan: $30 per seat per month.",
    )
    codex_pricing_new = make_delta(
        seed=304,
        entity="codex",
        source="website_changelog",
        computed_at=recent,
        statement="Codex raised its team plan to $45 per seat per month.",
        quote="Team plan: $45 per seat per month.",
    )
    gemini_retired = make_delta(
        seed=305,
        entity="gemini_cli",
        source="github_releases",
        computed_at=ancient,
        statement="Gemini CLI shipped an interactive extension installer.",
        quote="Extensions can now be installed interactively.",
    )
    dispute_delta = make_delta(
        seed=306,
        entity="claude_code",
        source="github_releases",
        computed_at=recent,
        statement="A third-party report claims sandboxing is opt-in on Windows.",
        quote="On Windows the sandbox remains opt-in.",
    )
    noise = make_delta(
        seed=307,
        entity="pi",
        source="github_releases",
        computed_at=recent,
        statement="Pi refreshed its release metadata.",
        quote="Metadata refreshed.",
    ).model_copy(update={"triage": Triage.NOISE, "changes": [], "routed_to": []})

    deltas = [
        claude_release,
        codex_release,
        codex_pricing_old,
        codex_pricing_new,
        gemini_retired,
        dispute_delta,
        noise,
    ]

    claude_active = make_claim(
        seed=401,
        entity="claude_code",
        scope="product/capabilities",
        statement="Claude Code ships sandboxed shell execution as the default on 2026-08-24.",
        rationale="Default-on isolation changes what a team must review before adoption.",
        deltas=[claude_release],
        created_at=recent,
        last_verified_at=now - timedelta(hours=6),
        severity=Severity.CRITICAL,
    )
    codex_active = make_claim(
        seed=402,
        entity="codex",
        scope="product/capabilities",
        statement="Codex ships a per-workspace execution sandbox with an opt-out on 2026-08-21.",
        rationale="Comparable isolation controls change how the two tools are evaluated.",
        deltas=[codex_release],
        created_at=older,
    )
    codex_price_old = make_claim(
        seed=403,
        entity="codex",
        scope="pricing",
        statement="Codex team plan is $30 per seat per month as of 2025-07-22.",
        rationale="The seat price is the number a buyer compares first.",
        deltas=[codex_pricing_old],
        created_at=ancient,
    ).model_copy(
        update={
            "status": ClaimStatus.SUPERSEDED,
            "superseded_by": synthetic_id("clm", 404),
            "last_verified_at": ancient,
        }
    )
    codex_price_new = make_claim(
        seed=404,
        entity="codex",
        scope="pricing",
        statement="Codex team plan is $45 per seat per month (was $30) as of 2026-08-24.",
        rationale="A 50% seat increase is a material change to the cost model.",
        deltas=[codex_pricing_new],
        created_at=recent,
        severity=Severity.CRITICAL,
    ).model_copy(update={"supersedes": synthetic_id("clm", 403)})
    gemini_gone = make_claim(
        seed=405,
        entity="gemini_cli",
        scope="product/capabilities",
        statement="Gemini CLI shipped an interactive extension installer on 2025-07-22.",
        rationale="Installer ergonomics affect first-run adoption.",
        deltas=[gemini_retired],
        created_at=ancient,
    ).model_copy(
        update={
            "status": ClaimStatus.RETIRED,
            "last_verified_at": ancient,
            "history": [
                {
                    "at": (ancient + timedelta(days=1)).isoformat(),
                    "event": "legacy_evidence_migration",
                    "action": "retired",
                    "actor": "legacy-claim-migration@1",
                    "reason": "archived evidence maps to validated canonical noise",
                    "previous_state": {
                        "claim_id": synthetic_id("clm", 405),
                        "version": 1,
                        "status": "active",
                        "evidence_delta_ids": [gemini_retired.delta_id],
                    },
                }
            ],
        }
    )
    dispute = Claim(
        claim_id=synthetic_id("clm", 406),
        entity="claude_code",
        scope="product/capabilities",
        **{"class": ClaimClass.INFERENCE},
        inference_kind=InferenceKind.PRESENT_STATE,
        statement="A third-party report states Claude Code sandboxing stays opt-in on Windows.",
        rationale="A conflicting signal exists; the established claim stays active.",
        confidence=Confidence.SPECULATIVE,
        severity=Severity.CRITICAL,
        evidence=[
            Evidence(
                delta_id=dispute_delta.delta_id,
                source=dispute_delta.source,
                note="The single conflicting third-party signal.",
            )
        ],
        status=ClaimStatus.ACTIVE,
        disputes=claude_active.claim_id,
        version=1,
        created_at=now - timedelta(days=1),
        last_verified_at=now - timedelta(days=1),
        created_by="gemini-analyst@1",
    )

    claims = [
        claude_active,
        codex_active,
        codex_price_old,
        codex_price_new,
        gemini_gone,
        dispute,
    ]

    observations = [
        observation_for(obs_id, delta.entity, delta.source, delta.computed_at)
        for delta in deltas
        for obs_id in (delta.obs_before, delta.obs_after)
    ]

    watchers = [
        {
            "entity": entity,
            "source": source,
            "last_fetched_at": now - timedelta(hours=7),
            "observation_count": 12,
        }
        for entity in ("claude_code", "codex", "gemini_cli", "pi")
        for source in ("github_releases", "website_changelog")
    ]

    session, brief = build_session(now, claude_active, codex_active, codex_price_new)
    return DashboardMarket(
        deltas=deltas,
        claims=claims,
        observations=observations,
        sessions=[session],
        briefs=[brief],
        watchers=watchers,
    )


def build_session(
    now: datetime, passed_a: Claim, passed_b: Claim, rejected_premise: Claim
) -> tuple[StrategySession, Brief]:
    """One completed session with one passed card and one rejected card."""
    period = SessionPeriod(**{"from": now - timedelta(days=7), "to": now})
    manifest = [
        ManifestEntry(
            claim_id=claim.claim_id,
            claim_version=claim.version,
            entity=claim.entity,
            scope=claim.scope,
            confidence=claim.confidence.value,
            severity=claim.severity.value,
            delta_ids=[item.delta_id for item in claim.evidence],
            source_families=[f"{claim.entity}/official_release"],
            last_verified_at=claim.last_verified_at,
            stale=False,
            staleness_days=60,
        )
        for claim in (passed_a, passed_b, rejected_premise)
    ]
    passed = StrategyCard(
        card_id=synthetic_id("stc", 501),
        statement=(
            "Claude Code and Codex both ship workspace execution isolation as a "
            "standard control."
        ),
        rationale=(
            "Two vendors now default to comparable isolation, which changes what "
            "a buyer treats as table stakes."
        ),
        confidence=StrategyConfidence.LIKELY,
        competing_explanation=(
            "Both teams may be answering the same procurement checklist rather "
            "than each other."
        ),
        falsifier="A release from either vendor returning isolation to opt-in.",
        entities=[passed_a.entity, passed_b.entity],
        scopes=["product/capabilities"],
        source_families=[
            f"{passed_a.entity}/official_release",
            f"{passed_b.entity}/official_release",
        ],
        premises=[
            PremiseReference(
                claim_id=passed_a.claim_id,
                claim_version=passed_a.version,
                delta_ids=[item.delta_id for item in passed_a.evidence],
            ),
            PremiseReference(
                claim_id=passed_b.claim_id,
                claim_version=passed_b.version,
                delta_ids=[item.delta_id for item in passed_b.evidence],
            ),
        ],
        status=CardStatus.PASSED,
    )
    rejected = StrategyCard(
        card_id=synthetic_id("stc", 502),
        statement="Codex raised seat pricing because rivals shipped isolation first.",
        rationale="A single vendor's pricing move, argued as a response to rivals.",
        confidence=StrategyConfidence.SPECULATIVE,
        competing_explanation="Ordinary annual repricing.",
        falsifier="A public price rollback.",
        entities=[rejected_premise.entity],
        scopes=["pricing"],
        source_families=[f"{rejected_premise.entity}/official_release"],
        premises=[
            PremiseReference(
                claim_id=rejected_premise.claim_id,
                claim_version=rejected_premise.version,
                delta_ids=[item.delta_id for item in rejected_premise.evidence],
            )
        ],
        status=CardStatus.REJECTED,
        rejection_reasons=[
            "a cross-entity conclusion needs 2 distinct entities; got 1",
            "a conclusion needs 2 independent source families; got 1 after "
            "mirrored-evidence normalization",
            "conclusion asserts unsupported causation",
        ],
    )
    challenge = ChallengeResult(card_id=passed.card_id, verdict=ChallengeVerdict.PASS)
    brief_id = "brf_2026w35-testcard"
    session = StrategySession(
        session_id=synthetic_id("sts", 601),
        question=STRATEGY_QUESTION,
        period=period,
        input_manifest=manifest,
        manifest_hash=manifest_hash(manifest),
        cards=[passed, rejected],
        challenges=[challenge],
        agent_versions=AgentVersions(),
        model_versions=ModelVersions(
            strategist="gemini-3.7-flash",
            challenger="gemini-3.7-flash",
            brief_writer="gemini-3.7-flash",
        ),
        state=SessionState.COMPLETED,
        metrics=SessionMetrics(
            cards_proposed=2,
            cards_passed=1,
            cards_rejected=1,
            input_bytes=5_362,
            estimated_input_tokens=1_341,
            input_tokens=8_047,
            output_tokens=485,
            total_tokens=8_532,
            latency_ms=3_137,
        ),
        run_ids=[f"{synthetic_id('sts', 601)}:strategist"],
        brief_id=brief_id,
        created_at=now - timedelta(minutes=5),
        updated_at=now,
    )
    brief = Brief(
        brief_id=brief_id,
        period=BriefPeriod(**{"from": period.from_, "to": period.to}),
        claims_referenced=[
            ClaimReference(claim_id=passed_a.claim_id, version=passed_a.version),
            ClaimReference(claim_id=passed_b.claim_id, version=passed_b.version),
        ],
        stats=BriefStats(new=2, superseded=1, confidence_changes=0, stale_flagged=0),
        rendered_md=(
            "# Tycho strategy brief — 2026-W35\n\n## What changed\n\n"
            f"Two vendors shipped comparable isolation controls "
            f"[{passed_a.claim_id}@v1](/claims/{passed_a.claim_id}?version=1).\n\n"
            "## What Tycho concludes\n\nExecution isolation is a standard control.\n\n"
            "## Counter-signals\n\nBoth may be answering one checklist.\n\n"
            "## What would change our mind\n\nA release returning isolation to opt-in."
        ),
        delivered_to=[],
        created_at=now,
        strategy_session_id=session.session_id,
        strategy_card_ids=[passed.card_id],
    )
    return session, brief


@dataclass
class RecordingSource:
    """An in-memory ``ReadSource`` that records every method it is asked for."""

    market: DashboardMarket
    calls: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in FORBIDDEN_METHODS:
            if hasattr(self, name):  # pragma: no cover - guards the fixture itself
                raise AssertionError(f"the fake source must not expose {name}")

    def _record(self, name: str) -> None:
        self.calls.append(name)

    def list_claims(self) -> list[Claim]:
        self._record("list_claims")
        return list(self.market.claims)

    def get_claim(self, claim_id: str) -> Claim | None:
        self._record("get_claim")
        return next((c for c in self.market.claims if c.claim_id == claim_id), None)

    def get_delta(self, delta_id: str) -> Delta | None:
        self._record("get_delta")
        return next((d for d in self.market.deltas if d.delta_id == delta_id), None)

    def list_canonical_deltas(self) -> list[Delta]:
        self._record("list_canonical_deltas")
        return list(self.market.deltas)

    def get_observation(self, obs_id: str) -> Observation | None:
        self._record("get_observation")
        return next((o for o in self.market.observations if o.obs_id == obs_id), None)

    def watcher_activity(self) -> list[dict[str, Any]]:
        self._record("watcher_activity")
        return [dict(row) for row in self.market.watchers]

    def strategy_sessions(self) -> list[StrategySession]:
        self._record("strategy_sessions")
        return list(self.market.sessions)

    def get_strategy_session(self, session_id: str) -> StrategySession | None:
        self._record("get_strategy_session")
        return next(
            (s for s in self.market.sessions if s.session_id == session_id), None
        )

    def get_brief(self, brief_id: str) -> Brief | None:
        self._record("get_brief")
        return next((b for b in self.market.briefs if b.brief_id == brief_id), None)
