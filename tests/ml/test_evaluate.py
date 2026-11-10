"""Evaluation pieces on small hand-made arrays."""

import json

import numpy as np
import pytest

from wikipulse.ml.evaluate import (
    EMBEDDING_BLOCK,
    bootstrap_interval,
    choose_threshold,
    evaluate_model,
    expected_calibration_error,
    paired_gap_interval,
    permutation_importance,
    ranking_metrics,
    threshold_metrics,
)
from wikipulse.ml.features import FEATURE_NAMES
from wikipulse.ml.models import RULE_COLUMN, ModelInput


def test_threshold_metrics_count_the_decisions():
    y = np.array([1, 1, 0, 0, 1])
    score = np.array([0.9, 0.4, 0.8, 0.1, 0.7])
    m = threshold_metrics(y, score, 0.5)  # flags rows 0, 2, 4: 2 right, 1 wrong, 1 missed
    assert m["precision"] == pytest.approx(2 / 3)
    assert m["recall"] == pytest.approx(2 / 3)
    assert m["f1"] == pytest.approx(2 / 3)
    assert m["accuracy"] == pytest.approx(3 / 5)
    assert m["flagged_share"] == pytest.approx(3 / 5)


def test_ranking_metrics_of_a_perfect_and_a_constant_scorer():
    y = np.array([0, 0, 1, 1])
    perfect = ranking_metrics(y, np.array([0.1, 0.2, 0.8, 0.9]))
    assert perfect["average_precision"] == 1.0
    assert perfect["roc_auc"] == 1.0
    constant = ranking_metrics(y, np.full(4, 0.5))
    assert constant["average_precision"] == pytest.approx(0.5)  # the positive share
    assert constant["brier"] == pytest.approx(0.25)
    assert np.isnan(ranking_metrics(np.ones(3, int), np.full(3, 0.5))["roc_auc"])


def test_threshold_reaches_the_target_precision_with_the_most_recall():
    y = np.array([1, 1, 1, 0, 1, 0, 0, 0])
    score = np.array([0.95, 0.9, 0.8, 0.7, 0.6, 0.5, 0.3, 0.1])
    threshold, how = choose_threshold(y, score, target_precision=0.75)
    assert threshold == 0.6  # flags 5 rows, 4 news: precision 0.8, recall 1.0
    assert how.startswith("precision")
    threshold, how = choose_threshold(y, score, target_precision=1.0)
    assert threshold == 0.8  # the most recall without a single mistake
    threshold, how = choose_threshold(np.array([0, 1]), np.array([0.9, 0.1]), 0.99)
    assert how == "max F1 on valid"


def test_threshold_ties_in_recall_go_to_the_highest_threshold():
    # 0.8, 0.7 and 0.6 all catch both news rows (recall 1.0) at precision >= 0.5; going
    # lower than 0.8 only lets in non-news rows, so 0.8 (precision 1.0) is the choice.
    y = np.array([1, 1, 0, 0, 0])
    score = np.array([0.9, 0.8, 0.7, 0.6, 0.5])
    threshold, how = choose_threshold(y, score, target_precision=0.5)
    assert threshold == 0.8
    assert how.startswith("precision")


def test_calibration_error_is_zero_when_scores_come_true():
    score = np.array([0.25] * 4 + [0.75] * 4)
    y = np.array([1, 0, 0, 0, 1, 1, 1, 0])
    assert expected_calibration_error(y, score) == pytest.approx(0.0)
    assert expected_calibration_error(y, np.where(score > 0.5, 1.0, 0.0)) == pytest.approx(0.25)


def test_bootstrap_interval_contains_the_estimate():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 200)
    score = np.clip(y * 0.3 + rng.random(200) * 0.7, 0, 1)
    from sklearn.metrics import average_precision_score

    low, high = bootstrap_interval(y, score, average_precision_score, seed=1, rounds=200)
    assert low < average_precision_score(y, score) < high


def inputs(rows: int, seed: int = 0) -> tuple[ModelInput, np.ndarray]:
    rng = np.random.default_rng(seed)
    features = rng.normal(size=(rows, len(FEATURE_NAMES)))
    y = (features[:, FEATURE_NAMES.index("prior_edits_24h")] > 0).astype(int)
    embeddings = rng.normal(size=(rows, 4))
    return ModelInput(features, rng.integers(0, 2, rows).astype(bool), embeddings), y


def test_permutation_importance_finds_the_one_feature_the_model_uses():
    x, y = inputs(200)
    column = FEATURE_NAMES.index("prior_edits_24h")
    rows = permutation_importance(lambda m: 1 / (1 + np.exp(-m.features[:, column])), x, y, 0)
    assert rows[0]["input"] == "prior_edits_24h"
    assert rows[0]["ap_drop"] > 0.2
    others = [r for r in rows if r["input"] != "prior_edits_24h"]
    assert all(r["ap_drop"] == 0 for r in others)
    assert {RULE_COLUMN, EMBEDDING_BLOCK} <= {r["input"] for r in rows}


def test_evaluate_model_writes_the_artifacts(tmp_path):
    splits = {s: inputs(60, i) for i, s in enumerate(("train", "valid", "test"))}
    column = FEATURE_NAMES.index("prior_edits_24h")

    def predict(m: ModelInput) -> np.ndarray:
        return 1 / (1 + np.exp(-2 * m.features[:, column]))

    result = evaluate_model("toy", predict, splits, 0.8, tmp_path, seed=0)
    assert result.metrics["test_average_precision"] > 0.9
    assert result.metrics["latency_ms_per_spike"] >= 0
    assert {"train_rows", "valid_f1", "test_ece", "threshold"} <= set(result.metrics)
    for name in ("pr_curve.png", "calibration.png", "feature_importance.png"):
        assert (tmp_path / name).stat().st_size > 1000
    saved = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert saved["threshold"] == result.threshold


def test_paired_gap_interval_separates_a_better_model_from_a_copy():
    rng = np.random.default_rng(3)
    y = rng.integers(0, 2, 150)
    good = np.clip(y * 0.5 + rng.random(150) * 0.5, 0, 1)
    noisy = rng.random(150)
    gap, low, high = paired_gap_interval(y, good, noisy, seed=0, rounds=200)
    assert 0 < low <= gap <= high  # clearly better
    assert paired_gap_interval(y, good, good, seed=0, rounds=50) == (0.0, 0.0, 0.0)
