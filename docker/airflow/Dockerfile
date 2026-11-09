# Airflow for WikiPulse's scheduled jobs (spec 06c). Built by `docker compose --profile
# airflow build`. Airflow and the jobs live in two separate Python environments:
# - Airflow's own (from the official image) parses the DAG files and runs the scheduler;
# - /opt/wikipulse-venv holds the few packages the jobs need (requirements-jobs.txt).
# Tasks start `/opt/wikipulse-venv/bin/python -m wikipulse.jobs...` with the repository
# mounted at /opt/wikipulse, so their dependencies never clash with Airflow's.
FROM apache/airflow:3.3.2-python3.12

COPY requirements-jobs.txt /tmp/requirements-jobs.txt
USER root
RUN python -m venv /opt/wikipulse-venv \
    && /opt/wikipulse-venv/bin/pip install --no-cache-dir -r /tmp/requirements-jobs.txt \
    && chown -R airflow:root /opt/wikipulse-venv
# The repository is mounted from the host and owned by another user, so git would refuse
# it ("detected dubious ownership") and evaluations would store no commit hash.
RUN git config --system --add safe.directory /opt/wikipulse
USER airflow
