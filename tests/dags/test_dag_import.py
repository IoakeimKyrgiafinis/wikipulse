"""DAG import test: load the DAG folder with Airflow itself, as its DAG processor does.

Needs Airflow, so it is skipped in the project's environment and runs inside the Airflow
image (CI job `dags`; locally: see docs/learning/06c-scheduled-jobs.md). No Airflow
server or database is needed: parsing a DAG file only builds Python objects.
"""

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
    }


@pytest.mark.parametrize("dag_id", ["daily_eval", "daily_stats"])
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
    assert evaluate.env == {"DAY": "{{ ds }}", "RUN_ID": "{{ params.run_id }}"}
