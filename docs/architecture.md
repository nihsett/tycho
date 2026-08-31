# Tycho — Architecture

> Status: living document. This grows into the submission's architecture
> diagram + write-up. Keep it in sync with `tycho-spec.org` (the contract);
> this file explains *how it runs*, the spec defines *what is true*.

## One paragraph

Tycho is a fleet of background agents that maintains a living, evidenced
theory of each competitor. Deterministic watchers observe public sources on
a schedule and store immutable raw Observations; a hash gate avoids unchanged
work, then a bounded Gemini 3.7 Flash semantic differ compares the normalized
before/after pair and emits one grounded canonical Delta@2. Mixed-product pages
are filtered to the configured entity before significance is assessed. Python
validates every quote within one real source field plus every ID, enum, bound,
and routing decision before the append-only canonical Delta is stored. Failed
provider or validation attempts create no Delta and remain retryable.
Retired mechanical Delta@1 rows live only in the immutable audit log used by
migration and rollback; normal reads cannot see them. A single generic analyst
agent converts meaningful canonical Deltas into versioned claims under a strict
lifecycle (evidence bars, supersession, demotion guard); consumers — a weekly
brief, an on-demand Q&A agent, and alerts — read only the claim store, never raw
data. The fleet also learns *about its own sources*, storing operational claims
that are injected before each fetch.

## Data flow

```
                       tycho.yaml (entities, sources, ontology, schedules)
                                        │ validated at boot
                                        ▼
 Cloud Scheduler ──► Cloud Run job (watcher: entity × source)
                        │  1. read operational claims for source  ◄─┐
                        │  2. adapter fetch (github|webpage|rss|search)
                        │  3. raw payload → Cloud Storage            │
                        │  4. observation row → BigQuery             │
                        ▼                                            │
                   hash gate ── unchanged ──► bump last_verified_at, stop
                        │ changed + clean                            │
                        ▼                                            │
                   bounded comparison bundle                         │
                        ▼                                            │
                   Gemini 3.7 Flash semantic differ (LOW, JSON)      │
                        ▼                                            │
                   Python schema + grounding + policy validation      │
                        │ failure ──► retryable run; no Delta         │
                        │ noise ──► canonical Delta@2 → BigQuery     │
                        │ meaningful                                 │
                        ▼                                            │
                   canonical Delta@2 → BigQuery → Pub/Sub             │

 Historical replay/import is a separate non-publishing path. During the
 governed cutover, the pre-swap physical table is renamed to
 tycho.delta_audit_log_20260826; only validated Delta@2 rows are copied into
 the fresh strict tycho.deltas table. The audit table is never a normal read
 source.
                        ▼                                            │
              Cloud Run analyst dispatcher                          │
                validates envelope; sends delta_id only             │
                        ▼                                            │
          Agent Runtime (managed Agent Identity, ADK)               │
                loads canonical delta from BigQuery                  │
                input: delta + scope claims + market claims          │
                ordered checks (see spec §Analyst)                   │
                tools: create / supersede / adjust /                 │
                       bump_verified / no_action                     │
                tool layer hard-enforces lifecycle rules             │
                        │                                            │
                        ├── intel claims ──► Firestore claim store   │
                        └── operational claims (sources/*) ──────────┘
                                        │
          ┌─────────────────────────────┼──────────────────────┐
          ▼                             ▼                      ▼
   Brief writer (weekly)        Q&A agent (on demand)    Alerts (critical)
   diff-of-beliefs report       answers ONLY from        immediate Slack
   → Slack/email                claims, cites evidence,
                                voices staleness
```

The Intelligence Dashboard is the fleet's read surface. It reads the same claim
store and canonical Delta table, adds nothing to them, and reaches the Strategy
Council only through the existing private dispatcher:

```
 browser (no Google credential, no database access)
        │  same-origin HTTPS; public judging read surface
        ▼
 Cloud Run tycho-dashboard  (min 0 / max 1, read-only service account)
   ├── static bundle (React/TypeScript/Vite, built ahead of deploy)
   └── FastAPI read model
         ├── Firestore  claims, strategy sessions, briefs   (datastore.viewer)
         ├── BigQuery   canonical delta@2 + observations    (dataViewer/jobUser)
         └── POST {"trigger":"dashboard","period":"previous_complete_week"}
               ──► tycho-strategy-dispatcher (run.invoker on that service only)
                     ──► the same governed workflow the weekly Scheduler runs
```

No GCS, no Pub/Sub, no Analyst Runtime, no claim or Delta write, and no access
to the archived `delta_audit_log_*` table.

The strategy council reads the same claim store and canonical Delta table. It
writes nothing back into them:

```
 Cloud Scheduler (weekly) ─┐
                           ├──► strategy dispatcher (bounded period + request ID;
 dashboard "Run Strategy" ─┘      no prompt text is accepted)
                                        ▼
              atomic: lease on (period, strategy_version) + running session
                          duplicate trigger ──► read the running session, no model call
                                        ▼
                        bounded context builder (Python, deterministic)
                          active claims on canonical Delta@2 only
                          exact versions + staleness + period activity
                          metrics carrying their contributing Delta IDs
                          byte/token budget ── over budget ──► durable failure
                                        ▼
                                 tycho_strategist        (≤ 3 draft cards)
                                        ▼
                        hard proposal validation (Python is the authority)
                          pinning, canonical v2, ≥2 entities, ≥2 source families,
                          mirrored-evidence collapse, staleness labels,
                          confidence ceiling, conclusion-language policy
                          failed ──► rejected before the Challenger sees it
                                        ▼
                                 tycho_challenger        (one pass per survivor)
                                        ▼
                        hard challenge gate — a pass can only reject, never revive
                                        ▼
                                 tycho_brief_writer      (passed cards only)
                                        ▼
                        citation validation → dashboard links
                          unpinned / malformed / URL ──► the run fails
                                        ▼
        atomic: brief + terminal session + lease release (ownership checked)
                              → Firestore / SQLite
```

## Components

| Component | Kind | Runs on | Notes |
|---|---|---|---|
| Watchers | deterministic jobs | Cloud Run jobs, fired by Cloud Scheduler | one logical watcher per (entity, source); config-driven, no per-entity code |
| Adapters | libraries | inside watchers | 4 types: `github` (API), `webpage` (fetch+extract), `rss`, `search`. Screenshots allowed as `image` observations for JS-hostile pages |
| Hash gate | deterministic | inside watchers | unchanged content bumps verification and stops before model work |
| Semantic differ | bounded model call | Cloud Run acquisition | Gemini 3.7 Flash over normalized observation pairs; no tools/search; exact quotes and configured-entity relevance required |
| Semantic validator | Python | inside acquisition | strict Pydantic, grounding, duplicate/bounds/policy checks; derives scopes |
| Delta generation leases | data | Firestore / SQLite | transactional `(obs_before, obs_after, generated_by, prompt_version)` retry identity |
| Analyst | ADK agent (Gemini) | local shadow mode; Agent Runtime via Cloud Run dispatcher | single generic agent; source-agnostic; lifecycle enforced by tool layer, not prompt; old Cloud Run service remains the rollback target |
| Analyst run leases | data | Firestore / SQLite | transactional `(delta_id, mode, analyst_version)` identity; blocks duplicate model calls |
| Claim store | data | Firestore | versioned docs, embedded history, doubly-linked supersession chain |
| Canonical Delta log | data | BigQuery `tycho.deltas` (partitioned) | append-only, strict Delta@2 |
| Delta audit log | data | BigQuery `tycho.delta_audit_log_20260826` | immutable migration/rollback evidence only |
| Observation log | data | BigQuery (partitioned) + Cloud Storage (raw) | append-only; the immutable "what happened" |
| Brief writer | ADK agent | Cloud Run, weekly schedule | renders diff-of-beliefs; pins (claim_id, version) |
| Strategy Council entrypoint | deterministic `BaseAgent` (`tycho_strategy_council`) | Strategy Council Runtime (Agent Identity, min 0 / max 1) | parses only a bounded `StrategyRequest`; no outer model, no tools; drives the three agents through the governed workflow, never as a raw ADK sequence |
| Strategist | ADK agent (`tycho_strategist`) | Strategy Council Runtime | proposes ≤3 present-state cross-entity conclusions; no tools, strict structured output |
| Challenger | ADK agent (`tycho_challenger`) | same Runtime | independently checks one card against its pinned premises; its pass is quality control, not evidence |
| Strategy brief writer | ADK agent (`tycho_brief_writer`) | same Runtime | writes the brief from passed cards only; cites claim versions, never URLs |
| Strategy evidence rules | Python | `pipeline/strategy_evidence.py` | pinning, canonical-v2, entity/source-family diversity, staleness, confidence ceiling, conclusion-language policy |
| Strategy context builder | Python | `pipeline/strategy_context.py` | deterministic bounded manifest and metrics; fails durably over budget |
| Strategy sessions | data | Firestore / SQLite | write-once audit of cards, challenges, manifest hash, safe metrics; brief + terminal state + lease release commit atomically |
| Strategy leases | data | Firestore / SQLite | transactional `(period_from, period_to, strategy_version)` identity; the lease and the running session are created together, and the final commit requires the session to still own an active lease |
| Strategy dispatcher | deterministic service | private Cloud Run (`tycho-strategy-dispatcher`), own service account | authenticated; accepts a trigger naming a period, never a date range or prompt; derives the previous complete week itself and normalizes it into the bounded `StrategyRequest`; returns and logs only IDs, state, counts, and `skipped` |
| Strategy schedule | Cloud Scheduler (`tycho-strategy-weekly`) | — | Monday 06:00 UTC (`0 6 * * 1`); OIDC to the strategy dispatcher only; its body is a static period *name*, so it cannot widen the window |
| Dashboard read model | Python | `dashboard/api/readmodel.py` | named query methods; canonical `delta@2` only, exact claim versions, deterministic active/stale/disputed counts, bounded pagination |
| Dashboard read source | Python | `dashboard/api/source.py` | the complete store surface a dashboard read may touch: nine read methods, no write, no GCS, no archive table |
| Dashboard API | FastAPI | public Cloud Run judging instance (`tycho-dashboard`), read-only service account, min 0 / max 1 | strict bounded response models; CSP and secure headers; same-origin writes only; structural logs |
| Dashboard frontend | React / TypeScript / Vite | served by the same Cloud Run service | one typed API client, closed SSE enum, no `dangerouslySetInnerHTML` |
| Dashboard strategy trigger | Python | `dashboard/api/runs.py` | posts the fixed trigger to the existing private dispatcher; duplicate-safe in-process and through the shared lease |
| Q&A agent | ADK agent | Cloud Run | claims-only answers with evidence citations; refuses when no claim covers the question |
| Delivery receipts | data | Firestore | (claim_id, version) delivered once per context; new version re-delivers |
| Config | YAML in repo | — | canonical entity keys, sources, ontology, staleness clocks, schedules |

## The two-store split (deliberate)

- **BigQuery + Cloud Storage = what happened.** Observations and canonical
  Delta@2 rows are append-only and partitioned by time. The legacy Delta audit
  table is immutable evidence, not a second application domain. Facts never
  change.
- **Firestore = what we believe.** Claims and their history, mutable only
  through the lifecycle (supersession, confidence adjustment). Beliefs revise.

Accumulate facts; revise beliefs. Never the other way around.

## Memory as the moat (why this isn't a scraper + summarizer)

1. **Claims, not summaries.** Every statement is scoped to an ontology node,
   classed (fact / inference / operational), confidence-tiered, evidenced,
   and versioned.
2. **Supersession, not overwrite.** Publish-before-retire; crash-safe;
   full belief history browsable per claim.
3. **Evidence bars in code.** Facts need a primary source; normal inferences
   need ≥2 independent sources. A one-source inference is allowed only when it
   links via `disputes` to established knowledge and asserts that a conflicting
   signal exists. Future/intent inferences are clamped to speculative.
4. **Disputes before demotion.** Third-party contradictions become separate
   speculative claims linked to established knowledge; speculative+critical
   creation alerts immediately. Primary evidence supersedes/resolves disputes.
   The demotion guard remains a backstop against unsafe model attempts.
5. **Operational memory.** Source quirks (moved URLs, JS walls, stale
   extraction hints) are learned once, stored as claims, injected pre-fetch.
   Specialization lives in data, not code.
6. **Staleness as a first-class signal.** `last_verified_at` clocks per
   branch; stale claims are greyed, flagged in briefs, and voiced by the
   Q&A agent.

## Security posture

- Scraped content is adversarial by default. A screen quarantines fetched
  content containing instruction-like text aimed at LLMs
  (`status: quarantined`); quarantined observations are stored but never
  reach the analyst.
- Model Armor on analyst / Q&A model calls (enterprise-track checkbox, but
  also genuinely load-bearing here).
- Watchers run with a service account scoped to write-only on raw storage
  and observation tables; only the claim tool layer writes Firestore.
- The managed analyst Agent Identity receives only Firestore, BigQuery read/job,
  and trace-writer roles. It receives no GCS or Pub/Sub permissions.
- Pub/Sub reaches the analyst through a separate authenticated dispatcher. The
  dispatcher sends only a validated `delta_id` and never writes claims.
- The strategy dispatcher is private and authenticated, runs as its own service
  account, and is separate from the analyst Pub/Sub dispatcher. It accepts only
  a trigger naming which bounded period to run; it resolves the week from its
  own clock and normalizes it through the same `StrategyRequest` the Runtime
  accepts, so it is structurally incapable of sending anything wider. It rejects
  any extra field by name, so no caller can smuggle prompt text into an agent,
  and it refuses a Runtime result carrying an unexpected field rather than
  logging or returning it.
- The strategy deployment tool routes every shell-out through one guard that
  refuses any non-read-only command naming the analyst subscription, dispatcher,
  Runtime, acquisition job, nightly schedule, or Delta topic. It reads those
  resources for before/after evidence and can never write them.
- The strategy council agents hold no tools: no web search, no GCS, no Pub/Sub,
  no Memory Bank, and no claim mutation. Its Runtime identity requests only
  `roles/datastore.user`, `roles/bigquery.dataViewer`, `roles/bigquery.jobUser`,
  and `roles/telemetry.tracesWriter`.
- Durable strategy records never store `str(exception)`. Pydantic renders
  `input_value`, so a model response or grounded quote could otherwise reach
  storage; one sanitizer reduces every failure to stage, class, and a curated
  reason.
- Model requests, responses, prompts, delta changes, and claim text are removed
  from persisted runtime spans; safe IDs, action names, and structural spans remain.
  Strategy events are built from an allowlist of structural fields, so a
  content-bearing field cannot leak by being added upstream.
- The dashboard is private Cloud Run with no `allUsers` binding, and the browser
  never receives a Google credential: the API authenticates with its own Cloud
  Run service account. That identity is read-only on data
  (`roles/bigquery.dataViewer`, `roles/bigquery.jobUser`,
  `roles/datastore.viewer`, `roles/logging.logWriter`) plus `roles/run.invoker`
  on the strategy dispatcher service alone. It cannot write a claim or Delta,
  publish to Pub/Sub, read Cloud Storage, or invoke the Analyst Runtime.
- The dashboard's own deployment tool reuses the analyst-path guard and adds the
  strategy resources to it. Its one permitted write against a protected resource
  is exactly `roles/run.invoker` on `tycho-strategy-dispatcher` for the dashboard
  service account; a wider role, a different member, or a different verb is
  refused.
- Dashboard responses are strict bounded Pydantic models, and a rejected request
  is answered with a generic error rather than FastAPI's default body, which
  would echo the caller's own input. Log lines carry route template, method,
  status, latency, and validated IDs; a path parameter that failed validation is
  recorded as the literal `invalid`.
- Grounded quotes and brief prose are untrusted text in the browser too. The
  frontend renders a bounded Markdown subset into React elements, strips `<` and
  `>`, keeps only internal claim-provenance links, and contains no
  `dangerouslySetInnerHTML`. A strict CSP (`default-src 'none'`, `connect-src
  'self'`, no inline script or style) is served on every response.

## Observability

- Every acquisition decision leaves bounded metadata: `generated_by`,
  `prompt_version`, `triage_by`, validation outcome, token counts, latency, and
  cost on the Delta-generation run; claims retain `created_by` + evidence.
- Provenance chain is fully clickable: brief line → claim (+ history) →
  Delta@2 → Observation → raw snapshot. Noise remains in BigQuery for audit.
- OpenTelemetry traces on agent runs (ADK-native), correlated by delta_id.
- Runtime trace export is redacted before the managed exporter; direct Cloud
  Trace inspection is required before any production routing change. Inspection
  must follow Cloud Trace pagination to exhaustion: `pageSize` is a scan budget
  over the project's traces, not a result count, so one page silently returns a
  subset.
- Field-name scanning alone is not proof. `infra/verify_strategy_production.py`
  also pulls the governed prose a session actually read and wrote — claim
  statements and rationales, Delta change statements, grounded quotes, card text,
  brief prose — out of the store and proves none of it occurs in any exported
  trace or log payload.

## Cost design

- The hash gate eliminates unchanged pairs before any semantic model call.
- Gemini reads only complete, normalized before/after observations within a
  conservative 1.5 MB / 500,000-estimated-token request ceiling. Near the byte
  ceiling, acquisition calls the token-count API; oversized input is recorded
  as a failed generation run, never silently truncated.
- GitHub input keeps only tag, name, body, draft, prerelease, and published_at.
  Webpage input keeps the extracted title and section content. Full immutable
  observations remain in raw storage; no prompt/response content is copied to
  Firestore or traces.
- The semantic response is capped at eight changes and every meaningful change
  must quote one actual string field in the after Observation; field boundaries
  cannot be concatenated into evidence. Broad changelogs omit sibling-product
  entries that are not connected to the configured entity. Current promotional
  pricing is $0.75/M
  input and $3.75/M output through 2026-12-31; price constants stay outside
  domain validation.
- Quantifiable from the Delta-generation runs: total attempts, valid
  meaningful/noise outcomes, failures, token counts, and estimated cost.

## Local / cloud parity

Every store sits behind a thin interface (`ObservationStore`, `ClaimStore`,
`ReceiptStore`, `AnalystLeaseStore`, `DeltaGenerationStore`, …) with two
implementations selected by env:
- `local`: filesystem/SQLite — development and bounded Gemini calibration;
  generation runs and leases use the same retry state machine and canonical
  deltas are still Delta@2.
- `gcp`: BigQuery / Firestore / GCS / Pub/Sub — production and demo; the semantic
  differ uses Vertex/ADC credentials and persists only safe generation metadata.
Production acquisition has only `semantic` mode. The old path/hunk differ is
isolated to explicit local/test or archive-decoding compatibility and is never a
canonical Delta producer or rollback authority.

## Deliberate non-goals (v1 fence)

No auto-generated scrapers; no dynamic ontology growth; no strategy
*recommendations* — the council concludes what is presently true and what would
falsify it, and never proposes an action; no multi-tenancy/auth; ≤4 entities,
≤4 source types.

## The dashboard read model (deliberate constraints)

- **Canonical only.** Every Delta query names `tycho.deltas` and pins
  `schema_version = 'delta@2'`. The archived `delta_audit_log_20260826` table is
  not reachable from any dashboard code path.
- **Exact versions.** A provenance request resolves `(claim_id, version)`. The
  current version returns the live claim; an earlier version is reconstructed
  from the claim's embedded history and labelled as reconstructed; a version
  that never existed is a 404.
- **Derived badges.** `stale` comes from the `tycho.yaml` threshold for the
  claim's scope; `disputed` is derived from active inbound `disputes` links, not
  stored on the target.
- **Derived agent activity.** The council's in-flight events are not persisted,
  so the dashboard does not replay them. It reconstructs a structural timeline
  from the session record — agent, state, counts, IDs — and says so in the UI. A
  rejection reason is reduced to a deterministic class before it becomes an
  event, so Challenger prose never reaches the activity timeline.
- **Bounded cache.** Overview and health share one 60-second snapshot so the two
  panels never disagree. Timelines, provenance, sessions, and event streams are
  never cached: an in-progress run can never be served as completed.

## Diagram TODO for submission

- [ ] Redraw the flow above as a proper diagram (one box per GCP service,
      rubric words as labels: Registry, Memory Bank, Gateway, Guardrails,
      Observability)
- [ ] Screenshot: claim history timeline (supersession chain)
- [ ] Screenshot: provenance click-through (brief → snapshot)
- [ ] Screenshot: quarantine catching the planted injection page
