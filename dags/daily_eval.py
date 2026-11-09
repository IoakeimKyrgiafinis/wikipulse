"""daily_eval: yesterday's spikes against yesterday's news.

    wait_for_late_data  >>  fetch_ground_truth  >>  evaluate

- `wait_for_late_data` waits until an hour after the day ended (a spike ends 30 minutes
  after its last edit, and the detector writes spikes every 30 seconds).
- `fetch_ground_truth` downloads the day's "Current events" portal page and its
  neighbours into the cache (`python -m wikipulse.jobs.daily_eval fetch`). It talks to
  Wikipedia, so it gets more retries, with growing pauses.
- `evaluate` runs the detection evaluation of spec 02b for that day and stores it in
  Postgres (`detection_eval_daily`) and `data/daily_eval/` (`... daily_eval evaluate`).

All the logic is in `wikipulse.jobs.daily_eval`. Both job tasks share the one-slot pool
`wikimedia_api`, so however many runs are active (a scheduled run next to a backfill),
Wikipedia's API sees one polite client at a time. Backfill with
`airflow backfill create --dag-id daily_eval --from-date 2026-11-04 --to-date 2026-11-06`
(add `--dag-run-conf '{"run_id": "02b-baseline"}'` to score another detector run).
"""

from datetime import timedelta

from airflow.providers.standard.sensors.time_delta import TimeDeltaSensor
from airflow.sdk import DAG
from airflow.timetables.interval import CronDataIntervalTimetable
from wikipulse_jobs import DEFAULT_ARGS, START_DATE, WIKIMEDIA_POOL, job_task, name_param

with DAG(
    dag_id="daily_eval",
    description="Daily detection evaluation against Wikipedia's Current events portal",
    doc_md=__doc__,
    # One run per UTC day; the run for day D starts after D ends and has `ds` = D.
    schedule=CronDataIntervalTimetable("0 0 * * *", timezone="UTC"),
    start_date=START_DATE,
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=6),
    default_args=DEFAULT_ARGS,
    params={
        "run_id": name_param("Detector run to evaluate; empty: JOBS_EVAL_RUN_ID (live)"),
    },
    tags=["wikipulse", "evaluation"],
) as dag:
    wait_for_late_data = TimeDeltaSensor(
        task_id="wait_for_late_data",
        delta=timedelta(hours=1),  # after the end of the day (the data interval)
        deferrable=True,  # waits in the triggerer, without holding a worker slot
    )
    fetch_ground_truth = job_task(
        "fetch_ground_truth",
        "daily_eval",
        "fetch",
        # Wikipedia may be slow or briefly down: 4 retries, 10 min, 20 min, 40 min, 1 h.
        retries=4,
        retry_delay=timedelta(minutes=10),
        pool=WIKIMEDIA_POOL,
    )
    evaluate = job_task(
        "evaluate",
        "daily_eval",
        "evaluate",
        options='--run-id "$RUN_ID"',
        env={"RUN_ID": "{{ params.run_id }}"},
        pool=WIKIMEDIA_POOL,  # refreshes portal pages whose cached copy has gone stale
    )

    wait_for_late_data >> fetch_ground_truth >> evaluate
