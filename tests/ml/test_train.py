"""Training on a tiny synthetic dataset: all four models run, metrics are computed and
logged, the best model is registered (a local SQLite MLflow store, no server)."""

from collections.abc import Iterator
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
import pytest

from tests.ml.synthetic import fake_embedder, synthetic_frames
from wikipulse.ml.models import INPUT_COLUMNS, NetConfig
from wikipulse.ml.train import (
    MODELS,
    DatasetInfo,
    TrainConfig,
    TrainReport,
    known_rows,
    local_artifact_location,
    markdown_table,
    register,
    select_model,
    train_all,
)

MODEL_NAME = "test-news-classifier"
DATASET = DatasetInfo("v9", "f" * 64, "abc123", False)


@pytest.fixture(scope="module")
def tracking(tmp_path_factory) -> Iterator[Path]:
    """A throwaway MLflow store: SQLite metadata, artifacts in a folder."""
    root = tmp_path_factory.mktemp("mlflow")
    previous = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f"sqlite:///{(root / 'mlflow.db').as_posix()}")
    yield root
    mlflow.set_tracking_uri(previous)


@pytest.fixture(scope="module")
def report(tracking) -> TrainReport:
    config = TrainConfig(seed=3, net=NetConfig(max_epochs=40, patience=10))
    return train_all(
        synthetic_frames(),
        DATASET,
        fake_embedder(),
        config,
        experiment="test-07b",
        model_name=MODEL_NAME,
        artifact_location=(tracking / "artifacts").as_uri(),
    )


def test_all_four_models_are_trained_and_logged(report):
    client = mlflow.MlflowClient()
    assert set(report.run_ids) == set(MODELS)
    experiment = client.get_run(report.parent_run_id).info.experiment_id
    for name, run_id in report.run_ids.items():
        run = client.get_run(run_id)
        assert run.info.experiment_id == experiment  # not in "Default"
        assert run.data.tags["dataset_version"] == "v9"
        assert run.data.tags["mlflow.parentRunId"] == report.parent_run_id
        for metric in ("test_average_precision", "test_brier", "test_f1", "threshold"):
            assert metric in run.data.metrics, (name, metric)
        files = {f.path for f in client.list_artifacts(run_id, "evaluation")}
        assert {
            "evaluation/pr_curve.png",
            "evaluation/calibration.png",
            "evaluation/feature_importance.json",
            "evaluation/metrics.json",
        } <= files


def test_metrics_are_computed_and_learning_beats_chance(report):
    for name in ("logreg", "gbt", "torch"):
        ap = report.results[name]["test_average_precision"]
        positive_share = report.results[name]["test_positive_share"]
        assert 0 <= ap <= 1
        assert ap > positive_share + 0.1, name  # better than random ranking
        assert 0 <= report.results[name]["test_brier"] <= 1


def test_unknown_labels_are_left_out_and_counted(report):
    frames = synthetic_frames()
    for split, frame in frames.items():
        known, excluded = known_rows(frame)
        assert report.excluded[split] == excluded > 0
        assert report.results["rule"][f"{split}_rows"] == len(known)


def test_the_comparison_is_logged_on_the_parent_run(report):
    client = mlflow.MlflowClient()
    parent = client.get_run(report.parent_run_id)
    assert parent.data.tags["chosen_model"] == report.chosen
    assert parent.data.params["train_excluded_unknown"] == str(report.excluded["train"])
    files = {f.path for f in client.list_artifacts(report.parent_run_id)}
    assert {"comparison.md", "comparison.csv", "pr_curves.png"} <= files
    assert list(report.table["model"]) == list(MODELS)


def test_the_chosen_model_is_registered_with_the_dataset_version(report):
    client = mlflow.MlflowClient()
    version = client.get_model_version(MODEL_NAME, report.registered_version)
    assert version.tags["dataset_version"] == "v9"
    assert version.tags["dataset_fingerprint"] == "f" * 64
    assert version.tags["model_type"] == report.chosen
    assert version.tags["selection_rule"] == "simplest_not_worse"
    assert version.tags["status"] == "latest"


def test_the_selection_rule_is_logged_on_the_parent_run(report):
    run = mlflow.MlflowClient().get_run(report.parent_run_id)
    assert run.data.tags["selection_rule"] == "simplest_not_worse"
    assert run.data.params["select_rule"] == "simplest_not_worse"


def test_a_new_registration_marks_older_versions_as_superseded(report):
    client = mlflow.MlflowClient()
    name = "test-supersede"
    uri = report.model_uris["rule"]
    first = register(uri, name, {"model_type": "rule"}, "first try")
    assert client.get_model_version(name, first).tags["status"] == "latest"
    second = register(uri, name, {"model_type": "rule"}, "second try")
    third = register(uri, name, {"model_type": "rule"}, "third try")
    v1 = client.get_model_version(name, first)
    v2 = client.get_model_version(name, second)
    v3 = client.get_model_version(name, third)
    assert v1.tags["status"] == "superseded"
    assert v1.tags["superseded_by"] == second  # the first successor is kept
    assert v1.description == f"Superseded by version {second}. first try"
    assert v2.tags["status"] == "superseded" and v2.tags["superseded_by"] == third
    assert v3.tags["status"] == "latest" and "superseded_by" not in v3.tags
    assert v3.description == "third try"


def test_the_registered_model_loads_and_scores_a_table(report):
    model = mlflow.pyfunc.load_model(f"models:/{MODEL_NAME}/{report.registered_version}")
    python_model = model.unwrap_python_model()
    python_model._embedder = fake_embedder()  # the real one would be downloaded
    frame = synthetic_frames()["test"].head(6)[INPUT_COLUMNS]
    out = model.predict(frame)
    assert list(out.columns) == ["score", "is_news"]
    assert ((out["score"] >= 0) & (out["score"] <= 1)).all()
    assert (out["is_news"] == (out["score"] >= python_model.threshold)).all()


def test_every_logged_model_loads_including_the_network(report):
    frame = synthetic_frames()["test"].head(4)[INPUT_COLUMNS]
    for name, uri in report.model_uris.items():
        model = mlflow.pyfunc.load_model(uri)
        model.unwrap_python_model()._embedder = fake_embedder()
        scores = model.predict(frame)["score"].to_numpy()
        assert scores.shape == (4,), name
        assert np.isfinite(scores).all(), name


def test_select_model_prefers_the_simpler_model_the_test_set_cant_separate():
    def m(ap: float, gap_low: float) -> dict[str, float]:
        return {"test_average_precision": ap, "test_ap_gap_low": gap_low, "test_ap_gap_high": 0.1}

    results = {
        "rule": m(0.60, 0.15),  # clearly worse: the gap's interval stays above 0
        "logreg": m(0.78, -0.01),  # its interval reaches 0: not clearly worse
        "gbt": m(0.80, 0.0),
        "torch": m(0.75, 0.01),
    }
    name, reason = select_model(results, 0.0)
    assert name == "logreg" and "not clearly worse" in reason
    results["logreg"] = m(0.78, 0.005)
    assert select_model(results, 0.0)[0] == "gbt"
    assert select_model(results, 0.01)[0] == "logreg"  # a margin forgives small gaps
    assert select_model(results, 0.2)[0] == "rule"


def test_select_model_follows_the_configured_rule():
    results = {
        "logreg": {
            "test_average_precision": 0.78,
            "test_ap_gap_low": -0.01,
            "test_ap_gap_high": 0.05,
        },
        "gbt": {"test_average_precision": 0.80, "test_ap_gap_low": 0.0, "test_ap_gap_high": 0.0},
    }
    assert select_model(results, 0.0, "simplest_not_worse")[0] == "logreg"
    name, reason = select_model(results, 0.0, "best_test_ap")
    assert name == "gbt" and "highest" in reason
    with pytest.raises(ValueError, match="unknown selection rule"):
        select_model(results, 0.0, "whatever_wins")


def test_select_model_can_choose_on_the_validation_split():
    results = {
        "logreg": {
            "test_average_precision": 0.90,  # best on test ...
            "test_ap_gap_low": 0.0,
            "test_ap_gap_high": 0.0,
            "valid_average_precision": 0.70,  # ... but clearly worse on valid
            "valid_ap_gap_low": 0.05,
            "valid_ap_gap_high": 0.15,
        },
        "gbt": {
            "test_average_precision": 0.80,
            "test_ap_gap_low": 0.02,
            "test_ap_gap_high": 0.2,
            "valid_average_precision": 0.80,
            "valid_ap_gap_low": 0.0,
            "valid_ap_gap_high": 0.0,
        },
    }
    assert select_model(results, 0.0)[0] == "logreg"
    name, reason = select_model(results, 0.0, split="valid")
    assert name == "gbt" and "highest valid average precision" in reason
    with pytest.raises(ValueError, match="unknown selection split"):
        select_model(results, 0.0, split="train")


def test_retraining_chooses_on_valid_and_still_reports_test_gaps(tracking):
    config = TrainConfig(seed=3, models=("rule", "logreg"), select_split="valid")
    result = train_all(
        synthetic_frames(),
        DATASET,
        None,
        config,
        experiment="test-07b-valid",
        model_name=MODEL_NAME,
        register_best=False,
        artifact_location=(tracking / "artifacts").as_uri(),
    )
    for metrics in result.results.values():
        assert {"valid_ap_gap_low", "test_ap_gap_low"} <= set(metrics)
    best_valid = max(result.results, key=lambda n: result.results[n]["valid_average_precision"])
    assert result.results[best_valid]["valid_ap_gap"] == 0
    assert "valid average precision" in result.reason
    params = mlflow.MlflowClient().get_run(result.parent_run_id).data.params
    assert params["select_split"] == "valid"


def test_paired_gaps_are_logged_and_zero_for_the_best(report):
    best = max(report.results, key=lambda n: report.results[n]["test_average_precision"])
    assert report.results[best]["test_ap_gap"] == 0
    for metrics in report.results.values():
        assert metrics["test_ap_gap_low"] <= metrics["test_ap_gap"] <= metrics["test_ap_gap_high"]
    assert report.results["rule"]["test_ap_gap_low"] > 0  # the rule is clearly worse here


def test_markdown_table_has_one_row_per_model():
    table = pd.DataFrame([{"model": "rule", "test AP": 0.5, "train s": 1234.0}])
    text = markdown_table(table)
    assert text.splitlines()[0] == "| model | test AP | train s |"
    assert "| rule | 0.500 | 1234 |" in text


def test_hyperparameter_candidates_are_nested_runs_of_the_same_experiment(report):
    client = mlflow.MlflowClient()
    experiment = client.get_run(report.parent_run_id).info.experiment_id
    candidates = client.search_runs([experiment], "tags.role = 'candidate'")
    assert len(candidates) == 6 + 6  # logistic regression C values, tree configurations
    parents = {r.data.tags["mlflow.parentRunId"] for r in candidates}
    assert parents == {report.run_ids["logreg"], report.run_ids["gbt"]}


def test_the_rule_keeps_its_own_threshold(report):
    assert report.results["rule"]["threshold"] == 0.5


def test_local_stores_keep_artifacts_under_the_data_folder(tmp_path):
    assert local_artifact_location("http://localhost:5000", tmp_path) is None
    location = local_artifact_location("sqlite:///x.db", tmp_path)
    assert location == (tmp_path.resolve() / "mlruns").as_uri()
