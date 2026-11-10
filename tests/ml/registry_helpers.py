"""A throwaway MLflow registry (SQLite + a folder) with small news classifiers in it,
for the serving, registry and retraining tests. No server, no network."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import mlflow
import numpy as np

from tests.ml.synthetic import synthetic_frames
from wikipulse.ml.models import (
    NewsClassifierModel,
    RuleScorer,
    SklearnScorer,
    features_of,
    logistic_regression,
)
from wikipulse.ml.train import ensure_experiment, log_model


@contextmanager
def local_tracking(root: Path) -> Iterator[Path]:
    """Point MLflow at a SQLite store under `root` for the duration."""
    previous = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f"sqlite:///{(root / 'mlflow.db').as_posix()}")
    mlflow.set_experiment(
        experiment_id=ensure_experiment("test-07c", (root / "artifacts").as_uri())
    )
    try:
        yield root
    finally:
        mlflow.set_tracking_uri(previous)


def fitted_logreg(c: float = 0.1) -> SklearnScorer:
    """Logistic regression fitted on the synthetic training split's known labels."""
    train = synthetic_frames()["train"]
    known = train[train["label"].notna()]
    estimator = logistic_regression(c).fit(features_of(known), known["label"].to_numpy(int))
    return SklearnScorer(estimator)


def register(
    name: str, scorer=None, threshold: float = 0.5, tags: dict[str, str] | None = None
) -> str:
    """Log `scorer` (default: the rule) as a pyfunc model and register a new version."""
    wrapper = NewsClassifierModel(scorer or RuleScorer(), threshold, None)
    example = synthetic_frames()["test"].head(5)
    with mlflow.start_run():
        uri = log_model(wrapper, example)
    version = mlflow.register_model(uri, name, tags=tags or {})
    return str(version.version)


def labeled_test_rows():
    """The synthetic test split's known-label rows (with `summary_text`)."""
    test = synthetic_frames()["test"]
    return test[test["label"].notna()].reset_index(drop=True)


def labels(frame) -> np.ndarray:
    return frame["label"].to_numpy(dtype=np.int64)
