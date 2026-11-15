"""retrain: the news classifier's weekly drift check, retraining and promotion (spec 07c).

    check_drift >> extend_history >> label_new_spikes >> build_dataset
                >> train_challenger >> evaluate_against_champion >> promote_or_reject

One run per week (Monday 00:00 to Monday 00:00 UTC), started after the week ends:

- `check_drift` compares the week's scored spikes (`spike_scores`, detector run
  `run_id`) with the champion's training data and stores the report in `drift_reports`.
  It runs first, against the champion that served that week. Drift is logged as a
  warning, not a failure, and even a failed drift check doesn't stop the retraining
  (`extend_history` runs when `check_drift` is done, whatever its result).
- `extend_history` brings the history lake up to the last complete hour from the
  stream's retained history, so each week's dataset includes the newest edits. It is
  a no-op when the lake is current, or when `lake_prefix` is the live archive.
- `label_new_spikes` (spec 11b) labels the spikes the build will use that nobody has
  judged yet, with the LLM spike labeler (`ML_RETRAIN_LABEL_MODEL`, qwen3:4b, through
  Ollama), within a budget (`ML_RETRAIN_LABEL_BUDGET` spikes,
  `ML_RETRAIN_LABEL_MAX_MINUTES`). Days that could be test days come first. It never
  fails for missing labels: with Ollama down or the budget used up it logs that and
  succeeds, and the build uses the labels there are.
- `build_dataset` builds a new dataset version from the lake and hands its name on.
  Its rows are the live detector's spikes (serving mode, spec 11a); the test split is
  the newest days *ready for testing*: their "Current events" portal is final, at
  least `ML_LABEL_MIN_SHARE` of their spikes are labeled, and they ended at least
  `ML_LABEL_DELAY_DAYS` days ago (the label delay). Newer days wait for a later week, so
  the test split lags the lake by about a week. If the lake's last contiguous stretch is
  shorter than about 10 days (a new lake, or a gap), the task fails with a message
  saying what to do (fill the gap, wait, or build in candidates mode) and nothing
  downstream runs: the champion stays.
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
# Reading a week of the stream's history takes about 2.5 hours (07a: 94 hours in 80 min).
EXTEND = timedelta(hours=6)

with DAG(
    dag_id="retrain",
    description="Weekly drift check, retraining and champion/challenger promotion",
    doc_md=__doc__,
    # One run per week; the run for the week starting Monday D starts after D + 7 days.
    schedule=CronDataIntervalTimetable("0 0 * * 1", timezone="UTC"),
    start_date=START_DATE,
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=12),
    default_args=DEFAULT_ARGS,
    params={
        "run_id": name_param("Detector run whose scores the drift check reads; empty: live"),
        "lake_prefix": name_param("Lake prefix the dataset is built from; empty: ML_LAKE_PREFIX"),
        "as_of": name_param(
            "Labels (and the label delay) as of this UTC moment, e.g. 2026-11-15T18:00; "
            "empty: the end of the lake"
        ),
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
    extend_history = job_task(
        "extend_history",
        "retrain",
        "extend",
        options='--lake-prefix "$LAKE_PREFIX"',
        env={"LAKE_PREFIX": "{{ params.lake_prefix }}"},
        python=WIKIPULSE_ML_PYTHON,
        pool=WIKIMEDIA_POOL,  # one stream connection, never two at once
        execution_timeout=EXTEND,
        trigger_rule="all_done",  # retrain even if the drift check failed
    )
    label_new_spikes = job_task(
        "label_new_spikes",
        "retrain",
        "label",
        options='--lake-prefix "$LAKE_PREFIX" --as-of "$AS_OF"',
        env={"LAKE_PREFIX": "{{ params.lake_prefix }}", "AS_OF": "{{ params.as_of }}"},
        python=WIKIPULSE_ML_PYTHON,
        pool=WIKIMEDIA_POOL,  # page summaries and edit comments for the labeler
        # The task stops itself after ML_RETRAIN_LABEL_MAX_MINUTES (40); this is the
        # hard limit around it (reading the lake takes a few minutes too).
        execution_timeout=LONG,
        retries=1,
    )
    build_dataset = job_task(
        "build_dataset",
        "retrain",
        "build",
        options='--lake-prefix "$LAKE_PREFIX" --as-of "$AS_OF"',
        env={"LAKE_PREFIX": "{{ params.lake_prefix }}", "AS_OF": "{{ params.as_of }}"},
        python=WIKIPULSE_ML_PYTHON,
        pool=WIKIMEDIA_POOL,  # page revisions and portal pages (cached after the first time)
        execution_timeout=LONG,
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
        >> extend_history
        >> label_new_spikes
        >> build_dataset
        >> train_challenger
        >> evaluate_against_champion
        >> promote_or_reject
    )
