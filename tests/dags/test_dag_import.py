"""DAG import test: load the DAG folder with Airflow itself, as its DAG processor does.

Needs Airflow, so it is skipped in the project's environment and runs inside the Airflow
image (CI job `dags`; locally: see docs/learning/06c-scheduled-jobs.md). No Airflow
server or database is needed: parsing a DAG file only builds Python objects.
"""

from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

pytest.importorskip("airflow", reason="Airflow is only installed in the Airflow image")

DAGS = Path(__file__).resolve().parents[2] / "dags"


@pytest.fixture(scope="module")
def dagbag():
    from airflow.dag_processing.dagbag import BundleDagBag

    # The same loader the DAG processor uses (it puts the folder on sys.path).
    return BundleDagBag(dag_folder=str(DAGS), bundle_path=DAGS, bundle_name="dags-folder")


def test_no_import_errors(dagbag) -> None:
    assert dagbag.import_errors == {}


def test_expected_dags_and_tasks(dagbag) -> None:
    tasks = {dag_id: [t.task_id for t in dag.tasks] for dag_id, dag in dagbag.dags.items()}
    assert tasks == {
        "daily_eval": ["wait_for_late_data", "fetch_ground_truth", "evaluate"],
        "daily_stats": ["wait_for_late_data", "aggregate", "check_quality"],
        "retrain": [
            "check_drift",
            "extend_history",
            "build_dataset",
            "train_challenger",
            "evaluate_against_champion",
            "promote_or_reject",
        ],
    }


@pytest.mark.parametrize("dag_id", ["daily_eval", "daily_stats", "retrain"])
def test_tasks_form_a_chain_and_dag_is_lean(dagbag, dag_id: str) -> None:
    dag = dagbag.dags[dag_id]
    order = [t.task_id for t in dag.tasks]
    for upstream, downstream in zip(order, order[1:], strict=False):
        assert downstream in dag.get_task(upstream).downstream_task_ids
    assert dag.catchup is False
    assert dag.max_active_runs == 1


def test_quality_check_is_not_retried_but_jobs_are(dagbag) -> None:
    stats = dagbag.dags["daily_stats"]
    assert stats.get_task("check_quality").retries == 0
    assert stats.get_task("aggregate").retries >= 1
    fetch = dagbag.dags["daily_eval"].get_task("fetch_ground_truth")
    assert fetch.retries >= 3
    assert fetch.pool == "wikimedia_api"


def test_commands_take_the_day_and_params_from_the_environment(dagbag) -> None:
    evaluate = dagbag.dags["daily_eval"].get_task("evaluate")
    assert evaluate.bash_command.endswith('evaluate --date "$DAY" --run-id "$RUN_ID"')
    assert evaluate.env == {
        "DAY": "{{ data_interval_start | ds }}",
        "RUN_ID": "{{ params.run_id }}",
    }


@pytest.mark.parametrize("dag_id", ["daily_eval", "daily_stats"])
def test_a_scheduled_run_is_for_the_day_that_just_ended(dagbag, dag_id: str) -> None:
    from airflow.timetables.base import DataInterval, TimeRestriction

    timetable = dagbag.dags[dag_id].timetable
    last = DataInterval(datetime(2026, 11, 4, tzinfo=UTC), datetime(2026, 11, 5, tzinfo=UTC))
    info = timetable.next_dagrun_info(
        last_automated_data_interval=last,
        restriction=TimeRestriction(earliest=None, latest=None, catchup=True),
    )
    assert info.data_interval.start == datetime(2026, 11, 5, tzinfo=UTC)
    assert info.data_interval.end == datetime(2026, 11, 6, tzinfo=UTC)


@pytest.mark.parametrize(
    "logical_date",
    [datetime(2026, 11, 9, 14, tzinfo=UTC), datetime(2026, 11, 9, tzinfo=UTC)],
)
@pytest.mark.parametrize("dag_id", ["daily_eval", "daily_stats"])
def test_a_manual_run_is_for_a_day_that_is_already_over(
    dagbag, dag_id: str, logical_date: datetime
) -> None:
    """Triggered by hand 'for' 9 November, a run processes a complete day, never the 9th."""
    interval = dagbag.dags[dag_id].timetable.infer_manual_data_interval(run_after=logical_date)
    assert interval.end <= logical_date
    assert interval.end - interval.start == timedelta(days=1)
    assert interval.start.time() == time(0)


def test_every_pool_a_task_uses_is_created_at_start(dagbag, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(DAGS))
    from wikipulse_jobs import POOLS

    used = {task.pool for dag in dagbag.dags.values() for task in dag.tasks}
    assert used - {"default_pool"} <= set(POOLS)
    assert POOLS["wikimedia_api"][0] == 1


def test_retraining_runs_weekly_with_the_ml_environment(dagbag) -> None:
    from airflow.timetables.base import DataInterval, TimeRestriction

    dag = dagbag.dags["retrain"]
    last = DataInterval(datetime(2026, 11, 2, tzinfo=UTC), datetime(2026, 11, 9, tzinfo=UTC))
    info = dag.timetable.next_dagrun_info(
        last_automated_data_interval=last,
        restriction=TimeRestriction(earliest=None, latest=None, catchup=True),
    )
    assert info.data_interval.start == datetime(2026, 11, 9, tzinfo=UTC)  # a Monday
    assert info.data_interval.end == datetime(2026, 11, 16, tzinfo=UTC)
    for task in dag.tasks:
        assert task.bash_command.startswith('"/opt/wikipulse-ml-venv/bin/python" -m ')
        assert "wikipulse.jobs.retrain" in task.bash_command


def test_retraining_passes_values_on_and_survives_a_failed_drift_check(dagbag) -> None:
    dag = dagbag.dags["retrain"]
    assert dag.get_task("extend_history").trigger_rule == "all_done"
    assert dag.get_task("build_dataset").trigger_rule == "all_success"  # on the new edits
    train = dag.get_task("train_challenger")
    assert train.env["DATASET"] == "{{ ti.xcom_pull(task_ids='build_dataset') }}"
    assert train.bash_command.endswith('train --date "$DAY" --dataset "$DATASET"')
    promote = dag.get_task("promote_or_reject")
    assert promote.env["CHALLENGER"] == "{{ ti.xcom_pull(task_ids='train_challenger') }}"
    assert promote.retries == 0
    assert dag.get_task("build_dataset").pool == "wikimedia_api"
    assert dag.get_task("extend_history").pool == "wikimedia_api"
