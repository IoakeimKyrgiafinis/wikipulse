# Spec 00: Foundation

Status: done
Stage: 0. Foundation
Owner: agent
Depends on: none

## Goal
A working project skeleton with services, configuration, tests and CI from the very first commit, so every later stage starts on solid ground.

## What you'll learn
- **Project structure and dependency management** with uv: read every generated file during review and make sure you could explain each line of `pyproject.toml`.
- **Configuration management:** why settings live in one place and come from the environment (the "twelve-factor app" idea; worth a 10-minute read).
- **Containers and health checks:** run `docker compose ps` and `docker compose logs`, stop a service, and watch the check fail.
- **CI basics:** read the workflow file line by line, then break a test on purpose, push, and watch CI fail. That's the moment CI makes sense.

## Context
The repo currently contains only AGENTS.md, CLAUDE.md, docs and the `.claude` folder.

## Outputs
- `pyproject.toml` (uv, Python 3.12) and `uv.lock`. Runtime dependencies: `pydantic`, `pydantic-settings`, `redis`, `psycopg[binary]`. Dev dependencies: `pytest`, `pytest-asyncio`, `ruff`.
- `wikipulse/__init__.py`
- `wikipulse/config.py`: a `Settings` class (pydantic-settings) reading environment variables and `.env`, with `get_settings()`. Settings for now: `REDIS_URL`, `POSTGRES_DSN`, `USER_AGENT`, `LOG_LEVEL`, `DATA_DIR=data`.
- `wikipulse/logging_setup.py`: one function configuring logging from `LOG_LEVEL`
- `wikipulse/check.py`: `uv run python -m wikipulse.check` connects to Redis and Postgres and prints their versions; on failure, prints which service is unreachable, suggests `docker compose up -d`, and exits non-zero
- `docker-compose.yml`: Redis 7 (with append-only persistence) and Postgres 16, each with a named volume and a health check; credentials from `.env`
- `.env.example`, with `USER_AGENT` shown as `WikiPulse/0.1 (https://github.com/<you>/wikipulse; <your email>)`
- `.github/workflows/ci.yml`: on every push and pull request, on `ubuntu-latest`: install uv, `uv sync`, `ruff check`, `ruff format --check`, `pytest`
- pytest configuration in `pyproject.toml`: markers `integration` and `live`, both skipped by default
- `tests/test_smoke.py`: the package imports and settings load with defaults
- `README.md`: a placeholder description and setup steps (install Git, uv and Docker Desktop; copy `.env.example` to `.env`; `uv sync`; `docker compose up -d`; run the check; run the tests)

## Requirements
1. Ruff configured with a line length of 100.
2. The CI workflow caches uv's downloads, so runs are fast.
3. `USER_AGENT` has no working default: if it still contains the placeholder text, `check` prints a warning explaining Wikimedia's User-Agent policy.

## Acceptance criteria
- `uv sync` succeeds.
- `docker compose up -d` starts both services, and both report healthy.
- `uv run python -m wikipulse.check` prints both versions; with the services stopped, it fails with the helpful message.
- `uv run pytest`, `uv run ruff check .` and `uv run ruff format --check .` pass.
- The CI workflow file is valid. If a GitHub remote is configured, push and confirm the run is green (`gh run watch` if the GitHub CLI is available).

## Look at it yourself (optional, never blocking)
- Read every file; this skeleton shapes the whole project.
- Create the GitHub repository, push, and confirm the CI run is green in the Actions tab.
- Confirm `.env` is not committed.

## Out of scope
- Any stream, detection or database-schema code
- Neo4j and every later-stage tool
