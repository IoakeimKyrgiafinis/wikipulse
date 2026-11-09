# Spec 06c: Scheduled jobs with Airflow

Status: done
Stage: 6. Data lake and batch
Owner: agent
Depends on: 02b, 06a

## Goal
Run the recurring batch work on a schedule with a workflow engine: daily ground-truth fetching and detection evaluation, daily statistics, and later model retraining.

## What you'll learn
- **Workflow orchestration:** DAGs, dependencies between tasks, retries, backfills, and why cron isn't enough.
- **Batch alongside streaming:** the live system and nightly jobs working on the same data.
- **Operational thinking:** what happens when a job fails at 3 a.m.? Alerts, retries, reruns.

## Outputs
- Airflow in `docker-compose.yml`, in a light single-container setup, with its metadata in a separate Postgres database
- `dags/daily_eval.py`: fetch yesterday's Current Events page, run the detection evaluation, store results
- `dags/daily_stats.py`: aggregate `hourly_stats` into daily statistics and check data quality (missing hours, sudden volume drops)
- Data quality failures fail the task visibly

## Requirements
1. Every task is idempotent and can be re-run for any past date (backfill).
2. Tasks call existing `wikipulse` modules; no business logic in DAG files.

## Acceptance criteria
- DAG import tests (no syntax or import errors) in CI.
- A backfill over three past days completes in the Airflow UI.

## Look at it yourself (optional, never blocking)
- Break a task on purpose and watch the retries and failure in the UI.
- Note: Argo Workflows (stage 8, optional) does the same job on Kubernetes.
