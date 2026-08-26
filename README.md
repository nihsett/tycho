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
| `just audit` | Read-only audit of the strict Delta@2 candidate table |
| `just cutover-apply` | **Production** table cutover (asks for confirmation) |
| `just deploy` | **Production** deployment (asks for confirmation) |

`TYCHO_PROJECT` and `TYCHO_REGION` override the project and region variables.
The two production recipes require an interactive `yes` and fail closed without
a terminal, so they can never run from `check`, `ci`, or the default recipe.

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

Production deployment of the Strategy Council Runtime is still gated on the
BigQuery table swap — see `docs/strategic-agent-fleet-handoff.org`.

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
- [`docs/semantic-delta-deployment-evidence.org`](docs/semantic-delta-deployment-evidence.org)
