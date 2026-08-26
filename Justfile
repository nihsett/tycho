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
packages := "adapters experiments infra pipeline platform_spike runtime_agent schemas strategy_agent tests"

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
        tests/test_strategy_agents.py tests/test_strategy_local_run.py

# Print the strategy sessions and briefs held in the disposable local store.
strategy-stats:
    @uv run python -c "from pathlib import Path; from pipeline.local_backend import LocalBackend, LocalSettings; from schemas.config import load_config; store = LocalBackend(load_config('tycho.yaml'), LocalSettings(Path('{{ strategy_dir }}').resolve())); print({'sessions': [(s.session_id, s.state.value, len(s.passed_cards()), len(s.rejected_cards())) for s in store.strategy_sessions()], 'briefs': [b.brief_id for b in store.briefs()]}); store.close()"

# Delete the disposable strategy store and its session report.
strategy-clean:
    rm -rf {{ strategy_dir }} data/strategy_local_session.json

# --- Read-only cloud inspection --------------------------------------------

# Read-only cutover inspection and validation queries. Writes nothing.
cutover-check:
    uv run python -m infra.cutover_semantic_deltas \
        --project {{ project }} --region {{ region }} --dry-run

# Read-only audit of a strict Delta@2 table. Writes nothing; calls no model.
audit table="deltas_v2_candidate":
    uv run python -m infra.audit_semantic_candidate \
        --project {{ project }} --table {{ table }}

# --- Production (interactive confirmation required) -------------------------

# PRODUCTION: run the resumable strict Delta@2 table cutover.
cutover-apply: (_confirm "apply the resumable strict Delta@2 table cutover")
    uv run python -m infra.cutover_semantic_deltas \
        --project {{ project }} --region {{ region }} --apply --resume

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
