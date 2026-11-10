# Airflow for WikiPulse's scheduled jobs (spec 06c). Built by `docker compose --profile
# airflow build`. Airflow and the jobs live in separate Python environments:
# - Airflow's own (from the official image) parses the DAG files and runs the scheduler;
# - /opt/wikipulse-venv holds the few packages the daily jobs need (requirements-jobs.txt);
# - /opt/wikipulse-ml-venv holds the whole project's dependencies for the retraining DAG
#   (spec 07c: MLflow, scikit-learn, CPU-only PyTorch, ...; requirements-ml.txt).
# Tasks start `<venv>/bin/python -m wikipulse.jobs...` with the repository mounted at
# /opt/wikipulse, so their dependencies never clash with Airflow's.
FROM apache/airflow:3.3.2-python3.12

COPY requirements-jobs.txt /tmp/requirements-jobs.txt
COPY requirements-ml.txt /tmp/requirements-ml.txt
USER root
RUN python -m venv /opt/wikipulse-venv \
    && /opt/wikipulse-venv/bin/pip install --no-cache-dir -r /tmp/requirements-jobs.txt \
    && chown -R airflow:root /opt/wikipulse-venv
# PyTorch first, from the CPU-only index (without its dependencies, which come from PyPI
# at the locked versions in the next step); PyPI's Linux wheels would bring several GB
# of CUDA libraries.
RUN python -m venv /opt/wikipulse-ml-venv \
    && TORCH="$(grep -oE '^torch==[^ ]+\+cpu' /tmp/requirements-ml.txt)" \
    && /opt/wikipulse-ml-venv/bin/pip install --no-cache-dir --no-deps \
        --index-url https://download.pytorch.org/whl/cpu "$TORCH" \
    && /opt/wikipulse-ml-venv/bin/pip install --no-cache-dir -r /tmp/requirements-ml.txt \
    && chown -R airflow:root /opt/wikipulse-ml-venv
# The repository is mounted from the host and owned by another user, so git would refuse
# it ("detected dubious ownership") and evaluations would store no commit hash.
RUN git config --system --add safe.directory /opt/wikipulse
USER airflow
