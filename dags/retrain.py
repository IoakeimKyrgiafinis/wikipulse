"""retrain: the news classifier's weekly drift check, retraining and promotion (spec 07c).

    check_drift >> build_dataset >> train_challenger >> evaluate_against_champion
                >> promote_or_reject

One run per week (Monday 00:00 to Monday 00:00 UTC), started after the week ends:

- `check_drift` compares the week's scored spikes (`spike_scores`, detector run
  `run_id`) with the champion's training data and stores the report in `drift_reports`.
  It runs first, against the champion that served that week. Drift is logged as a
  warning, not a failure, and even a failed drift check doesn't stop the retraining
  (`build_dataset` runs when `check_drift` is done, whatever its result).
- `build_dataset` builds a new dataset version from the lake and hands its name on.
- `train_challenger` trains the models of spec 07b on it and registers the chosen one as
  a new version with the alias `@challenger`.
- `evaluate_against_champion` scores champion and challenger on the same labeled test
  rows and decides: promote only if the challenger is better by `ML_PROMOTE_MARGIN`.
- `promote_or_reject` moves `@champion` to the challenger, or tags it `rejected`.

All the logic is in `wikipulse.jobs.retrain`; the tasks run it with the machine-learning
environment of the image (`WIKIPULSE_ML_PYTHON`). Values pass between tasks as the last
line a step prints (Airflow's XCom), and reach the next command as environment
variables. The MLflow server must be running (`docker compose --profile mlflow up -d`).
"""

from datetime import timedelta

from airflow.sdk import DAG
from airflow.timetables.interval import CronDataIntervalTimetable
from wikipulse_jobs import (
    DEFAULT_ARGS,
    START_DATE,
    WIKIMEDIA_POOL,
    WIKIPULSE_ML_PYTHON,
    job_task,
    name_param,
)

# The last line an earlier task printed (a BashOperator pushes it as its XCom value).
BUILT_DATASET = "{{ ti.xcom_pull(task_ids='build_dataset') }}"
CHALLENGER = "{{ ti.xcom_pull(task_ids='train_challenger') }}"
LONG = timedelta(hours=1)  # building and training take minutes, more on a first run

with DAG(
    dag_id="retrain",
    description="Weekly drift check, retraining and champion/challenger promotion",
    doc_md=__doc__,
    # One run per week; the run for the week starting Monday D starts after D + 7 days.
    schedule=CronDataIntervalTimetable("0 0 * * 1", timezone="UTC"),
    start_date=START_DATE,
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=6),
    default_args=DEFAULT_ARGS,
    params={
        "run_id": name_param("Detector run whose scores the drift check reads; empty: live"),
        "lake_prefix": name_param("Lake prefix the dataset is built from; empty: ML_LAKE_PREFIX"),
    },
    tags=["wikipulse", "ml"],
) as dag:
    check_drift = job_task(
        "check_drift",
        "retrain",
        "drift",
        options='--run-id "$RUN_ID"',
        env={"RUN_ID": "{{ params.run_id }}"},
        python=WIKIPULSE_ML_PYTHON,
        retries=1,
    )
    build_dataset = job_task(
        "build_dataset",
        "retrain",
        "build",
        options='--lake-prefix "$LAKE_PREFIX"',
        env={"LAKE_PREFIX": "{{ params.lake_prefix }}"},
        python=WIKIPULSE_ML_PYTHON,
        pool=WIKIMEDIA_POOL,  # page revisions and portal pages (cached after the first time)
        execution_timeout=LONG,
        trigger_rule="all_done",  # retrain even if the drift check failed
    )
    train_challenger = job_task(
        "train_challenger",
        "retrain",
        "train",
        options='--dataset "$DATASET"',
        env={"DATASET": BUILT_DATASET},
        python=WIKIPULSE_ML_PYTHON,
        pool=WIKIMEDIA_POOL,  # the summaries the neural network embeds
        execution_timeout=LONG,
    )
    evaluate_against_champion = job_task(
        "evaluate_against_champion",
        "retrain",
        "evaluate",
        options='--dataset "$DATASET" --challenger "$CHALLENGER"',
        env={"DATASET": BUILT_DATASET, "CHALLENGER": CHALLENGER},
        python=WIKIPULSE_ML_PYTHON,
        pool=WIKIMEDIA_POOL,
    )
    promote_or_reject = job_task(
        "promote_or_reject",
        "retrain",
        "promote",
        options='--challenger "$CHALLENGER"',
        env={"CHALLENGER": CHALLENGER},
        python=WIKIPULSE_ML_PYTHON,
        retries=0,  # it refuses for a reason (no decision, champion changed): don't repeat
    )

    (
        check_drift
        >> build_dataset
        >> train_challenger
        >> evaluate_against_champion
        >> promote_or_reject
    )
