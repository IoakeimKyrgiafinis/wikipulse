# Learning note: Spec 00, Foundation

## What was built

A project skeleton that every later stage builds on:

| File | Role |
|------|------|
| `pyproject.toml`, `uv.lock`, `.python-version` | Project metadata, dependencies, and tool settings (ruff, pytest). uv reads these. |
| `wikipulse/config.py` | The single place where settings live: `Settings` and `get_settings()`. |
| `wikipulse/logging_setup.py` | `setup_logging()` configures the `logging` module from `LOG_LEVEL`. |
| `wikipulse/check.py` | `uv run python -m wikipulse.check`: connects to Redis and Postgres and prints their versions. |
| `docker-compose.yml` | Redis 7 and Postgres 16, each with a named volume and a health check. |
| `.env.example` | Every setting, documented. You copy it to `.env` (which is never committed). |
| `.github/workflows/ci.yml` | On every push and pull request: install uv, `uv sync`, ruff lint, ruff format check, pytest. |
| `tests/test_smoke.py` | Checks that the package imports and that settings load with defaults and from the environment. |
| `README.md` | Setup steps. |

How the pieces fit: Docker Compose starts the services, reading credentials from `.env`.
The Python code reads the *same* `.env` (through `Settings`) to learn where those services are.
`wikipulse.check` ties both together. If it prints two versions, the machine is ready for stage 1.
CI runs the same lint and test commands you run locally, on a clean Linux machine.

**Action for you:** `.env` was created from `.env.example` and still has the placeholder contact
`you@example.com`. Open `.env` and set `USER_AGENT` to your own GitHub URL and email before
stage 1 talks to Wikimedia. The committed files use only the placeholder. Your real address
belongs only in `.env`, which git ignores.

## Key concepts

### Project structure and dependency management with uv

`pyproject.toml`, section by section:

- `[project]`: name, version, and `requires-python = ">=3.12,<3.13"`. The upper bound pins
  the project to 3.12 as the spec asks. Your machine has Python 3.14 installed, but uv
  downloads and uses 3.12 for this project automatically (`.python-version` says `3.12`).
- `dependencies`: the four runtime libraries. `pydantic` validates data, `pydantic-settings`
  loads settings from the environment, `redis` is the Redis client, and `psycopg[binary]` is
  the Postgres client (the `[binary]` extra ships a precompiled C library, so nothing has to be
  compiled on Windows).
- `[dependency-groups] dev`: tools that are needed for development and not at runtime (`pytest`,
  `pytest-asyncio`, `ruff`). `uv sync` installs them by default.
- `[build-system]` with hatchling: makes `wikipulse` an installable package, so
  `import wikipulse` works from anywhere (tests included) without editing `sys.path`.
- `[tool.ruff]`: line length 100, plus a lint rule set: pyflakes/pycodestyle (`E`, `F`, `W`),
  import sorting (`I`), common bug patterns (`B`) and "use modern syntax" (`UP`).
- `[tool.pytest.ini_options]`: registers the `integration` and `live` markers, and
  `addopts = "-m 'not integration and not live'"` skips them by default. A `-m` you type on the
  command line comes later and wins, so `uv run pytest -m integration` runs only those tests.

`uv.lock` records the exact version of every package, including indirect ones. `uv sync` installs
exactly those versions, so your machine and CI run the same code. CI uses `uv sync --locked`,
which fails if `pyproject.toml` and `uv.lock` disagree instead of quietly updating the lock.

### Configuration management (twelve-factor)

The twelve-factor app idea is that config (anything that differs between your laptop, CI and
production) lives in the environment and not in the code. In `wikipulse/config.py`:

- `Settings(BaseSettings)` declares each setting as a typed field: `redis_url`, `postgres_dsn`,
  `user_agent`, `log_level` and `data_dir` (a `Path`). pydantic-settings fills each field from
  the matching environment variable (`REDIS_URL` and so on), then from `.env`, then from the default.
- `extra="ignore"`: `.env` also contains Compose-only values such as `POSTGRES_PASSWORD`, and
  `Settings` ignores them instead of raising an error.
- `get_settings()` is wrapped in `@lru_cache`, so the settings are read once per process and
  every module sees the same object.
- Tests build `Settings(_env_file=None)` with a clean environment, so a developer's local
  `.env` can't change the test results.

### Containers and health checks

A container being "running" doesn't mean the service inside is ready. Postgres needs a few seconds
to initialise. Each service in `docker-compose.yml` therefore has a `healthcheck`:
`redis-cli ping` for Redis and `pg_isready` for Postgres. `docker compose ps` shows `(healthy)`
only once that command succeeds. Named volumes (`redis-data`, `postgres-data`) keep the data
when containers are recreated. Redis runs with `--appendonly yes`, so every write is logged to
disk and survives a restart. That matters once stage 1 stores the edit stream in Redis.

Try it yourself: `docker compose stop redis`, then `uv run python -m wikipulse.check`. It
reports `Redis: UNREACHABLE`, suggests `docker compose up -d`, and exits with code 1.

### CI basics

`.github/workflows/ci.yml` runs on `push` and `pull_request` on `ubuntu-latest`.
`astral-sh/setup-uv` installs uv, and `enable-cache: true` saves uv's download cache between
runs, keyed on `uv.lock`, so packages are only downloaded again when the lock changes. The steps
then mirror what you run locally. Any step that exits non-zero turns the run red.

## Decisions and deviations

- **No real email anywhere committed.** `.env.example` uses
  `WikiPulse/0.1 (https://github.com/IoakeimKyrgiafinis/wikipulse; you@example.com)`, and the
  code default is the spec's `<you>` / `<your email>` template. `Settings.user_agent_is_placeholder()`
  treats `<you>`, `<your email>` and `example.com` as placeholder text, so `check` warns until
  you set a real contact in `.env`.
- **`check` uses `print`.** AGENTS.md says to log rather than print inside the package. `check`
  is a command-line tool whose output *is* its result (the spec says it "prints"), so it prints
  the report and uses `logging` only for debug details (the full traceback at `DEBUG` level).
- **Compose credentials have defaults** (`${POSTGRES_USER:-wikipulse}`), so `docker compose up -d`
  works even before `.env` exists. The values still come from `.env` when it is present.
- **Connect timeouts of 5 seconds** in `check`, so a stopped service fails quickly instead of hanging.
- **Alpine images** (`redis:7-alpine`, `postgres:16-alpine`) are smaller downloads. The versions
  are the same.
- **pytest-asyncio `asyncio_mode = "auto"`**: later async tests won't each need a decorator.
- **CI not pushed by the implementer.** The coordinator handles commits and pushes, so the
  "push and watch the run go green" step is left to them. The workflow was validated locally
  with `actionlint` and the GitHub workflow JSON schema (both run with `uvx`, nothing installed
  globally).
- **`LOG_LEVEL` is validated (review fix).** `Settings.log_level` is now a `Literal` of the five
  standard levels, with a "before" validator that upper-cases the input (so `debug` works).
  A typo like `LOG_LEVEL=verbose` used to crash `check` with a raw `ValueError` traceback; now
  `check.main()` catches the pydantic `ValidationError`, prints "Invalid settings ..." with the
  allowed values to stderr, and exits with code 2. Covered by three new tests in
  `tests/test_smoke.py`.

## Results

- `uv sync`: succeeded (Python 3.12, 20 packages resolved).
- `docker compose up -d`: both containers reported `(healthy)` within about 6 seconds.
- `uv run python -m wikipulse.check` with services up printed `Redis: OK (version 7.4.12)`,
  `Postgres: OK (version 16.15)` and exited 0. With services stopped, it printed both as
  UNREACHABLE plus the `docker compose up -d` suggestion and exited 1. The placeholder
  User-Agent warning appeared both times, as expected.
- `uv run pytest`: 4 passed (7 after the LOG_LEVEL review fix). `ruff check` and `ruff format --check`: clean.

## Check your understanding

1. Why does `tests/test_smoke.py` build `Settings(_env_file=None)` instead of calling `get_settings()`?
2. A container shows `Up 3 seconds` but `check` can't connect to Postgres yet. Why, and what
   in `docker-compose.yml` tells you when it is actually ready?
3. CI runs `uv sync --locked`. What happens if you add a dependency to `pyproject.toml` but
   forget to commit the updated `uv.lock`?

<details>
<summary>Answers</summary>

1. `get_settings()` reads your local `.env` and caches the result for the whole process. A test
   that depends on your personal `.env` might pass on your machine and fail in CI. Passing
   `_env_file=None` with a cleaned environment makes the test see only the defaults, every time.
2. "Up" only means the container process started. Postgres is still initialising. The
   `healthcheck` (`pg_isready`) runs every 5 seconds, and `docker compose ps` shows `(healthy)`
   once it succeeds.
3. `uv sync --locked` sees that the lock file no longer matches `pyproject.toml` and fails, so
   the CI run goes red. Without `--locked`, CI would quietly resolve new versions and could test
   different code from what you run locally.

</details>
