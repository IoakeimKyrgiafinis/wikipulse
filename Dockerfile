# One image for every WikiPulse service (spec 08a); each Kubernetes Deployment runs it
# with a different command, e.g. `python -m wikipulse.stream.consume`.
#
#   docker build -t wikipulse:dev .        (scripts/cluster.py builds and tags it for you)
#
# Two stages: the "build" stage has uv and installs the locked dependencies into a
# virtual environment; the "runtime" stage copies only that environment onto a clean
# Python image, so uv, its download cache and the source tree never reach the result.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# --- build: resolve nothing, install exactly what uv.lock says ---
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /src
# Dependencies first, in their own layer: it is rebuilt only when uv.lock changes, not on
# every code change. torch comes from the CPU-only index (pyproject.toml), so no CUDA.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project --no-editable
# Then WikiPulse itself, installed as a regular (non-editable) package.
COPY pyproject.toml uv.lock README.md ./
COPY wikipulse ./wikipulse
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- runtime: Python, the virtual environment, and an unprivileged user ---
FROM ${PYTHON_IMAGE} AS runtime
# A fixed numeric user, so Kubernetes can check `runAsNonRoot` without looking it up.
RUN groupadd --system --gid 10001 wikipulse \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin wikipulse
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    DATA_DIR=/tmp/data \
    HF_HOME=/models/huggingface
WORKDIR /tmp
USER 10001:10001
# Each Deployment sets its own command; alone, the image checks its services.
CMD ["python", "-m", "wikipulse.check"]
