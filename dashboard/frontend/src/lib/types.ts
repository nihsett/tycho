/**
 * The dashboard API contract, mirrored exactly.
 *
 * Every event and provenance type here is closed: there is no `any`, no index
 * signature, and no free-form payload. If the API grows a field, this file has
 * to grow with it, which is the point.
 */

export type ComponentState = "ok" | "stale" | "idle" | "failed" | "unknown";

export interface HealthComponent {
  key: string;
  name: string;
  state: ComponentState;
  detail: string;
  last_success_at: string | null;
  count: number;
}

export interface HealthResponse {
  generated_at: string;
  state: ComponentState;
  components: HealthComponent[];
}

export interface WatcherStatus {
  source: string;
  kind: string;
  target: string;
  last_observed_at: string | null;
  observation_count: number;
}

export interface LatestChange {
  delta_id: string;
  statement: string;
  category: string | null;
  scope: string | null;
  source: string;
  source_family: string;
  observed_at: string;
  change_count: number;
}

export interface NotableClaim {
  claim_id: string;
  version: number;
  statement: string;
  scope: string;
  confidence: string;
  severity: string;
  stale: boolean;
  last_verified_at: string;
}

export interface EntityCard {
  entity: string;
  name: string;
  description: string;
  latest_change: LatestChange | null;
  active_fact_count: number;
  active_claim_count: number;
  last_observed_at: string | null;
  last_verified_at: string | null;
  notable_claim: NotableClaim | null;
  stale: boolean;
  disputed: boolean;
  watchers: WatcherStatus[];
  waiting_for: string | null;
}

export interface OverviewTotals {
  active_claims: number;
  retired_claims: number;
  superseded_claims: number;
  canonical_deltas: number;
  meaningful_deltas: number;
  noise_deltas: number;
  observations: number;
}

export interface OverviewResponse {
  generated_at: string;
  entities: EntityCard[];
  totals: OverviewTotals;
}

export type LifecycleKind =
  | "created"
  | "verified"
  | "disputed"
  | "superseded"
  | "retired";

export interface ClaimPin {
  claim_id: string;
  version: number;
  entity: string;
  scope: string;
  claim_class: string;
  statement: string;
  confidence: string;
  severity: string;
  status: string;
  stale: boolean;
}

export interface EvidenceChip {
  delta_id: string;
  source: string;
  source_family: string;
  canonical: boolean;
}

export interface TimelineEvent {
  event_id: string;
  kind: LifecycleKind;
  at: string;
  claim: ClaimPin;
  replacement: ClaimPin | null;
  evidence: EvidenceChip[];
  note: string | null;
}

export interface TimelineResponse {
  entity: string;
  scope: string | null;
  total: number;
  limit: number;
  offset: number;
  next_offset: number | null;
  events: TimelineEvent[];
}

export interface ObservationRef {
  obs_id: string;
  role: string;
  fetched_at: string | null;
  kind: string | null;
  status: string | null;
  resolved: boolean;
}

export interface GroundedChange {
  category: string | null;
  scope: string | null;
  statement: string;
  before: string | null;
  after: string | null;
  quote_before: string | null;
  quote_after: string | null;
}

export interface SourceRef {
  source: string;
  kind: string;
  target: string;
}

export interface DeltaEvidence {
  delta_id: string;
  entity: string;
  source: string;
  source_family: string;
  computed_at: string;
  triage: string;
  summary: string;
  generated_by: string;
  prompt_version: string;
  changes: GroundedChange[];
  observations: ObservationRef[];
  source_ref: SourceRef | null;
  admissible: boolean;
  defect: string | null;
}

export interface LifecycleLinks {
  supersedes: string | null;
  superseded_by: string | null;
  disputes: string | null;
  disputed_by: string[];
}

export interface HistoryEntry {
  at: string | null;
  event: string | null;
  action: string | null;
  actor: string | null;
  reason: string | null;
  version: number | null;
  status: string | null;
  delta_ids: string[];
}

export interface ProvenanceResponse {
  claim: ClaimPin;
  requested_version: number;
  current_version: number;
  exact_version: boolean;
  reconstruction_note: string | null;
  rationale: string;
  created_at: string;
  last_verified_at: string;
  created_by: string;
  staleness_days: number;
  lifecycle: LifecycleLinks;
  history: HistoryEntry[];
  evidence: DeltaEvidence[];
}

export interface PremiseChip {
  claim_id: string;
  claim_version: number;
  delta_ids: string[];
  entity: string | null;
  scope: string | null;
  statement: string | null;
  confidence: string | null;
  resolved: boolean;
}

export interface CardView {
  card_id: string;
  statement: string;
  rationale: string;
  confidence: string;
  competing_explanation: string;
  falsifier: string;
  entities: string[];
  scopes: string[];
  source_families: string[];
  premises: PremiseChip[];
  limitations: string[];
  status: string;
  rejection_reasons: string[];
  challenger_verdict: string | null;
  challenger_reasons: string[];
}

export interface BriefView {
  brief_id: string;
  period_from: string;
  period_to: string;
  created_at: string;
  rendered_md: string;
  claims_referenced: ClaimPin[];
  stats_new: number;
  stats_superseded: number;
  stats_confidence_changes: number;
  stats_stale_flagged: number;
  empty: boolean;
}

export interface SessionMetricsView {
  cards_proposed: number;
  cards_passed: number;
  cards_rejected: number;
  challenges: number;
  manifest_entries: number;
  input_bytes: number;
  estimated_input_tokens: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  latency_ms: number;
}

export interface SessionView {
  session_id: string;
  state: string;
  question: string;
  period_from: string;
  period_to: string;
  created_at: string;
  updated_at: string;
  strategy_version: string;
  manifest_hash: string;
  agent_versions: Record<string, string>;
  model_versions: Record<string, string>;
  metrics: SessionMetricsView;
  error: string | null;
  brief_id: string | null;
}

export interface StrategySessionResponse {
  session: SessionView | null;
  brief: BriefView | null;
  passed_cards: CardView[];
  rejected_cards: CardView[];
  waiting_for: string | null;
}

/** The closed SSE / activity event enum. Structure only, never content. */
export type ActivityKind =
  | "run_started"
  | "agent_started"
  | "agent_completed"
  | "card_rejected"
  | "brief_completed"
  | "run_failed"
  | "heartbeat";

export interface ActivityEvent {
  seq: number;
  event: ActivityKind;
  at: string;
  session_id: string | null;
  run_id: string | null;
  agent: string | null;
  state: string | null;
  card_id: string | null;
  brief_id: string | null;
  card_count: number;
  passed_count: number;
  rejected_count: number;
  reason_count: number;
  reason_classes: string[];
  claim_versions: string[];
  failure_class: string | null;
  derived: boolean;
}

export interface ActivityResponse {
  session_id: string;
  events: ActivityEvent[];
  derived_from: string;
}

export type RunState = "dispatching" | "running" | "completed" | "failed";

export interface TriggerResponse {
  run_id: string;
  state: RunState;
  duplicate: boolean;
  session_id: string | null;
  brief_id: string | null;
  period_from: string;
  period_to: string;
  stream_path: string;
  detail: string;
}

export interface MetaResponse {
  entities: string[];
  scopes: string[];
  service: string;
  revision: string;
}

export interface StreamClosed {
  state: RunState;
  session_id: string | null;
  brief_id: string | null;
  duplicate: boolean;
}

export const ACTIVITY_KINDS: readonly ActivityKind[] = [
  "run_started",
  "agent_started",
  "agent_completed",
  "card_rejected",
  "brief_completed",
  "run_failed",
  "heartbeat",
];

export function isActivityKind(value: string): value is ActivityKind {
  return (ACTIVITY_KINDS as readonly string[]).includes(value);
}
