import type {
  ActivityEvent,
  ActivityKind,
  EntityCard,
  HealthResponse,
  OverviewResponse,
  ProvenanceResponse,
  StrategySessionResponse,
  TimelineResponse,
} from "../lib/types";

export const CLAIM_A = "clm_01M0YFPDQQH8KSB322AHQ8GZ1C";
export const CLAIM_B = "clm_01M0YFMB5EEFBEQX2F6EJ3YC2T";
export const CLAIM_OLD = "clm_01M0YFN2J0VBN56P46Z3TM5257";
export const DELTA_A = "dlt_01M0YF3Q87GQZ38YYFF2W8N2AF";

export function entityCard(overrides: Partial<EntityCard> = {}): EntityCard {
  return {
    entity: "claude_code",
    name: "Claude Code",
    description: "Anthropic's agentic coding tool.",
    latest_change: {
      delta_id: DELTA_A,
      statement: "Claude Code enables sandboxed shell execution by default.",
      category: "capability",
      scope: "product/capabilities",
      source: "github_releases",
      source_family: "claude_code/official_release",
      observed_at: "2026-08-26T02:03:21Z",
      change_count: 3,
    },
    active_fact_count: 4,
    active_claim_count: 6,
    last_observed_at: "2026-08-27T02:02:01Z",
    last_verified_at: "2026-08-26T02:03:25Z",
    notable_claim: {
      claim_id: CLAIM_A,
      version: 1,
      statement: "Claude Code cost estimates factor in the US-only inference premium.",
      scope: "pricing",
      confidence: "confirmed",
      severity: "critical",
      stale: false,
      last_verified_at: "2026-08-26T02:03:21Z",
    },
    stale: false,
    disputed: false,
    watchers: [],
    waiting_for: null,
    ...overrides,
  };
}

export function overview(entities: EntityCard[] = [entityCard()]): OverviewResponse {
  return {
    generated_at: "2026-08-27T10:00:00Z",
    entities,
    totals: {
      active_claims: 11,
      retired_claims: 27,
      superseded_claims: 0,
      canonical_deltas: 56,
      meaningful_deltas: 16,
      noise_deltas: 40,
      observations: 98,
    },
  };
}

export function health(): HealthResponse {
  return {
    generated_at: "2026-08-27T10:00:00Z",
    state: "ok",
    components: [
      {
        key: "acquisition",
        name: "Acquisition watchers",
        state: "ok",
        detail: "8 watchers, 98 observations recorded",
        last_success_at: "2026-08-27T02:02:01Z",
        count: 8,
      },
      {
        key: "strategy",
        name: "Strategy Council",
        state: "ok",
        detail: "1 completed of 3 sessions",
        last_success_at: "2026-08-26T17:05:07Z",
        count: 1,
      },
    ],
  };
}

export function timeline(): TimelineResponse {
  return {
    entity: "codex",
    scope: null,
    total: 3,
    limit: 25,
    offset: 0,
    next_offset: null,
    events: [
      {
        event_id: `${CLAIM_A}:1:created`,
        kind: "created",
        at: "2026-08-26T02:03:21Z",
        claim: {
          claim_id: CLAIM_A,
          version: 1,
          entity: "codex",
          scope: "pricing",
          claim_class: "fact",
          statement: "Codex team plan is $45 per seat per month (was $30).",
          confidence: "confirmed",
          severity: "critical",
          status: "active",
          stale: false,
        },
        replacement: null,
        evidence: [
          {
            delta_id: DELTA_A,
            source: "website_changelog",
            source_family: "codex/official_release",
            canonical: true,
          },
        ],
        note: "created by gemini-analyst@1",
      },
      {
        event_id: `${CLAIM_OLD}:1:superseded`,
        kind: "superseded",
        at: "2026-08-26T02:03:20Z",
        claim: {
          claim_id: CLAIM_OLD,
          version: 1,
          entity: "codex",
          scope: "pricing",
          claim_class: "fact",
          statement: "Codex team plan is $30 per seat per month.",
          confidence: "confirmed",
          severity: "critical",
          status: "superseded",
          stale: true,
        },
        replacement: {
          claim_id: CLAIM_A,
          version: 1,
          entity: "codex",
          scope: "pricing",
          claim_class: "fact",
          statement: "Codex team plan is $45 per seat per month (was $30).",
          confidence: "confirmed",
          severity: "critical",
          status: "active",
          stale: false,
        },
        evidence: [],
        note: "publish-before-retire: the replacement claim is linked",
      },
      {
        event_id: `${CLAIM_B}:1:retired`,
        kind: "retired",
        at: "2026-08-20T02:03:20Z",
        claim: {
          claim_id: CLAIM_B,
          version: 1,
          entity: "codex",
          scope: "product/capabilities",
          claim_class: "fact",
          statement: "Codex shipped an interactive agents dashboard.",
          confidence: "confirmed",
          severity: "notable",
          status: "retired",
          stale: true,
        },
        replacement: null,
        evidence: [],
        note: "retired from the active belief set",
      },
    ],
  };
}

export function emptyTimeline(): TimelineResponse {
  return {
    entity: "pi",
    scope: null,
    total: 0,
    limit: 25,
    offset: 0,
    next_offset: null,
    events: [],
  };
}

export function strategySession(): StrategySessionResponse {
  return {
    session: {
      session_id: "sts_01M0ZGG793KQK3BTCVPAP3DH2D",
      state: "completed",
      question: "What materially changed across the monitored coding-agent market?",
      period_from: "2026-08-17T00:00:00Z",
      period_to: "2026-08-24T00:00:00Z",
      created_at: "2026-08-26T17:04:51Z",
      updated_at: "2026-08-26T17:05:07Z",
      strategy_version: "strategy-council@1",
      manifest_hash: "eaff310db5b2964af12c2251cbbc0cbb9654bcdeec447ab8bb5b7b629ad76589",
      agent_versions: { strategist: "tycho_strategist@1" },
      model_versions: { strategist: "gemini-3.5-flash-lite" },
      metrics: {
        cards_proposed: 2,
        cards_passed: 1,
        cards_rejected: 1,
        challenges: 1,
        manifest_entries: 11,
        input_bytes: 22866,
        estimated_input_tokens: 5717,
        input_tokens: 8047,
        output_tokens: 485,
        total_tokens: 8532,
        latency_ms: 3137,
      },
      error: null,
      brief_id: "brf_2026w35-pap3dh2d",
    },
    brief: {
      brief_id: "brf_2026w35-pap3dh2d",
      period_from: "2026-08-17T00:00:00Z",
      period_to: "2026-08-24T00:00:00Z",
      created_at: "2026-08-26T17:05:07Z",
      rendered_md: `# Tycho strategy brief\n\n## What changed\n\nTwo vendors shipped isolation controls [${CLAIM_A}@v1](/claims/${CLAIM_A}?version=1).\n\n## What Tycho concludes\n\nIsolation is now standard.`,
      claims_referenced: [],
      stats_new: 5,
      stats_superseded: 0,
      stats_confidence_changes: 0,
      stats_stale_flagged: 0,
      empty: false,
    },
    passed_cards: [
      {
        card_id: "stc_01M0ZGGMZ5GF44D2R8M7FT4GYG",
        statement: "Workspace execution isolation is a standard control.",
        rationale: "Two vendors converged on comparable isolation defaults.",
        confidence: "likely",
        competing_explanation: "Both may be answering one procurement checklist.",
        falsifier: "A release returning isolation to opt-in.",
        entities: ["claude_code", "codex"],
        scopes: ["product/capabilities"],
        source_families: ["claude_code/official_release", "codex/official_release"],
        premises: [
          {
            claim_id: CLAIM_A,
            claim_version: 1,
            delta_ids: [DELTA_A],
            entity: "claude_code",
            scope: "product/capabilities",
            statement: "Claude Code enables sandboxed shell execution by default.",
            confidence: "confirmed",
            resolved: true,
          },
        ],
        limitations: [],
        status: "passed",
        rejection_reasons: [],
        challenger_verdict: "pass",
        challenger_reasons: [],
      },
    ],
    rejected_cards: [
      {
        card_id: "stc_01M0ZGGPA4W87FCF28W9RBCS50",
        statement: "Codex raised seat pricing because rivals shipped isolation.",
        rationale: "A single vendor's pricing move.",
        confidence: "speculative",
        competing_explanation: "Ordinary annual repricing.",
        falsifier: "A public price rollback.",
        entities: ["codex"],
        scopes: ["pricing"],
        source_families: ["codex/official_release"],
        premises: [],
        limitations: [],
        status: "rejected",
        rejection_reasons: [
          "a cross-entity conclusion needs 2 distinct entities; got 1",
          "conclusion asserts unsupported causation",
        ],
        challenger_verdict: null,
        challenger_reasons: [],
      },
    ],
    waiting_for: null,
  };
}

export function provenance(): ProvenanceResponse {
  return {
    claim: {
      claim_id: CLAIM_A,
      version: 1,
      entity: "claude_code",
      scope: "product/capabilities",
      claim_class: "fact",
      statement: "Claude Code enables sandboxed shell execution by default.",
      confidence: "confirmed",
      severity: "notable",
      status: "active",
      stale: false,
    },
    requested_version: 1,
    current_version: 1,
    exact_version: true,
    reconstruction_note: null,
    rationale: "Default-on isolation changes what a team must review.",
    created_at: "2026-08-26T02:03:21Z",
    last_verified_at: "2026-08-26T02:03:21Z",
    created_by: "gemini-analyst@1",
    staleness_days: 60,
    lifecycle: { supersedes: null, superseded_by: null, disputes: null, disputed_by: [] },
    history: [],
    evidence: [
      {
        delta_id: DELTA_A,
        entity: "claude_code",
        source: "github_releases",
        source_family: "claude_code/official_release",
        computed_at: "2026-08-26T02:03:21Z",
        triage: "meaningful",
        summary: "Claude Code enables sandboxed shell execution by default.",
        generated_by: "gemini-3.7-flash@semantic-differ-1",
        prompt_version: "semantic-delta@2",
        changes: [
          {
            category: "capability",
            scope: "product/capabilities",
            statement: "Sandboxing became the default.",
            before: null,
            after: "Sandboxed shell execution is enabled by default.",
            quote_before: null,
            quote_after: "Sandboxed shell execution is enabled by default.",
          },
        ],
        observations: [
          {
            obs_id: "obs_01M0YF3Q87GQZ38YYFF2W8N2AF",
            role: "before",
            fetched_at: "2026-08-25T02:03:21Z",
            kind: "structured",
            status: "ok",
            resolved: true,
          },
          {
            obs_id: "obs_01M0YF3Q87GQZ38YYFF2W8N2AG",
            role: "after",
            fetched_at: "2026-08-26T02:03:21Z",
            kind: "structured",
            status: "ok",
            resolved: true,
          },
        ],
        source_ref: {
          source: "github_releases",
          kind: "repository",
          target: "https://github.com/anthropics/claude-code",
        },
        admissible: true,
        defect: null,
      },
    ],
  };
}

export function activityEvent(
  kind: ActivityKind,
  seq: number,
  overrides: Partial<ActivityEvent> = {},
): ActivityEvent {
  return {
    seq,
    event: kind,
    at: "2026-08-27T10:00:00Z",
    session_id: "sts_01M0ZGG793KQK3BTCVPAP3DH2D",
    run_id: "run_0123456789abcdef",
    agent: "tycho_strategy_council",
    state: "completed",
    card_id: null,
    brief_id: null,
    card_count: 0,
    passed_count: 0,
    rejected_count: 0,
    reason_count: 0,
    reason_classes: [],
    claim_versions: [],
    failure_class: null,
    derived: true,
    ...overrides,
  };
}
