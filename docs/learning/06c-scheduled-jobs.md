# Learning note: Spec 06c, Scheduled jobs with Airflow

## What was built

Two daily batch jobs, run on a schedule by Apache Airflow: the detection evaluation of
spec 02b for "yesterday", and daily statistics of the lake with data-quality checks.

| File | Role |
|------|------|
| `docker-compose.yml` | New service `airflow` (profile `airflow`): one container running `airflow standalone` (scheduler, DAG processor, triggerer, web UI/API) with the LocalExecutor. Metadata in its own database `airflow` of the existing Postgres. UI on http://localhost:8080 (loopback only). Volume `airflow-logs` for task logs. |
| `docker/airflow/Dockerfile` | `FROM apache/airflow:3.3.2-python3.12`, plus a second Python environment `/opt/wikipulse-venv` with only the packages the jobs need. |
| `docker/airflow/requirements-jobs.txt` | Those packages (httpx, psycopg, pydantic, pydantic-settings), pinned to the versions in `uv.lock`. |
| `docker/airflow/start.py` | The container's command: create the `airflow` database if missing, `airflow db migrate`, create the pool `wikimedia_api` (1 slot), then `exec airflow standalone`. All repeatable. |
| `dags/wikipulse_jobs.py` | What both DAGs share: `job_task()` (a `BashOperator` running `python -m wikipulse.jobs.<module> <step> --date {{ data_interval_start | ds }}`), `DEFAULT_ARGS` (retries, backoff, timeout, failure alert), `name_param()`, `WIKIMEDIA_POOL`. |
| `dags/daily_eval.py` | `wait_for_late_data >> fetch_ground_truth >> evaluate`. |
| `dags/daily_stats.py` | `wait_for_late_data >> aggregate >> check_quality`. |
| `wikipulse/jobs/daily_eval.py` | `fetch` (portal pages into the cache) and `evaluate` (02b's `build_report()` for one day, stored in Postgres and `data/daily_eval/<run>/<day>.md/.json`). |
| `wikipulse/jobs/stats.py` | Pure functions: `aggregate_day()`, `find_issues()`, `QualityRules`, `DailyStats`. |
| `wikipulse/jobs/daily_stats.py` | `aggregate` (hourly_stats -> daily_stats row with its issues) and `check` (exit code 1 when the stored row has issues). |
| `wikipulse/db/daily.py`, `wikipulse/db/migrations/0007_create_daily_tables.sql` | Tables `daily_stats` (key: dataset, day) and `detection_eval_daily` (key: run_id, day); upserts. |
| `wikipulse/config.py`, `.env.example` | `JOBS_EVAL_RUN_ID`, `JOBS_HOUR_DROP_RATIO`, `JOBS_DAY_DROP_RATIO`, `JOBS_BASELINE_DAYS`; Compose variables `AIRFLOW_PORT`, `AIRFLOW_DB`, `AIRFLOW_JWT_SECRET` (dev-only default). |
| `tests/jobs/` | 19 unit tests (the quality checks, the daily evaluation with saved portal pages) and 5 integration tests (real Postgres, own random dataset and run id). |
| `tests/dags/` | `test_dag_files.py` (no Airflow needed: syntax, "no business logic" import rule, every task names a real job step, image pins equal `uv.lock`) and `test_dag_import.py` (loads `dags/` with Airflow's own `BundleDagBag`; skipped without Airflow). |
| `.github/workflows/ci.yml` | New job `dags`: builds the Airflow image and runs `tests/dags` inside it. |

How it fits together:

```
                     Airflow container (airflow standalone, LocalExecutor)
                     scheduler ── metadata ──> Postgres database "airflow"
                        │ one run per UTC day D, after D ends (ds = D)
                        ▼
daily_stats:  wait_for_late_data ─> aggregate ─────────> check_quality
              (deferred, D+1 01:00)  │                     │ exit 1 = red task + ALERT log
                                     ▼                     ▼
              /opt/wikipulse-venv/bin/python -m wikipulse.jobs.daily_stats aggregate|check
                                     │ reads hourly_stats (06b), upserts daily_stats
daily_eval:   wait_for_late_data ─> fetch_ground_truth ─> evaluate      (pool wikimedia_api)
                                     │                     │
              ... -m wikipulse.jobs.daily_eval fetch       ... evaluate
                    portal pages -> data/ground_truth/     spikes + portal + labels
                                                           -> detection_eval_daily, data/daily_eval/
```

Run it:

```
uv run python -m wikipulse.db.migrate                  # tables daily_stats, detection_eval_daily
docker compose --profile airflow up -d                 # builds the image the first time
# http://localhost:8080 ; the DAGs start paused: unpause them in the UI to schedule them
docker compose exec airflow airflow backfill create --dag-id daily_eval --from-date 2026-11-04 --to-date 2026-11-06 --dag-run-conf '{\"run_id\": \"02b-baseline\"}'
#   (the \" escapes are for Windows PowerShell 5; in PowerShell 7 write '{"run_id": "02b-baseline"}')
docker compose --profile airflow stop airflow          # frees about 1 GB of RAM
```

The jobs also run without Airflow:

```
uv run python -m wikipulse.jobs.daily_stats aggregate --date 2026-11-06 --dataset wikipulse-lake/history/edits
uv run python -m wikipulse.jobs.daily_stats check --date 2026-11-06 --dataset wikipulse-lake/history/edits
uv run python -m wikipulse.jobs.daily_eval evaluate --date 2026-11-05 --run-id 02b-baseline
```

## Key concepts

### Workflow orchestration: why cron isn't enough

Cron can start a command at 00:00 every day. That is all it does. Everything else these
jobs need, cron leaves to you:

- **Dependencies.** `evaluate` must run only after `fetch_ground_truth` succeeded, and
  `check_quality` only after `aggregate`. In Airflow that is one line per DAG
  (`wait_for_late_data >> fetch_ground_truth >> evaluate`), and a failed task stops the
  tasks after it (they show as "upstream_failed").
- **Retries.** `DEFAULT_ARGS` in `dags/wikipulse_jobs.py` gives every task 2 retries
  with exponential backoff (5 min, 10 min, ... at most 1 h). `fetch_ground_truth` gets 4
  retries starting at 10 min, because it depends on Wikipedia. `check_quality` gets
  **none**: a missing hour doesn't appear because you ask again. Retry what is
  temporary, never what is deterministic.
- **History and state.** Airflow stores every run and every task attempt with its log
  (in its metadata database and the `airflow-logs` volume). "Did the job of 6 November
  run, and what did it print?" has an answer. With cron it's a log file, if you
  redirected one.
- **Backfills.** "Run this job for 4, 5 and 6 November" is one command or one form in the
  UI. Airflow creates a run per day, each with its own `ds`.
- **Concurrency limits.** `max_active_runs=1` per DAG, and the **pool** `wikimedia_api`
  with one slot (created by `docker/airflow/start.py`). A pool caps tasks *across all
  DAGs and backfills*: when a scheduled run and a 3-day backfill were active together,
  the six Wikipedia tasks still ran strictly one after another (see Results). That is
  how the "one request at a time" rule of AGENTS.md survives parallel runs.

A **DAG** (directed acyclic graph) is the set of tasks and their order. Here both DAGs
are simple chains of three tasks. The DAG files say *when* and *in which order*; all the
*what* is in `wikipulse/jobs/` (requirement 2, checked by
`test_dag_files_hold_no_business_logic`).

### Time in a scheduler: data intervals, `ds`, and idempotent tasks

Each run is *for* a stretch of time, its **data interval**. The DAGs use
`CronDataIntervalTimetable("0 0 * * *", timezone="UTC")`: the run for 6 November covers
[6 Nov 00:00, 7 Nov 00:00), starts after the interval is over, and the start of the
interval, as `YYYY-MM-DD`, is `2026-11-06`. Every task gets the day as `--date "$DAY"`
with `DAY = {{ data_interval_start | ds }}` (`DAY_TEMPLATE` in `dags/wikipulse_jobs.py`).
Not `{{ ds }}`: that is the run's *logical date*, which equals the interval start only
for scheduled runs. A manual run triggered "for 9 November, 14:00" gets the last
complete interval before that moment, [8 Nov, 9 Nov), so `ds` would be the 9th (a day
still going on) while the interval start is the 8th, a finished day. This is the same idea as the project's "event time, not
wall-clock time" rule: the job processes *the day it is given*, never "whatever today
is", so the run for 6 November gives the same answer whether it runs on the 7th or a
month later as a backfill.

Airflow 3 changed the default for cron schedules to "trigger" timetables, where a run
has no interval and its logical date is the moment it runs; then "yesterday" has to be
computed in the task. The explicit data-interval timetable keeps "a run is for one day"
visible, which is what backfills are about.

**Idempotence** (requirement 1) means running a task twice leaves the same result as
running it once. Both jobs only *replace* rows keyed by the day:
`INSERT ... ON CONFLICT (dataset, day) DO UPDATE` in `wikipulse/db/daily.py`, and the
report files are named by run and day (`data/daily_eval/<run>/<day>.md`), not by the time
they were made. So a retry after a crash halfway, a "clear task" in the UI, or a backfill
over days that already ran, all leave exactly one correct row per day
(`test_aggregate_stores_one_row_and_rerunning_replaces_it`). The second backfill below
re-ran three completed days: still 3 rows.

Re-running is also how late data is handled: if hours 22 and 23 arrive after the run,
the check fails; once they are in, clearing the day's tasks recomputes it and the check
passes (`test_a_later_rerun_sees_hours_that_arrived_late`). The portal pages change for
a few days after their day, so re-running a daily evaluation later refreshes a
provisional result in the same way.

### Batch alongside streaming

The live system (stage 1 to 6b) handles every edit as it comes; the nightly jobs look at
a finished day. They share data, not code paths:

- the 06b worker writes `hourly_stats` within seconds of each lake write (streaming);
  `daily_stats` reads those rows once the day is over (batch);
- the live detector writes `spikes`; `daily_eval` scores a finished day of them against
  the portal.

The `wait_for_late_data` sensor is where the two meet. A day's last hour reaches the lake
up to about 5 minutes after the hour (the archiver's flush interval), and a spike ends
30 minutes after its last edit; so both DAGs wait until an hour after the day ended.
`TimeDeltaSensor(..., deferrable=True)` hands the wait to the **triggerer** instead of
keeping a worker process busy. For a past day (a backfill) the time has long passed and
the sensor succeeds at once.

### Operational thinking: the job fails at 3 a.m.

What happens, step by step, with this setup:

1. A temporary problem (Postgres restarting, Wikipedia timing out): the task fails, is
   retried after 5 (or 10) minutes, and usually succeeds. Nobody needs to wake up.
2. A real problem: after the last retry the task is **failed** (red in the grid view),
   the tasks after it are not run, and `alert_on_failure()` writes an `ALERT:` line into
   the task log saying what failed, for which day, and what to do. In production this
   callback would send the alert somewhere a human sees it (email, chat, a pager);
   stage 9 adds Prometheus and Grafana alerts.
3. A data-quality problem: `check_quality` fails at once (no retries), while `aggregate`
   stays green: the numbers are stored (look at them in `daily_stats`), and the red task
   says they can't be trusted. The log lists exactly which hours are missing.
4. In the morning: fix the cause, then **clear** the failed task (or the whole run) in
   the UI; Airflow runs it again. Because tasks are idempotent, clearing is always safe.
   Many days broken: a backfill with `--reprocess-behavior failed`.

`execution_timeout` (30 min) and `dagrun_timeout` (6 h) make sure a *stuck* job fails too
instead of silently blocking the next days.

## Design decisions and deviations from the spec

- **Airflow, not Argo Workflows** (AGENTS.md allows either; Argo needs Kubernetes, which
  arrives in stage 8). Version **3.3.2**, the latest patch release of the line before the
  brand-new 3.4.0, pinned in the Dockerfile (`apache/airflow:3.3.2-python3.12`).
- **Airflow runs in Docker, not in the project's Python environment.** Airflow pins
  hundreds of packages (SQLAlchemy, FastAPI, pydantic, ...) and would bloat `uv.lock` and
  conflict with the project's versions. Instead the image has *two* environments:
  Airflow's own, which parses the DAGs and never imports `wikipulse`, and
  `/opt/wikipulse-venv`, which runs the jobs. A unit test
  (`test_image_requirements_match_the_lockfile`) keeps the four pinned packages equal to
  `uv.lock`. The venv holds only what `wikipulse.jobs` imports (no torch, no Neo4j), so
  it adds about 70 MB to the 3.2 GB official image. Transitive dependencies (e.g.
  `pydantic-core`) are resolved by pip, not pinned; pydantic pins its core exactly.
- **The repository is mounted, not copied, into the container** (`.:/opt/wikipulse`).
  Changes to DAGs and jobs apply without rebuilding, tasks read the same `.env` (so the
  same User-Agent) and write into the same `data/`. Compose overrides only the
  addresses that differ inside Docker (`POSTGRES_DSN`, `REDIS_URL`, `S3_ENDPOINT` with the
  service names, and `DATA_DIR`); environment variables beat `.env` in pydantic-settings.
- **Tasks are `BashOperator`s running `python -m wikipulse.jobs...`**, not Python
  functions inside Airflow. Each job is an ordinary command you can run by hand, a
  task's exit code is its result, and the scheduler and the jobs share no Python
  packages. Values from the UI (the `run_id` and `dataset` parameters) reach the command
  as environment variables (`"$RUN_ID"`), never pasted into the shell line, and are
  limited to a safe pattern (`NAME_PATTERN`).
- **Light single-container setup**: `airflow standalone` with the LocalExecutor,
  `PARALLELISM=2`, one API worker, no example DAGs. It used about **1.1 to 1.2 GB of RAM**.
  The other services kept running (neo4j-eval did not need to be stopped). The service
  is behind the Compose profile `airflow`, so a plain `docker compose up -d` doesn't
  start it.
- **Metadata in a separate database** `airflow` in the existing Postgres server (as the
  spec asks), created by `start.py` because Postgres's init scripts only run on an empty
  volume, and this one already holds WikiPulse's data.
- **Login**: `SIMPLE_AUTH_MANAGER_ALL_ADMINS=true`, so the UI has no login form. That is
  only acceptable because the port is bound to `127.0.0.1`. `AIRFLOW__API_AUTH__JWT_SECRET`
  is set from `AIRFLOW_JWT_SECRET` with a dev-only default, so all of Airflow's components
  sign their internal tokens with the same key across restarts.
- **The DAGs start paused** (Airflow's default) and were paused again after the
  acceptance runs. The live pipeline doesn't run all the time on this laptop, so an
  unpaused `daily_stats` would fail every night with 24 missing hours. That is correct,
  but noisy. Unpause both DAGs in the UI once the live pipeline runs continuously.
- **Where results go.** The spec says "store results". The daily evaluation is stored
  in Postgres (`detection_eval_daily`: headline `metrics` and the full `report` as JSONB),
  so results can be trended with SQL, and as Markdown/JSON under `data/daily_eval/`. It is
  *not* stored under `evals/results/` (committed): a file per day would flood git.
- **The daily evaluation reuses 02b unchanged** (`build_report`, `load_portal`,
  `render_markdown`, `config_snapshot` from `wikipulse/eval/detection.py`). Its label
  precision uses a random sample of *that day's* spikes (seed 2026), so it only partly
  overlaps the 02b labels (37 of 50 labeled for 5 and 6 November). No LLM labeling runs
  nightly; that would be its own, GPU-heavy task.
- **"Yesterday"**: the scheduled run for day D starts after D ends and evaluates D.
  Spikes of D are matched against portal pages D-2 to D+2, and pages that don't exist yet
  count as missing (`portal_missing` in the table). The evaluation is therefore
  provisional for a few days, and a later re-run refreshes it.
  `fetch_ground_truth` fails (and retries) if day D's own page is missing. Both steps
  refuse a day that isn't over yet (`DayNotOverError`). Comparing with the wall clock is
  only a guard: it never changes the results.
- **Quality rules** (`wikipulse/jobs/stats.py`, thresholds in config):
  - *missing hours*: any hour of the UTC day without an `hourly_stats` row;
  - *hour drop*: an hour below 30% of the day's median hour. In the archived history the
    quietest hour was at least 57% of the median, so 30% leaves room for the daily rhythm;
  - *day drop*: a day below 50% of the mean of the earlier *complete* days (up to 7).
    These are read straight from `hourly_stats`, not from earlier `daily_stats` rows, so
    the result doesn't depend on the order in which days are backfilled.
- **What `daily_stats` holds**: totals that can be added up honestly (edits, bot edits,
  bot share, min/median/max hour, peak hour, hours present). There are no daily
  distinct pages or editors and no daily top pages: summing hourly *distinct* counts
  counts a page edited in two hours twice. Computing them correctly would mean reading
  the lake (DuckDB) instead of aggregating `hourly_stats` as the spec says.
- **Split into `aggregate` and `check_quality`**: the numbers are stored even on a bad
  day, and the failure is a separate red task that can be re-checked alone.
- **DAG import tests in CI**: the project's own CI job can't import Airflow (it isn't
  installed), so `tests/dags/test_dag_import.py` skips there cleanly. A new CI job `dags`
  builds the Airflow image and runs `tests/dags` inside it. It installs pytest at run
  time and starts no Airflow server or database. Locally (PowerShell):
  `docker run --rm -v "${PWD}:/src:ro" --entrypoint bash wikipulse-airflow:3.3.2 -c "pip install -q --user pytest==9.2.0 && cd /src && python -m pytest tests/dags -p no:cacheprovider -o addopts=''"`.
  The CI job itself has not run yet; it runs on the next push.
- **`BundleDagBag`, not `DagBag`, in the test**: in Airflow 3 the DAG processor puts the
  DAG folder on `sys.path` through the bundle. Plain `DagBag` doesn't, so the shared helper
  `wikipulse_jobs` couldn't be imported.
- **Side effects on the dev database**: unpausing the DAGs for the backfills also
  created each DAG's latest scheduled run (for 8 November, live data). `daily_stats` for
  `wikipulse-lake/edits` on 8 November failed its check (0 of 24 hours: the archiver
  wasn't running), and `detection_eval_daily` has a `live` row for 8 November with 0
  spikes. Both are true statements about that day, left in place.

## Results

All on 2026-11-09, on the human's Windows machine, with the Airflow container next to
Redis, Postgres, both Neo4j servers and RustFS.

### Acceptance criterion 1: DAG import tests (no syntax or import errors) in CI

- `uv run pytest tests/dags`: 11 passed, 1 skipped (the Airflow import test, as designed).
  These run in the existing CI job.
- Inside the Airflow image (the command of the new CI job `dags`): **17 passed**. Airflow's
  `BundleDagBag` loads both DAGs with no import errors. The tests also check the task
  chains, the retry settings, the pool, and how the commands receive the day.
- The running Airflow lists both DAGs, and `airflow dags list-import-errors` prints "No data found".

### Acceptance criterion 2: a backfill over three past days completes

Checked through Airflow's REST API (`/api/v2/backfills`, `/api/v2/dags/.../dagRuns`),
the API the UI itself reads, and with `airflow dags list-runs`. I could not click through
the UI. It answered HTTP 200 on http://localhost:8080; open the grid view of each DAG to
see the runs.

- **`daily_eval`, 4 to 6 November**, detector run `02b-baseline`
  (`--dag-run-conf '{"run_id": "02b-baseline"}'`): backfill 2 **completed, 3 of 3 runs
  success**, about 25 s in all. Backfill 3 re-ran the same days
  (`--reprocess-behavior completed --max-active-runs 3`): again 3 of 3 success, and
  `detection_eval_daily` still has exactly 3 rows for the run (replaced, not added). The
  three `fetch_ground_truth` and three `evaluate` tasks ran strictly one after another,
  although three runs were active: the one-slot pool at work.

  | Day | Spikes | Recall (all) | Recall (lead) | Portal precision (lower bound) | Label precision (labeled) | Time to detection, median |
  |---|---:|---:|---:|---:|---:|---:|
  | 4 Nov (from 12:00 only) | 44 | 0.0% | 0.0% | 13.6% | n/a (0) | 9.8 min |
  | 5 Nov | 73 | 1.0% | 0.0% | 12.3% | 51.4% (37) | 9.1 min |
  | 6 Nov | 62 | 1.5% | 0.0% | 1.6% | 51.4% (37) | 9.8 min |

  These match the 02b picture: very low recall, a low portal-precision floor, and about
  half of the labeled spikes being news. Per day, portal precision swings a lot (12% vs
  2%) on samples of 60 to 70 spikes; the two-day pooled number in 02b was 7.4%.
- **`daily_stats`, 5 to 7 November**, dataset `wikipulse-lake/history/edits` (the
  recorded history from 06a; the live archive holds only 3 hours): backfill 1
  **completed** in about 15 s. 5 and 6 November **success** (109,579 and 127,199 edits,
  24 of 24 hours, no issues; 6 November is 116% of the 5 November baseline).
  **7 November failed in `check_quality`**, as it should. The recordings end at 13:52
  UTC, so the log says `missing_hours: 10 of 24 hours have no statistics: 14:00, ...,
  23:00 UTC`, followed by the `ALERT:` line. `aggregate` stayed green and stored the
  numbers (73,006 edits in 14 hours). This is the spec's "data quality failures fail the
  task visibly", on real data. There aren't three *complete* days of lake data yet, so a
  fully green three-day `daily_stats` backfill wasn't possible without faking data.

### Tests and checks

- `uv run pytest`: 851 passed, 1 skipped (Airflow import test). `tests/jobs` 19 and
  `tests/dags` 11 of them are new.
- `uv run pytest -m integration`: 65 passed, 1 skipped (5 new in
  `tests/jobs/test_jobs_db.py`, own random dataset and run id, deleted afterwards).
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- The Airflow container used 1.2 GB of RAM; the image is 3.3 GB on disk (3.2 GB of it is the
  official image).

### Look at it yourself (optional)

- Grid view of `daily_stats`: the red `check_quality` square of 7 November. Open its
  log for the missing hours and the `ALERT:` line.
- Break a task on purpose: add `raise RuntimeError("broken on purpose")` as the first
  line of `run_aggregate()` in `wikipulse/jobs/daily_stats.py` (the repository is
  mounted, so the next task run sees it). Then clear the `aggregate` task of 6 November
  in the UI and watch it go to "up_for_retry", wait 5 and then 10 minutes, and fail for
  good with the `ALERT:` line. Remove the line, clear again, and it turns green. (Don't
  stop Postgres for this: Airflow's own metadata lives there too.)
- `SELECT day, metrics->>'recall_lead', metrics->>'portal_precision' FROM
  detection_eval_daily WHERE run_id = 'live' ORDER BY day;` turns into a trend line
  once the live pipeline runs for some days with the DAGs unpaused.

## Check your understanding

1. The scheduled run of `daily_stats` for 9 November starts at 10 November 00:00 UTC.
   Why does it compute 9 November and not 10 November, and why does that matter for
   backfills?
2. `aggregate` has 2 retries but `check_quality` has none. Why is that the right way
   round?
3. During a backfill of five days, `daily_eval` has several runs active, but Wikipedia
   only ever sees one request at a time. Which setting does that, and why wouldn't
   `max_active_runs=1` on the DAG be enough?

<details>
<summary>Answers</summary>

1. The DAG uses a data-interval timetable: the run *for* 9 November covers
   [9 Nov 00:00, 10 Nov 00:00) and can only start once that interval is over. Its `ds`
   is `2026-11-09`, and the jobs get that as `--date`. A backfill creates exactly these
   runs for past intervals, so "backfill 4 to 6 November" processes those three days,
   and the result for a day never depends on *when* the run happens.
2. Retries help with temporary problems: a database restart or a network error during
   `aggregate` is likely gone five minutes later. A failed quality check is about the
   data. Asking again gives the same answer and only delays the alert. It should fail
   at once and visibly. After someone fixes the data, they clear the task.
3. The pool `wikimedia_api` with one slot. Pools count running tasks across *all* runs
   and DAGs. `max_active_runs=1` limits only one DAG's own runs, and a backfill brings
   its own `max_active_runs` (it was 3 in the re-run) next to the scheduled runs. In
   this project's run, three active runs still executed their six Wikipedia tasks one
   after another.

</details>

## Review fixes

Review `docs/reviews/06c-scheduled-jobs.md`, problems 1, 2, 3 and 7:

- The image runs `git config --system --add safe.directory /opt/wikipulse`, so
  evaluations in the container record the commit again. Checked: `git_commit()` in the
  container returned `6c40bd7...`, and the `02b-baseline`/5 Nov row of
  `detection_eval_daily` written by `airflow dags test daily_eval 2026-11-06` has it
  (same metrics as before: 73 spikes, recall 1.0%).
- Tasks get the day as `{{ data_interval_start | ds }}` instead of `{{ ds }}`, so a
  manual trigger never processes an unfinished day. `airflow dags test daily_stats
  2026-11-07` processed the 6th (127,199 edits, 24 hours, check passed with the
  `wikipulse-lake/history/edits` dataset). `daily_stats aggregate` itself now also
  refuses a day until an hour after it ended (`check_day_is_complete`,
  `LATE_DATA_WAIT`; tests in `tests/jobs/test_daily_stats.py`), so a hand-run command
  can't store a partial day either.
- The pool is defined once, in `POOLS` of `dags/wikipulse_jobs.py`, which
  `docker/airflow/start.py` imports; the comment in `requirements-jobs.txt` names the
  right test. New tests in `tests/dags` check the day template, the scheduled and manual
  data intervals, and that every pool a task uses is created at start.
