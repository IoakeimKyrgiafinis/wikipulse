# MLflow tracking server for WikiPulse (spec 07b). Built by `docker compose --profile
# mlflow build`. The official image holds MLflow only; the server also needs a Postgres
# driver (metadata in the `mlflow` database of the postgres service) and boto3 (artifacts
# in the S3 store, RustFS). Both are pinned, like the base image. (SQLAlchemy 2.1, in the
# image, uses psycopg 3 for postgresql:// URLs.)
FROM ghcr.io/mlflow/mlflow:v3.18.0

RUN pip install --no-cache-dir "psycopg[binary]==3.3.8" boto3==1.43.127
COPY start.py /opt/mlflow/start.py
CMD ["python", "/opt/mlflow/start.py"]
