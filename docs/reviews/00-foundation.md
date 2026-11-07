# Review: Spec 00, Foundation

Reviewed branch `spec-00-foundation` (commit `3dac06c`) against `main`.
Reviewer ran every criterion on the human's Windows 11 machine with Docker Desktop.

## Acceptance criteria

| Criterion | Result | Evidence |
|-----------|--------|----------|
| `uv sync` succeeds | pass | `Resolved 20 packages ... Checked 20 packages`; `uv sync --locked` also succeeds, so `uv.lock` matches `pyproject.toml`. |
| `docker compose up -d` starts both services, both healthy | pass | `docker compose ps`: `wikipulse-postgres-1 ... Up (healthy)`, `wikipulse-redis-1 ... Up (healthy)`. |
| `check` prints both versions | pass | `Redis: OK (version 7.4.12)`, `Postgres: OK (version 16.15)`, `All services reachable.`, exit 0. |
| `check` with services stopped fails with the helpful message | pass | After `docker compose stop`: both reported `UNREACHABLE (...)`, then `Cannot reach: Redis, Postgres. Start the services with docker compose up -d ...`, exit 1. Services restarted afterwards. |
| Placeholder User-Agent produces a policy warning (Req. 3) | pass | Both runs printed `WARNING: USER_AGENT still contains placeholder text. Wikimedia's User-Agent policy (...)`. |
| `uv run pytest` passes | pass | `4 passed in 0.26s`. |
| `uv run ruff check .` passes | pass | `All checks passed!` |
| `uv run ruff format --check .` passes | pass | `44 files already formatted`. |
| Ruff line length 100 (Req. 1) | pass | `pyproject.toml`: `line-length = 100`. |
| CI caches uv downloads (Req. 2) | pass | `astral-sh/setup-uv@v6` with `enable-cache: true`, `cache-dependency-glob: "uv.lock"`. |
| Markers `integration` and `live`, skipped by default | pass | `addopts = "-m 'not integration and not live'"` and both markers registered. |
| CI workflow file is valid | pass (by inspection) | Structure, triggers, `runs-on: ubuntu-latest` and steps are correct. I could not run a schema validator (the download via `uvx` was blocked in my environment), so this rests on reading the file plus the implementer's claim of `actionlint` and schema checks. |
| Remote configured: push and confirm the CI run is green | not verified | `origin` points to `github.com/IoakeimKyrgiafinis/wikipulse`, but `git ls-remote` shows only `main`; the branch is not pushed and `gh run list` returns no runs. The learning note says the push is left to the coordinator. |
| No real email in committed files | pass | Only `you@example.com` and the `<your email>` placeholders appear; `.env` is git-ignored and not tracked. |

## Problems

1. **medium**, `docs/specs/00-foundation.md:3`: The spec's `Status:` line still says `ready`, even though AGENTS.md ("How to work on a task", step 6, and "Definition of done") requires it to be set to `done`.
2. **medium**, `wikipulse/config.py:33` / `wikipulse/logging_setup.py:12`: `log_level` is a plain `str` with no validation, so a typo such as `LOG_LEVEL=verbose` makes `check` crash with a raw `ValueError: Unknown level: 'VERBOSE'` traceback before it prints anything, when a `Literal[...]` type would reject it with a clear settings error.
3. **medium**, `.github/workflows/ci.yml`: The acceptance criterion "push and confirm the run is green" is not met yet: a remote is configured, but the branch was never pushed and no CI run exists. This has to happen before merge.
4. **low**, `wikipulse/check.py:18`: `CONNECT_TIMEOUT_SECONDS = 5` is a tunable value hard-coded in the module, not in `wikipulse/config.py`, which AGENTS.md calls the home of "all settings".
5. **low**, `wikipulse/check.py:58`: `except Exception` labels every failure `UNREACHABLE` and suggests `docker compose up -d`, which is misleading when the service is up but the credentials in `POSTGRES_DSN` are wrong (an authentication error).
6. **low**, `wikipulse/config.py:14`: The `"example.com"` marker is a substring check, so it also flags legitimate addresses at domains that end in `example.com` (for example `me@myexample.com`). This is harmless but imprecise.
7. **low**, `.env.example:8`: The example User-Agent has the owner's real GitHub handle hard-coded, where the spec asked for `<you>`. This leaks no secret and is fine for this repository, but forks inherit a wrong URL that the placeholder check won't catch, because only the email is a placeholder.
8. **low**, `.env.example:20-25`: `POSTGRES_USER/PASSWORD/DB/PORT` and `REDIS_PORT` are repeated by hand inside `REDIS_URL` and `POSTGRES_DSN`, so changing one without the other silently breaks `check`. The comment warns about this, but nothing enforces it.
9. **low**, `tests/test_smoke.py`: No unit test covers `check.main()`'s exit code or failure message, for example by monkeypatching `redis_version` and `postgres_version` to raise. The spec doesn't require one, but this is the module's most important behavior and was only verified by hand.
10. **low**, `wikipulse/check.py:73`: The User-Agent warning and the failure message go to stdout instead of stderr. This is a nitpick for a CLI that a script might parse later.

No out-of-scope work was found: there is no stream, detection or schema code and no later-stage dependency. The runtime and dev dependencies match the spec exactly. `data/` and `.env` are git-ignored. No tests were weakened. Deviations (`print` in `check`, Alpine images, Compose defaults, and the CI push left to the coordinator) are recorded in the learning note.

Verdict: fix

The code itself is sound and all local criteria pass. Before merge: set the spec status to `done`, push and confirm a green CI run, and preferably validate `LOG_LEVEL`.

## Questions for the human before merging

1. `get_settings()` is wrapped in `@lru_cache`, but the tests construct `Settings(_env_file=None)` directly and clear environment variables with `monkeypatch`. What would go wrong in the tests if they called `get_settings()` instead, and why does the real `.env` on your machine matter here?
2. `docker-compose.yml` uses `${POSTGRES_USER:-wikipulse}` in `environment:` but `$${POSTGRES_USER}` in the health check. What does each form mean, which process expands each one, and what would break if the health check used a single `$`?
3. If you change `POSTGRES_PASSWORD` in `.env` after the `postgres-data` volume already exists, then run `docker compose up -d` and `check`, what happens and why? What is the role of the named volume in that outcome?
