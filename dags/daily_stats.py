"""daily_stats: one day of lake statistics, then the data-quality check.

    wait_for_late_data  >>  aggregate  >>  check_quality

- `wait_for_late_data` waits until an hour after the day ended, so the last hours' files
  (written every few minutes by the archiver, counted by the 06b worker) are in.
- `aggregate` sums the day's `hourly_stats` into `daily_stats`
  (`python -m wikipulse.jobs.daily_stats aggregate`).
- `check_quality` fails, visibly, when that day has missing hours or a sudden volume
  drop (`... daily_stats check`). It is not retried: the data won't change by itself.
  Fix the cause (or accept the gap), then clear the task to re-check.

All the logic is in `wikipulse.jobs.daily_stats`; this file only says when and in which
order. Backfill past days with
`airflow backfill create --dag-id daily_stats --from-date 2026-11-05 --to-date 2026-11-07`
(or from the UI).
"""

from datetime import timedelta

from airflow.providers.standard.sensors.time_delta import TimeDeltaSensor
from airflow.sdk import DAG
from airflow.timetables.interval import CronDataIntervalTimetable
from wikipulse_jobs import DEFAULT_ARGS, START_DATE, job_task, name_param

with DAG(
    dag_id="daily_stats",
    description="Daily statistics of the data lake and data-quality checks",
    doc_md=__doc__,
    # One run per UTC day. The run for day D covers [D 00:00, D+1 00:00) and starts
    # once that interval is over; its `ds` is D.
    schedule=CronDataIntervalTimetable("0 0 * * *", timezone="UTC"),
    start_date=START_DATE,
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=6),
    default_args=DEFAULT_ARGS,
    params={
        "dataset": name_param(
            "'<bucket>/<prefix>' of the archive in hourly_stats; empty: the live archive"
        ),
    },
    tags=["wikipulse", "lake", "data-quality"],
) as dag:
    wait_for_late_data = TimeDeltaSensor(
        task_id="wait_for_late_data",
        delta=timedelta(hours=1),  # after the end of the day (the data interval)
        deferrable=True,  # waits in the triggerer, without holding a worker slot
    )
    aggregate = job_task(
        "aggregate",
        "daily_stats",
        "aggregate",
        options='--dataset "$DATASET"',
        env={"DATASET": "{{ params.dataset }}"},
    )
    check_quality = job_task(
        "check_quality",
        "daily_stats",
        "check",
        options='--dataset "$DATASET"',
        env={"DATASET": "{{ params.dataset }}"},
        retries=0,
    )

    wait_for_late_data >> aggregate >> check_quality
