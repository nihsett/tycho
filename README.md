# Tycho

Tycho watches fast-moving coding agents and turns immutable source changes into
versioned, evidenced claims. The v1 tracer tracks:

- Claude Code — `anthropics/claude-code`
- OpenAI Codex — `openai/codex`
- Gemini CLI — `google-gemini/gemini-cli`
- Pi from [pi.dev](https://pi.dev) — `earendil-works/pi`

## Quick start

Everything runs through [`just`](https://just.systems). You do not need to
remember `uv`, pytest, Ruff, or `gcloud` invocations.

```bash
just setup     # sync the locked Python 3.13 environment
just check     # lock check, lint, compile, tests
just           # list every available command
```

Run one local acquisition cycle:

```bash
just local-github   # GitHub releases for all configured entities
just local-web      # official changelogs
just local-stats    # counts in the local SQLite store
```

The first cycle establishes real baselines. Later cycles append immutable
observations and deltas to `data/tycho.sqlite3`, store write-once payloads under
`data/raw/`, and persist evidenced claims. A durable outbox retries analyst
delivery after a crash. `pipeline/local_tracer.py` remains a stateless live-data
replay for demos.

## Commands

| Command | What it does |
|---|---|
| `just` / `just help` | List every recipe with its description |
| `just setup` | Sync the locked Python 3.13 environment |
| `just lint` | Run Ruff |
| `just test` | Run pytest |
| `just compile` | Byte-compile every Python package |
| `just check` | Lock check, lint, compile, tests |
| `just ci` | Noninteractive gate used by GitHub Actions |
| `just local-github` | One local GitHub-releases acquisition cycle |
| `just local-web` | One local changelog acquisition cycle |
| `just local-stats` | Print local SQLite store counts |
| `just calibrate` | Run the Gemini analyst calibration |
| `just strategy-session` | One synthetic, offline strategy-council session |
| `just strategy-brief` | The same, plus the rendered brief markdown |
| `just strategy-test` | Run only the strategy test suites |
| `just strategy-stats` | Sessions and briefs in the disposable strategy store |
| `just strategy-clean` | Delete the disposable strategy store |
| `just cutover-check` | Read-only BigQuery cutover inspection |
| `just audit` | Read-only audit of the strict Delta@2 canonical table |
| `just strategy-plan` | Print the Strategy Council deployment plan; contacts nothing |
| `just strategy-readback` | Read every deployed Strategy Council resource back |
| `just strategy-snapshot` | Record the untouched acquisition/Analyst production state |
| `just strategy-verify` | Re-verify the latest production strategy session |
| `just strategy-telemetry` | Re-inspect Runtime traces and dispatcher logs for leakage |
| `just dashboard-install` | Install the pinned frontend dependencies (`npm ci`) |
| `just dashboard-build` | Type-check and build the dashboard bundle |
| `just dashboard-test` | Run only the dashboard backend suites |
| `just dashboard-test-ui` | Run the frontend test suite (Vitest + Testing Library) |
| `just dashboard-serve` | Serve the dashboard locally against production read-only data |
| `just dashboard-plan` | Print the dashboard deployment plan; contacts nothing |
| `just dashboard-readback` | Read every deployed dashboard resource back |
| `just dashboard-snapshot` | Record the untouched strategy/analyst state and data counts |
| `just dashboard-verify` | Read-only end-to-end verification of the deployed dashboard |
| `just cutover-apply` | **Production** table cutover (asks for confirmation) |
| `just strategy-deploy` | **Production** Strategy Council deployment (asks for confirmation) |
| `just dashboard-deploy` | **Production** dashboard build and deployment (asks for confirmation) |
| `just dashboard-grant` | **Production** grant one identity access to the dashboard (asks for confirmation) |
| `just deploy` | **Production** deployment (asks for confirmation) |

`TYCHO_PROJECT` and `TYCHO_REGION` override the project and region variables.
The five production recipes require an interactive `yes` and fail closed
without a terminal, so they can never run from `check`, `ci`, or the default
recipe. Everything else in the table reads or runs locally.

## How acquisition works

Acquisition has one production mode: `TYCHO_DIFFER_MODE=semantic`. Every
canonical Delta, including noise, is a validated Gemini 3.7 `delta@2`; provider
or validation failure creates no Delta and remains retryable. Semantic attempts
use SQLite/Firestore generation leases and retry failed pairs before fetching
new content. The semantic differ uses Vertex/ADC, never an AI Studio API key.
The retired deterministic differ remains only for tests and audit compatibility;
it is not a production fallback or rollback mode.

## Run the strategy council locally

Three named ADK agents — `tycho_strategist`, `tycho_challenger`, and
`tycho_brief_writer` — turn governed claims into at most three present-state
market conclusions, and Python decides which of them survive.

```bash
just strategy-session   # one session end to end, one passed and one rejected card
just strategy-brief     # the same, plus the rendered brief
just strategy-stats     # what landed in the disposable store
just strategy-clean     # throw it away
```

These recipes are synthetic and offline: no Gemini call, no Google Cloud access,
and a disposable store that refuses to be `data/tycho.sqlite3`. The report lands
in `data/strategy_local_session.json`.

What the council will not do: search the web, read raw snapshots, mutate a
claim, recommend an action, or infer intent, causation, market leadership, or
the future. A conclusion needs two distinct entities and two independent source
families, where one vendor's mirrored release channels count as one family.
Confidence never exceeds the weakest premise and is never `confirmed`. A
Challenger `pass` is quality control, not evidence: it can only reject.

## Run the strategy council in production

The council runs on its own managed Agent Runtime, behind its own private
authenticated Cloud Run dispatcher, on its own weekly Cloud Scheduler job. None
of it shares a resource with the analyst path.

```bash
just strategy-plan       # what would be created, and the week a trigger resolves to
just strategy-deploy     # confirmation required; resumable and idempotent
just strategy-readback   # read every resource back from the API
```

Deployment creates nine resources in order and persists each one the moment it
exists, so a failed attempt resumes instead of creating a second Runtime. Every
step is read back from the API rather than trusted from the state file, a
resource that exists but does not match the recorded identity is a hard failure,
and nothing is ever deleted or replaced. The Runtime identity receives exactly
four roles — Firestore user, BigQuery data viewer, BigQuery job user, and trace
writer — and the readback fails closed on anything wider *or* narrower.

The tool is structurally incapable of modifying the analyst path: every
shell-out passes a guard that refuses any non-read-only command naming
`tycho-analyst-push`, the analyst dispatcher or Runtime, the acquisition job,
the nightly schedule, or the Delta topic. It reads them for before/after
evidence and can never write them.

The Scheduler job posts a static body naming a *period*, never a date range:

```json
{"trigger": "scheduler", "period": "previous_complete_week"}
```

The dispatcher resolves that to the last calendar week that entirely finished,
Monday to Monday UTC. One static body therefore yields a different, deterministic
week every Monday, a caller cannot widen the window, and two triggers inside one
week land on the same lease — so a duplicate returns the existing session with
`skipped: true` and makes no model call. Unknown fields are rejected by name, so
`prompt`, `question`, or `instructions` cannot be smuggled in and silently
ignored.

Verify a session after the fact, without writing anything:

```bash
just strategy-verify     # re-resolves every pinned premise against the store
just strategy-telemetry  # inspects every persisted trace and dispatcher log
```

`strategy-telemetry` does more than scan for forbidden field names. It pulls the
governed prose the session actually read and wrote — claim statements and
rationales, Delta change statements, grounded quotes, card text, brief prose —
out of the store and proves none of it occurs in any exported trace or log.

See `docs/strategic-agent-fleet-evidence.org` for the deployed resource IDs, the
exact IAM, the first production session, and the telemetry inspection.

## The Intelligence Dashboard

One private Cloud Run service, `tycho-dashboard`, is the fleet's read surface.
It answers four questions on one page: what changed across competitors, what
Tycho currently believes, which strategic conclusions survived challenge, and
exactly which evidence caused each conclusion or belief change.

```bash
just dashboard-install   # npm ci, pinned frontend dependencies
just dashboard-build     # tsc --noEmit && vite build
just dashboard-test      # backend suites
just dashboard-test-ui   # Vitest + Testing Library
just dashboard-serve     # local server against production read-only data
```

The browser holds no Google credential and never talks to Firestore, BigQuery,
Agent Runtime, or Pub/Sub. The API uses its own Cloud Run service account, which
is read-only on data: BigQuery data viewer, BigQuery job user, Firestore
*viewer*, and log writer, plus `roles/run.invoker` on the strategy dispatcher
service alone. It cannot write a claim or Delta, publish to Pub/Sub, read Cloud
Storage, or invoke the Analyst Runtime.

Every Delta query names the canonical `tycho.deltas` table and pins
`schema_version = 'delta@2'`; the archived audit table is unreachable from
dashboard code. Provenance resolves an exact `(claim_id, version)`: the current
version returns the live claim, an earlier version is reconstructed from the
claim's embedded history and labelled as reconstructed, and a version that never
existed is a 404. The drawer shows the grounded quote already stored in the
Delta, its observation IDs, and the source URL recorded in `tycho.yaml` — it
never fetches a raw GCS payload.

`Run Strategy Session` starts only the fixed bounded workflow, by posting
`{"trigger": "dashboard", "period": "previous_complete_week"}` to the same
private dispatcher the weekly Scheduler uses. There is no field for a prompt,
model, scope, or evidence policy. Duplicate protection has two layers: the
dashboard refuses a second run for the same period while one is in flight, and
the shared `(period_from, period_to, strategy_version)` lease returns the
existing session with `skipped: true` and no model call.

Agent activity is reconstructed from the persisted session record — agent, state,
counts, claim versions — and the page says so. Rejection reasons are reduced to
deterministic class names before they become events, so Challenger prose never
reaches the activity timeline. The full reasons are shown where they belong: in
the collapsed **Rejected by Challenger** section of the brief.

### Deploy and open it

```bash
just dashboard-plan       # what would be created; contacts nothing
just dashboard-deploy     # confirmation required; builds, then a resumable deploy
just dashboard-readback   # read every deployed resource back
just dashboard-verify     # read-only end-to-end verification of the deployed service
just dashboard-grant member=user:someone@example.com   # confirmation required
```

The service is private. An unauthenticated browser request returns `403`, which
is the expected result. Open it with your own identity:

```bash
gcloud run services proxy tycho-dashboard --region us-central1 \
  --project gen-lang-client-0110801105
# then open http://127.0.0.1:8080
```

The deployment tool reuses the analyst-path guard and adds the strategy
resources to it. Its one permitted write against a protected resource is exactly
`roles/run.invoker` on `tycho-strategy-dispatcher` for the dashboard service
account; a wider role, a different member, or a different verb is refused.

See `docs/intelligence-dashboard-evidence.org` for the deployed resource IDs,
the exact IAM, the demo sequence, and the production verification.

## Calibrate the Gemini analyst

```bash
just calibrate
```

The Python ADK analyst runs in shadow mode for meaningful local deltas. Its tool
proposals are validated and logged in SQLite, while the deterministic stub
remains authoritative locally. Cloud Run uses the same lifecycle tools in live
mode through Vertex/ADC authentication; the `.env` API key is never uploaded.
The worked-example suite covers price facts, redundancy, supersession,
cross-source fusion, no-action, third-party disputes, and primary-source dispute
resolution.

Set `TYCHO_ANALYST_MODEL` to override the default `gemini-3.5-flash-lite`. The
credential lives only in the gitignored `.env`, which is also excluded from
Cloud Run source uploads. Rotate it before sharing the project.

## Inspect production without changing it

```bash
just audit           # strict Delta@2 candidate table audit; exits nonzero on failures
just cutover-check   # cutover inventory and validation queries
```

Both read BigQuery, GCS, and Firestore only. `just audit` reloads every
candidate row through the strict Delta model, re-verifies comparison IDs,
observation identity, chronology, raw payload hashes, and canonical metadata,
rebuilds the normalized before/after bundles, and reruns the current grounding
and policy validation. Its JSON output is bounded to IDs, counts, and failure
classes; it calls no model and writes nothing.

## Deploy

```bash
just deploy          # confirmation required
just cutover-apply   # confirmation required
```

Prerequisites: a billed Google Cloud project, `gcloud`, and both CLI and
Application Default Credentials:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project PROJECT_ID
```

`infra/deploy.py` enables APIs and creates billable cloud resources: a GCS
bucket, BigQuery dataset/tables, Pub/Sub topic/subscription, Firestore database,
private Cloud Run service, Cloud Run job, Vertex AI access, service account/IAM
bindings, and a nightly Cloud Scheduler job. The Cloud Run analyst uses Gemini
through the runtime service account; no `.env` credential is uploaded.

The first cloud run establishes the immutable baseline. Production deployment
accepts only `TYCHO_DIFFER_MODE=semantic`. Operational rollback pauses the
Scheduler, preserves the failed v2 table, restores the immutable audit table
only if required, and keeps acquisition semantic-only; it never re-enables
Python Delta generation.

## Raw commands and troubleshooting

These paths have no recipe because they are one-time, argument-heavy, or
operational. Always use the module form (`python -m infra.x`), never a file
path.

Backfill local tenure into a fresh cloud project:

```bash
uv run python -m infra.backfill_local --project PROJECT_ID
```

Backfill preserves IDs, timestamps, hashes, evidence links, and claim lifecycle;
only local `file://` content references become `gs://` references.

Read-only historical replay — loads existing observation pairs from GCS, runs
the production builder/model/validators, and writes a bounded local report
without BigQuery, Pub/Sub, Firestore, or claim writes:

```bash
uv run python -m experiments.semantic_delta_replay \
  --project PROJECT_ID --output data/semantic_delta_replay.json
```

Resumable, non-publishing historical repair and the one-time claim migration:

```bash
uv run python -m infra.backfill_semantic_deltas \
  --project PROJECT_ID --dataset tycho --dry-run

uv run python -m infra.migrate_legacy_claims \
  --project PROJECT_ID --dataset tycho --dry-run
```

The old physical Delta table is preserved as `delta_audit_log_20260826`; normal
reads use only the fresh strict-v2 `tycho.deltas` table.

Inspect a deployed environment:

```bash
gcloud scheduler jobs run tycho-nightly \
  --location=us-central1 --project=PROJECT_ID

gcloud run jobs executions list \
  --job=tycho-acquire --region=us-central1 --project=PROJECT_ID

bq query --use_legacy_sql=false \
  'SELECT entity, status, fetched_at FROM `PROJECT_ID.tycho.observations` ORDER BY fetched_at DESC LIMIT 10'
```

## Documentation

- [`docs/tycho-spec.org`](docs/tycho-spec.org) — the product/data contract
- [`docs/architecture.md`](docs/architecture.md) — how it runs
- [`handoff.org`](handoff.org) — current implementation and operational state
- [`docs/strategic-agent-fleet-handoff.org`](docs/strategic-agent-fleet-handoff.org) — the strategy council brief and its split start gate
- [`docs/strategic-agent-fleet-evidence.org`](docs/strategic-agent-fleet-evidence.org) — what the local strategy implementation actually did
- [`docs/intelligence-dashboard-handoff.org`](docs/intelligence-dashboard-handoff.org) — the dashboard brief
- [`docs/intelligence-dashboard-evidence.org`](docs/intelligence-dashboard-evidence.org) — the deployed dashboard, its IAM, and its production verification
- [`docs/semantic-delta-deployment-evidence.org`](docs/semantic-delta-deployment-evidence.org)
