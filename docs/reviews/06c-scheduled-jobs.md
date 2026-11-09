# Review: Spec 06c, Scheduled jobs with Airflow

Branch `spec-06c-scheduled-jobs` (commit 6c40bd7), diffed against `main`. Reviewed on 2026-11-09.

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| DAG import tests (no syntax or import errors) in CI | pass | `uv run pytest tests/dags tests/jobs`: 30 passed, 1 skipped (the Airflow import test skips cleanly without Airflow). The CI job's exact command, run locally inside `wikipulse-airflow:3.3.2`: **17 passed**, including `test_no_import_errors` (`BundleDagBag.import_errors == {}`). The new `dags` CI job exists in `.github/workflows/ci.yml`. The coordinator checks the GitHub run itself. |
| A backfill over three past days completes in the Airflow UI | pass | Airflow metadata DB (`backfill`, `dag_run` tables): backfill 2 and backfill 3 of `daily_eval` (4 to 6 Nov, conf `{"run_id": "02b-baseline"}`) are completed, and all 3 runs ended `success`. Backfill 1 of `daily_stats` (5 to 7 Nov) completed with 5 and 6 Nov `success` and 7 Nov `failed`. That failure is correct: the recorded history ends at 13:52 UTC, which I confirmed by hand with `daily_stats check --date 2026-11-07`, exit 1, 10 missing hours. I also ran `airflow dags test daily_eval 2026-11-05 -c '{"run_id":"02b-baseline"}'` inside the container: all 3 tasks succeeded, and the result matches the learning note (73 spikes, recall 1.0%, portal precision >= 12.3%). |
| Requirement 1: tasks are idempotent and re-runnable for past dates | pass | Integration tests `test_aggregate_stores_one_row_and_rerunning_replaces_it` and `test_detection_eval_upsert_replaces_the_days_row` pass (`-m integration tests/jobs`: 5 passed). Running `evaluate` for 2026-11-05 locally and then again through Airflow left exactly one row per (run, day) in `detection_eval_daily`. |
| Requirement 2: no business logic in DAG files | pass | The DAGs only build `BashOperator`s that run `python -m wikipulse.jobs.*`. `test_dag_files_hold_no_business_logic` enforces this through an import allow-list. |
| Tests / ruff | pass | `uv run pytest`: 851 passed, 1 skipped. `ruff check .` and `ruff format --check .` are clean. |
| Secrets / email | pass | `git diff main...HEAD` contains no email address and no real secret. The JWT secret is an obvious dev-only placeholder (`wikipulse-local-dev-only-change-me`) that `AIRFLOW_JWT_SECRET` overrides. No admin password exists: `SIMPLE_AUTH_MANAGER_ALL_ADMINS=true`, and the port is bound to `127.0.0.1`. Airflow's generated files stay in `/opt/airflow` inside the container, not in the repo. `git status` was clean after the container run. |

Side effects of this review: one extra `manual__` dag run of `daily_eval` (logical date 2026-11-05) in the Airflow metadata, and the `02b-baseline`/2026-11-05 row of `detection_eval_daily` replaced with identical metrics. Both DAGs are still paused, and the Airflow container is stopped again.

## Problems

1. **medium**, `wikipulse/jobs/daily_eval.py:318` (`git_commit()` via `run_evaluate`) / `docker-compose.yml:345`: inside the Airflow container, git refuses the mounted repository ("detected dubious ownership"), so every scheduled or backfilled evaluation stores `{"commit": null, "dirty": null}`. The provenance that spec 02b's reports exist to record is silently lost; all four rows in `detection_eval_daily` written by Airflow show this.
2. **medium**, `wikipulse/jobs/daily_stats.py:443-454` and `dags/wikipulse_jobs.py:287`: unlike `daily_eval`, `daily_stats` has no "day is over" guard, and for a manual UI trigger `{{ ds }}` is the chosen logical date, not the start of the data interval. I confirmed this with `dags test`: logical date 11-05 gave the interval [11-04, 11-05) and `--date 2026-11-05`. So triggering `daily_stats` for today passes `wait_for_late_data` immediately, stores a partial-day row, and fails the check with "missing hours". The `job_task` docstring ("`{{ ds }}`: the start of the run's data interval") is wrong for manual runs.
3. **low**, `docker/airflow/requirements-jobs.txt:3`: the comment points to `tests/dags/test_airflow_image.py`, which doesn't exist; the check is `test_image_requirements_match_the_lockfile` in `tests/dags/test_dag_files.py`.
4. **low**, `wikipulse/jobs/stats.py:505-507`: `QualityRules` repeats the threshold defaults (0.3, 0.5, 7) that already live in `wikipulse/config.py`, so the two can drift; AGENTS.md wants thresholds only in config.
5. **low**, `wikipulse/db/daily.py:15`: the database layer imports from `wikipulse.jobs.stats` (`DailyStats`, `day_start`, ...), which inverts the usual db <- jobs dependency. `day_start` also duplicates `wikipulse.eval.metrics.day_start`.
6. **low**, `tests/dags/test_dag_files.py:534-542`: `test_dag_tasks_run_existing_job_modules` only checks that the string `"<step>"` appears somewhere in the module source, so a step name in a comment or log message would pass. Checking the argparse `choices` would be stronger.
7. **low**, `dags/wikipulse_jobs.py:31` and `docker/airflow/start.py:27`: the pool name `wikimedia_api` and its single slot are defined twice (DAG helper and startup script). Renaming one silently turns the "one Wikimedia client at a time" guarantee into a missing-pool scheduling error.
8. **low**, `tests/jobs/test_daily_eval.py:638`: imports the helper `load` from another test module (`tests.eval.test_ground_truth`) rather than from a shared helper, so it couples two test files.

Verdict: fix

The two medium problems are small: for example, `git config --global --add safe.directory /opt/wikipulse` in the image, or `GIT_CONFIG_*` env vars in Compose, and the `check_day_is_over` guard reused in `daily_stats` plus a corrected docstring. Everything else meets the spec and is well tested.

## Questions for the human before merging

1. A scheduled `daily_stats` run for 9 November starts after midnight on the 10th and processes the 9th. If you trigger the same DAG by hand in the UI with logical date 9 November at 14:00, which day does it process, how long does `wait_for_late_data` wait, and what will `check_quality` report?
2. `check_quality` has `retries=0` while `aggregate` has 2. After you fix a gap in `hourly_stats` for 7 November, which task(s) must you clear so the check passes, and why does clearing only `check_quality` not help?
3. Both `fetch_ground_truth` and `evaluate` are in the one-slot pool `wikimedia_api`. Why isn't `max_active_runs=1` on the DAG enough to keep Wikipedia seeing one client at a time during a backfill, and what happens to those tasks if the pool doesn't exist (for example, Airflow was started without `docker/airflow/start.py`)?
