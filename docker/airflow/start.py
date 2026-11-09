"""Start Airflow for WikiPulse: the command of the `airflow` Compose service.

1. Create Airflow's metadata database in the shared Postgres server, if it is missing.
   Airflow's tables live in their own database (AIRFLOW_DB, default `airflow`), next to
   WikiPulse's, so neither can touch the other's tables.
2. `airflow db migrate`: create or upgrade Airflow's tables.
3. Create the pools the DAGs use (a pool caps how many tasks run at once across all
   DAGs and backfills), as `POOLS` in dags/wikipulse_jobs.py defines them:
   `wikimedia_api` with one slot, so only one task at a time talks to Wikipedia, as
   AGENTS.md asks.
4. Replace this process with `airflow standalone` (scheduler, DAG processor,
   triggerer and the web UI/API in one container).

Every step is safe to repeat, so it runs at every start.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

# The pools are defined once, next to the DAGs that use them (dags/wikipulse_jobs.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dags"))
from wikipulse_jobs import POOLS  # noqa: E402


def create_database() -> int:
    url = make_url(os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"])
    name = url.database
    if not name or not name.replace("_", "").isalnum():
        print(f"Unexpected database name {name!r} (letters, digits and _ only)")
        return 1
    # Connect to the server's maintenance database to create the other one.
    engine = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    for attempt in range(1, 31):
        try:
            with engine.connect() as conn:
                exists = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
                ).scalar()
                if not exists:
                    conn.execute(text(f'CREATE DATABASE "{name}"'))
                    print(f"Created database {name}")
                else:
                    print(f"Database {name} exists")
            return 0
        except OperationalError as error:
            print(f"Postgres not reachable yet (attempt {attempt}): {error.orig}")
            time.sleep(2)
    return 1


def main() -> int:
    if create_database() != 0:
        return 1
    subprocess.run(["airflow", "db", "migrate"], check=True)
    for name, (slots, description) in POOLS.items():
        subprocess.run(["airflow", "pools", "set", name, str(slots), description], check=True)
    os.execvp("airflow", ["airflow", "standalone"])  # does not return


if __name__ == "__main__":
    sys.exit(main())
