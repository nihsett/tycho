# Tycho command runner.
#
# `just` is the interface: every routine task is a recipe here, so nothing
# depends on remembering raw uv, pytest, Ruff, or gcloud invocations.
#
# Production project and region come from variables and can be overridden with
# the TYCHO_PROJECT / TYCHO_REGION environment variables.
#
# Recipes that change production (`cutover-apply`, `deploy`) are guarded by an
# interactive confirmation and are never reachable from `setup`, `check`, `ci`,
# or the default recipe.

project := env_var_or_default("TYCHO_PROJECT", "gen-lang-client-0110801105")
region := env_var_or_default("TYCHO_REGION", "us-central1")
python := env_var_or_default("TYCHO_PYTHON", "3.13")
packages := "adapters dashboard experiments infra pipeline platform_spike runtime_agent schemas strategy_agent tests"

# Show every available command.
default: help

# List the available commands with descriptions.
help:
    @just --list --unsorted

# --- Environment and quality gates (no cloud access) -----------------------

# Sync the locked Python 3.13 environment.
setup:
    uv sync --python {{ python }}

# Fail if uv.lock no longer matches pyproject.toml.
lock-check:
    uv lock --check

# Run Ruff over the repository.
lint:
    uv run ruff check .

# Run the pytest suite.
test:
    uv run pytest

# Byte-compile every Python package.
compile:
    uv run python -m compileall -q {{ packages }}

# Run the full local gate: lock check, lint, compile, tests.
check: lock-check lint compile test

# Noninteractive gate used by GitHub Actions.
ci: setup check

# --- Local fleet ------------------------------------------------------------

# Run one local GitHub-releases acquisition cycle.
local-github:
    uv run python -m pipeline.run_local --source github_releases

# Run one local changelog acquisition cycle.
local-web:
    uv run python -m pipeline.run_local --source website_changelog

# Print the local SQLite store counts.
local-stats:
    @uv run python -c "from pathlib import Path; from pipeline.local_backend import LocalBackend, LocalSettings; from schemas.config import load_config; store = LocalBackend(load_config('tycho.yaml'), LocalSettings(Path('data').resolve())); print(store.stats()); store.close()"

# Run the Gemini analyst calibration against the worked-example fixtures.
calibrate:
    uv run python -m pipeline.calibrate_analyst

# --- Local strategy council -------------------------------------------------
#
# Every recipe here is synthetic and offline: no Gemini call, no Google Cloud
# access, and a disposable store that is never the real local fleet database.

strategy_dir := env_var_or_default("TYCHO_STRATEGY_DIR", "data/strategy-local")

# Run one synthetic strategy session end to end (one passed, one rejected card).
strategy-session:
    uv run python -m pipeline.run_strategy_local \
        --data-dir {{ strategy_dir }} --output data/strategy_local_session.json

# Run the synthetic session and print the rendered brief markdown.
strategy-brief:
    uv run python -m pipeline.run_strategy_local \
        --data-dir {{ strategy_dir }} --output data/strategy_local_session.json \
        --print-brief

# Run only the strategy test suites.
strategy-test:
    uv run pytest tests/test_strategy_schemas.py tests/test_strategy_evidence.py \
        tests/test_strategy_context.py tests/test_strategy_citations.py \
        tests/test_strategy_council.py tests/test_strategy_persistence.py \
        tests/test_strategy_agents.py tests/test_strategy_local_run.py \
        tests/test_strategy_recovery.py tests/test_strategy_dispatcher.py \
        tests/test_strategy_deploy.py tests/test_strategy_production_checks.py

# Print the strategy sessions and briefs held in the disposable local store.
strategy-stats:
    @uv run python -c "from pathlib import Path; from pipeline.local_backend import LocalBackend, LocalSettings; from schemas.config import load_config; store = LocalBackend(load_config('tycho.yaml'), LocalSettings(Path('{{ strategy_dir }}').resolve())); print({'sessions': [(s.session_id, s.state.value, len(s.passed_cards()), len(s.rejected_cards())) for s in store.strategy_sessions()], 'briefs': [b.brief_id for b in store.briefs()]}); store.close()"

# Delete the disposable strategy store and its session report.
strategy-clean:
    rm -rf {{ strategy_dir }} data/strategy_local_session.json

# --- Intelligence dashboard -------------------------------------------------
#
# The frontend is a separate npm workspace under dashboard/frontend. Its build
# output (dist/) is what the Cloud Run service serves, so `dashboard-build` is
# a prerequisite of `dashboard-deploy`.

dashboard_dir := "dashboard/frontend"

# Install the pinned frontend dependencies from package-lock.json.
dashboard-install:
    cd {{ dashboard_dir }} && npm ci

# Type-check and build the dashboard bundle into dashboard/frontend/dist.
dashboard-build:
    cd {{ dashboard_dir }} && npm run build

# Run the frontend test suite (Vitest + Testing Library, jsdom).
dashboard-test-ui:
    cd {{ dashboard_dir }} && npm test

# Run only the dashboard backend suites.
dashboard-test:
    uv run pytest tests/test_dashboard_readmodel.py tests/test_dashboard_api.py \
        tests/test_dashboard_activity.py tests/test_dashboard_runs.py \
        tests/test_dashboard_deploy.py

# Serve the dashboard locally against production read-only data (needs ADC).
dashboard-serve port="8080":
    TYCHO_PROJECT={{ project }} \
    TYCHO_DASHBOARD_STATIC={{ dashboard_dir }}/dist \
    TYCHO_STRATEGY_DISPATCHER_URL="$(gcloud run services describe tycho-strategy-dispatcher \
        --region {{ region }} --project {{ project }} --format='value(status.url)')" \
    uv run uvicorn dashboard.api.main:app --host 127.0.0.1 --port {{ port }} --no-access-log

# Print the dashboard deployment plan. Contacts nothing.
dashboard-plan:
    uv run python -m infra.deploy_dashboard plan \
        --project {{ project }} --region {{ region }}

# Read every deployed dashboard resource back. Writes nothing.
dashboard-readback:
    uv run python -m infra.deploy_dashboard readback \
        --project {{ project }} --region {{ region }}

# Record the untouched strategy/analyst state and the data counts. Reads only.
dashboard-snapshot:
    uv run python -m infra.deploy_dashboard snapshot \
        --project {{ project }} --region {{ region }}

# Verify the deployed private dashboard end to end. Reads only.
dashboard-verify:
    uv run python -m dashboard.e2e.verify_dashboard \
        --project {{ project }} --region {{ region }}

# --- Read-only cloud inspection --------------------------------------------

# Read-only cutover inspection and validation queries. Writes nothing.
cutover-check:
    uv run python -m infra.cutover_semantic_deltas \
        --project {{ project }} --region {{ region }} --dry-run

# Read-only audit of a strict Delta@2 table. Writes nothing; calls no model.
audit table="deltas":
    uv run python -m infra.audit_semantic_candidate \
        --project {{ project }} --table {{ table }}

# Print the Strategy Council deployment plan. Contacts nothing.
strategy-plan:
    uv run python -m infra.deploy_strategy_council plan \
        --project {{ project }} --location {{ region }}

# Read every deployed Strategy Council resource back. Writes no cloud resource.
strategy-readback:
    uv run python -m infra.deploy_strategy_council readback \
        --project {{ project }} --location {{ region }}

# Record the untouched acquisition/Analyst production state. Reads only.
strategy-snapshot key="untouched_after":
    uv run python -m infra.deploy_strategy_council snapshot \
        --project {{ project }} --location {{ region }} --snapshot-key {{ key }}

# Re-verify the latest production strategy session. Reads only; calls no model.
strategy-verify:
    uv run python -m infra.verify_strategy_production session --project {{ project }}

# Re-inspect Runtime traces and dispatcher logs for leakage. Reads only.
strategy-telemetry:
    uv run python -m infra.verify_strategy_production telemetry --project {{ project }}

# --- Production (interactive confirmation required) -------------------------

# PRODUCTION: run the resumable strict Delta@2 table cutover.
cutover-apply: (_confirm "apply the resumable strict Delta@2 table cutover")
    uv run python -m infra.cutover_semantic_deltas \
        --project {{ project }} --region {{ region }} --apply --resume

# PRODUCTION: resumable Strategy Council Runtime, dispatcher, and Scheduler deploy.
strategy-deploy: (_confirm "deploy the Tycho Strategy Council production path")
    uv run python -m infra.deploy_strategy_council deploy --resume \
        --project {{ project }} --location {{ region }}

# PRODUCTION: build and deploy the private Intelligence Dashboard.
dashboard-deploy: dashboard-build (_confirm "deploy the Tycho Intelligence Dashboard")
    uv run python -m infra.deploy_dashboard deploy --resume \
        --project {{ project }} --region {{ region }}

# PRODUCTION: allow one named identity to open the private dashboard.
dashboard-grant member: (_confirm "grant dashboard access")
    uv run python -m infra.deploy_dashboard grant-viewer --member {{ member }} \
        --project {{ project }} --region {{ region }}

# PRODUCTION: deploy cloud resources and the acquisition job.
deploy: (_confirm "deploy Tycho cloud resources")
    uv run python -m infra.deploy \
        --project {{ project }} --region {{ region }}

# Shared interactive guard. Fails closed when there is no terminal, so no
# production recipe can run unattended.
_confirm action:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -t 0 ]; then
        echo "Refusing to {{ action }}: no interactive terminal." >&2
        exit 1
    fi
    printf 'About to %s\n  project: %s\n  region:  %s\nType "yes" to continue: ' \
        '{{ action }}' '{{ project }}' '{{ region }}'
    read -r reply
    if [ "$reply" != "yes" ]; then
        echo "Aborted." >&2
        exit 1
    fi
