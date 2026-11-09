"""What the WikiPulse DAGs share: how a task runs a job, retries, and failure alerts.

Not a DAG itself. Airflow puts the DAG folder on `sys.path`, so the DAG files import it
as `wikipulse_jobs`.

Every task runs one step of a `wikipulse.jobs` command in its own process, with the
Python of the WikiPulse environment (`WIKIPULSE_PYTHON`) from the repository folder
(`WIKIPULSE_HOME`). Airflow's own Python never imports `wikipulse`, so the two keep
separate dependencies, and the DAG files hold no business logic, only orchestration.
"""

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import Param

logger = logging.getLogger(__name__)

WIKIPULSE_HOME = os.environ.get("WIKIPULSE_HOME", "/opt/wikipulse")
WIKIPULSE_PYTHON = os.environ.get("WIKIPULSE_PYTHON", "/opt/wikipulse-venv/bin/python")

# Runs before this date are never scheduled. `catchup=False` in the DAGs means Airflow
# doesn't run every missed day on its own either: past days are run with a backfill.
START_DATE = datetime(2026, 11, 1, tzinfo=UTC)

# Pool with one slot: tasks in it run one at a time, across all DAGs, scheduled runs and
# backfills. For everything that calls Wikimedia.
WIKIMEDIA_POOL = "wikimedia_api"

# The pools the DAGs use, name -> (slots, description). docker/airflow/start.py imports
# this and creates them at every start, so the names are defined only here.
POOLS = {
    WIKIMEDIA_POOL: (1, "Tasks that call Wikimedia APIs: one at a time (AGENTS.md)"),
}

# The UTC day a run is for: the start of its data interval. A scheduled run for day D
# starts after D ends and has the interval [D, D+1). A manual run (UI "Trigger") gets
# the last *complete* interval before its logical date, so it never processes a day
# that is still going on. (`{{ ds }}` would be the logical date itself, which for a
# manual run may be today.)
DAY_TEMPLATE = "{{ data_interval_start | ds }}"

# The names a parameter may contain (run ids, '<bucket>/<prefix>'); empty = the default.
NAME_PATTERN = r"^[A-Za-z0-9._:/-]*$"


def alert_on_failure(context: dict[str, Any]) -> None:
    """Called once a task has failed for good (after its last retry).

    This is where an alert would be sent (email, chat, a pager). Locally it is a loud
    log line in the task's log; the UI shows the failed task in red either way.
    """
    ti = context["ti"]
    logger.error(
        "ALERT: %s.%s failed for %s after %s attempt(s). Fix the cause, then clear the "
        "task in the UI (or run a backfill) to run it again.",
        ti.dag_id,
        ti.task_id,
        context.get("ds"),
        ti.try_number,
    )


DEFAULT_ARGS: dict[str, Any] = {
    "owner": "wikipulse",
    # Temporary failures (a database restart, a network error) usually pass on retry:
    # wait 5 minutes, then 10, ... (exponential backoff, at most an hour).
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(hours=1),
    # A stuck task is stopped (and retried) instead of blocking the next days.
    "execution_timeout": timedelta(minutes=30),
    "on_failure_callback": alert_on_failure,
}


def name_param(description: str) -> Param:
    """A run parameter shown in the UI's trigger form; empty means the job's default."""
    return Param("", type="string", pattern=NAME_PATTERN, description=description)


def job_task(
    task_id: str,
    module: str,
    step: str,
    options: str = "",
    env: dict[str, str] | None = None,
    **kwargs: Any,
) -> BashOperator:
    """A task that runs `python -m wikipulse.jobs.<module> <step> --date <day> <options>`.

    `<day>` is `DAY_TEMPLATE`, the start of the run's data interval: the UTC day the run
    is *for* (yesterday for a scheduled run, any past day for a backfill, the last
    complete day before the chosen logical date for a manual run). Values from
    the UI (parameters) reach the command as environment variables (`env`, templated),
    which `options` refers to as "$NAME"; they are never pasted into the shell line.
    The rest of `kwargs` goes to the operator (retries, ...).
    """
    command = f'"{WIKIPULSE_PYTHON}" -m wikipulse.jobs.{module} {step} --date "$DAY" {options}'
    return BashOperator(
        task_id=task_id,
        bash_command=command.strip(),
        cwd=WIKIPULSE_HOME,
        env={"DAY": DAY_TEMPLATE, **(env or {})},
        append_env=True,  # keep the container's settings (POSTGRES_DSN, S3_ENDPOINT, ...)
        **kwargs,
    )
