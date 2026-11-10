"""Start the MLflow tracking server: the command of the `mlflow` Compose service.

1. Create MLflow's metadata database in the shared Postgres server, if it is missing.
   MLflow's tables live in their own database (MLFLOW_DB, default `mlflow`), next to
   WikiPulse's and Airflow's, so none can touch the others' tables.
2. Create the artifact bucket in the S3 store (RustFS), if it is missing. Models, plots
   and other run artifacts go there, in a bucket of their own (not the data lake's).
3. Replace this process with `mlflow server`. The server proxies artifacts
   (`--serve-artifacts`): clients upload and download through it, so they need neither
   S3 credentials nor boto3. One worker keeps it light on memory.

Every step is safe to repeat, so it runs at every start.
"""

import os
import sys
import time

import boto3
import psycopg
from botocore.exceptions import ClientError, EndpointConnectionError


def create_database(dsn: str, name: str) -> None:
    if not name.replace("_", "").isalnum():
        raise SystemExit(f"Unexpected database name {name!r} (letters, digits and _ only)")
    for attempt in range(1, 31):
        try:
            conn = psycopg.connect(dsn, dbname="postgres", autocommit=True)
            break
        except psycopg.OperationalError as error:
            print(f"Postgres not reachable yet (attempt {attempt}): {error}", flush=True)
            time.sleep(2)
    else:
        raise SystemExit("Postgres not reachable")
    with conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{name}"')
            print(f"Created database {name}", flush=True)


def create_bucket(bucket: str) -> None:
    s3 = boto3.client("s3", endpoint_url=os.environ["MLFLOW_S3_ENDPOINT_URL"])
    for attempt in range(1, 31):
        try:
            s3.head_bucket(Bucket=bucket)
            return
        except ClientError:
            s3.create_bucket(Bucket=bucket)
            print(f"Created bucket {bucket}", flush=True)
            return
        except EndpointConnectionError as error:
            print(f"S3 store not reachable yet (attempt {attempt}): {error}", flush=True)
            time.sleep(2)
    raise SystemExit("S3 store not reachable")


def main() -> None:
    database = os.environ.get("MLFLOW_DB", "mlflow")
    bucket = os.environ.get("MLFLOW_BUCKET", "wikipulse-mlflow")
    server_dsn = os.environ["MLFLOW_POSTGRES_DSN"]  # without a database name
    create_database(server_dsn, database)
    create_bucket(bucket)
    backend = f"{server_dsn.rstrip('/')}/{database}"
    args = [
        "mlflow",
        "server",
        "--host=0.0.0.0",
        "--port=5000",
        "--workers=1",
        f"--backend-store-uri={backend}",
        f"--artifacts-destination=s3://{bucket}",
        "--serve-artifacts",
    ]
    print(f"Starting MLflow: metadata in database {database}, artifacts in s3://{bucket}")
    sys.stdout.flush()
    os.execvp(args[0], args)  # does not return


if __name__ == "__main__":
    main()
