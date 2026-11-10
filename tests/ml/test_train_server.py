"""Training against the real MLflow server (`-m integration`).

Needs `docker compose --profile mlflow up -d`; skipped when the server isn't reachable
(CI doesn't start it: its image is built locally, and the unit tests cover the same
code with a local SQLite store). Uses its own experiment and registered-model names and
deletes both afterwards (the server keeps a deleted experiment's rows until
`mlflow gc`); the real `news-classifier` experiment and model are never touched.
"""

import uuid
from collections.abc import Iterator

import httpx
import mlflow
import pytest

from tests.ml.synthetic import fake_embedder, synthetic_frames
from wikipulse.config import get_settings
from wikipulse.ml.models import NetConfig
from wikipulse.ml.train import DatasetInfo, TrainConfig, train_all

pytestmark = pytest.mark.integration


@pytest.fixture
def server() -> Iterator[str]:
    uri = get_settings().mlflow_tracking_uri
    try:
        httpx.get(f"{uri.rstrip('/')}/health", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip(f"no MLflow server at {uri} (docker compose --profile mlflow up -d)")
    previous = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(uri)
    yield uri
    mlflow.set_tracking_uri(previous)


def test_training_logs_and_registers_on_the_server(server):
    suffix = uuid.uuid4().hex[:12]
    experiment, model_name = f"test-07b-{suffix}", f"test-news-classifier-{suffix}"
    client = mlflow.MlflowClient()
    try:
        report = train_all(
            synthetic_frames(),
            DatasetInfo("v0-test", "0" * 64),
            fake_embedder(),
            TrainConfig(
                seed=1, net=NetConfig(max_epochs=10, patience=5), models=("rule", "logreg", "torch")
            ),
            experiment=experiment,
            model_name=model_name,
        )
        version = client.get_model_version(model_name, report.registered_version)
        assert version.tags["dataset_version"] == "v0-test"
        # Artifacts went through the server's proxy into the S3 store, and come back.
        files = {f.path for f in client.list_artifacts(report.run_ids["logreg"], "evaluation")}
        assert "evaluation/pr_curve.png" in files
        model = mlflow.pyfunc.load_model(f"models:/{model_name}/{report.registered_version}")
        assert model.metadata.signature is not None
    finally:
        if client.search_registered_models(filter_string=f"name = '{model_name}'"):
            client.delete_registered_model(model_name)
        found = client.get_experiment_by_name(experiment)
        if found is not None:
            client.delete_experiment(found.experiment_id)
