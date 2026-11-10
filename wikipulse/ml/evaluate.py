"""Evaluating a news classifier: metrics, threshold, curves, importance (spec 07b).

A classifier gives each spike a *score*; turning scores into "news / not news" needs a
*threshold*. So there are two kinds of metric:

- **threshold-free**, judging the ranking: *average precision* (the area under the
  precision-recall curve; 1 = every news spike ranked above every other, the share of
  positives = random) and ROC AUC; and, judging the scores as probabilities, the *Brier
  score* (mean squared error of the score, lower is better), log loss, and the *expected
  calibration error* (`expected_calibration_error`: do 70 % scores come true 70 % of
  the time?).
- **at the threshold**: precision, recall, F1, accuracy, and the share of spikes flagged.

The threshold is chosen on the *validation* split for the use case (`choose_threshold`):
the feed should flag news without crying wolf, so it is the threshold with the most
recall among those that reach a target precision there; among thresholds with that same
recall, the highest one (it lets in the fewest non-news spikes).

`evaluate_model` computes all of it for one model, draws the precision-recall curve, the
calibration curve and the feature importance (`permutation_importance`), and writes them
to a folder; `log_evaluation` logs that folder and the metrics to the active MLflow run.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # draw into files, never open a window

import matplotlib.pyplot as plt  # noqa: E402
import mlflow  # noqa: E402
import numpy as np  # noqa: E402
from sklearn.calibration import calibration_curve  # noqa: E402
from sklearn.metrics import (  # noqa: E402
    average_precision_score,
    brier_score_loss,
    log_loss,
    precision_recall_curve,
    roc_auc_score,
)

from wikipulse.ml.features import FEATURE_NAMES  # noqa: E402
from wikipulse.ml.models import RULE_COLUMN, ModelInput  # noqa: E402

logger = logging.getLogger(__name__)

EMBEDDING_BLOCK = "summary_embedding"
Predict = Callable[[ModelInput], np.ndarray]


# --- metrics ----------------------------------------------------------------------------


def expected_calibration_error(y: np.ndarray, score: np.ndarray, bins: int = 10) -> float:
    """Mean |share of positives - mean score| over equal-width score bins, weighted by
    how many rows fall into each bin. 0 = perfectly calibrated."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    which = np.clip(np.digitize(score, edges[1:-1]), 0, bins - 1)
    error = 0.0
    for b in range(bins):
        mask = which == b
        if mask.any():
            error += mask.mean() * abs(y[mask].mean() - score[mask].mean())
    return float(error)


def ranking_metrics(y: np.ndarray, score: np.ndarray) -> dict[str, float]:
    """Threshold-free metrics (NaN where a split has only one class)."""
    both = len(np.unique(y)) == 2
    clipped = np.clip(score, 1e-6, 1 - 1e-6)
    return {
        "average_precision": float(average_precision_score(y, score)) if both else np.nan,
        "roc_auc": float(roc_auc_score(y, score)) if both else np.nan,
        "brier": float(brier_score_loss(y, score)),
        "log_loss": float(log_loss(y, clipped, labels=[0, 1])),
        "ece": expected_calibration_error(y, score),
        "positive_share": float(y.mean()) if len(y) else np.nan,
        "rows": float(len(y)),
    }


def threshold_metrics(y: np.ndarray, score: np.ndarray, threshold: float) -> dict[str, float]:
    """Metrics of the decisions `score >= threshold`."""
    flagged = score >= threshold
    tp = int((flagged & (y == 1)).sum())
    fp = int((flagged & (y == 0)).sum())
    fn = int((~flagged & (y == 1)).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": float((flagged == (y == 1)).mean()) if len(y) else np.nan,
        "flagged_share": float(flagged.mean()) if len(y) else np.nan,
    }


def choose_threshold(
    y: np.ndarray, score: np.ndarray, target_precision: float
) -> tuple[float, str]:
    """The threshold with the most recall among those reaching `target_precision` on
    these rows; if none does, the one with the best F1. Returns (threshold, how).

    Ties in recall go to the highest precision, then the highest threshold: lowering the
    threshold without catching another news spike only lets in more non-news ones, and
    leaves less room for precision to drop on new data."""
    candidates = np.unique(score)
    best_f1, best_f1_threshold = -1.0, 0.5
    reaching = []
    for threshold in candidates:
        m = threshold_metrics(y, score, float(threshold))
        if m["precision"] >= target_precision and m["recall"] > 0:
            reaching.append((m["recall"], m["precision"], float(threshold)))
        if m["f1"] > best_f1:
            best_f1, best_f1_threshold = m["f1"], float(threshold)
    if reaching:
        return max(reaching)[2], f"precision >= {target_precision:g} on valid"
    return best_f1_threshold, "max F1 on valid"


def bootstrap_interval(
    y: np.ndarray,
    score: np.ndarray,
    metric: Callable[[np.ndarray, np.ndarray], float],
    seed: int,
    rounds: int = 1000,
) -> tuple[float, float]:
    """95 % bootstrap interval of a metric: resample the rows with replacement, recompute,
    take the 2.5th and 97.5th percentiles. Shows how much a small test set can wobble."""
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(rounds):
        rows = rng.integers(0, len(y), len(y))
        if len(np.unique(y[rows])) == 2:
            values.append(metric(y[rows], score[rows]))
    if not values:
        return np.nan, np.nan
    low, high = np.percentile(values, [2.5, 97.5])
    return float(low), float(high)


def paired_gap_interval(
    y: np.ndarray,
    best: np.ndarray,
    other: np.ndarray,
    seed: int,
    rounds: int = 1000,
) -> tuple[float, float, float]:
    """How far `other` trails `best` in average precision on the same rows:
    (gap, 95 % low, 95 % high) of AP(best) - AP(other).

    *Paired*: each bootstrap sample scores both models on the same resampled rows, so
    the luck of the sample (easy or hard spikes) cancels out and only the difference
    between the models is left. If the interval reaches 0, the test set can't tell them
    apart."""
    rng = np.random.default_rng(seed)
    gap = float(average_precision_score(y, best) - average_precision_score(y, other))
    gaps = []
    for _ in range(rounds):
        rows = rng.integers(0, len(y), len(y))
        if len(np.unique(y[rows])) == 2:
            gaps.append(
                average_precision_score(y[rows], best[rows])
                - average_precision_score(y[rows], other[rows])
            )
    if not gaps:
        return gap, np.nan, np.nan
    low, high = np.percentile(gaps, [2.5, 97.5])
    return gap, float(low), float(high)


# --- feature importance -------------------------------------------------------------------


def shuffled(x: ModelInput, column: str, rng: np.random.Generator) -> ModelInput:
    """A copy of `x` with one input (a feature, the rule, or the embedding) shuffled."""
    order = rng.permutation(len(x))
    features, rule, embeddings = x.features.copy(), x.rule.copy(), x.embeddings
    if column == RULE_COLUMN:
        rule = rule[order]
    elif column == EMBEDDING_BLOCK:
        embeddings = None if embeddings is None else embeddings[order]
    else:
        i = FEATURE_NAMES.index(column)
        features[:, i] = features[order, i]
    return ModelInput(features, rule, embeddings)


def permutation_importance(
    predict: Predict, x: ModelInput, y: np.ndarray, seed: int, repeats: int = 5
) -> list[dict[str, Any]]:
    """How much average precision drops when one input is shuffled across rows (which
    cuts its link to the label while keeping its values). Works for any model; the
    summary embedding is shuffled as one block. Sorted, most important first."""
    rng = np.random.default_rng(seed)
    base = float(average_precision_score(y, predict(x)))
    columns = [*FEATURE_NAMES, RULE_COLUMN]
    if x.embeddings is not None:
        columns.append(EMBEDDING_BLOCK)
    rows = []
    for column in columns:
        drops = [
            base - float(average_precision_score(y, predict(shuffled(x, column, rng))))
            for _ in range(repeats)
        ]
        rows.append(
            {"input": column, "ap_drop": float(np.mean(drops)), "std": float(np.std(drops))}
        )
    return sorted(rows, key=lambda r: r["ap_drop"], reverse=True)


# --- plots ------------------------------------------------------------------------------


def plot_pr_curves(curves: dict[str, tuple[np.ndarray, np.ndarray]], path: Path) -> None:
    """Precision against recall, one line per model (name -> (labels, scores))."""
    fig, ax = plt.subplots(figsize=(6, 4.5))
    for name, (y, score) in curves.items():
        precision, recall, _ = precision_recall_curve(y, score)
        ap = average_precision_score(y, score)
        ax.step(recall, precision, where="post", label=f"{name} (AP {ap:.3f})")
    if curves:
        y = next(iter(curves.values()))[0]
        ax.axhline(y.mean(), color="grey", linestyle=":", label=f"random ({y.mean():.2f})")
    ax.set(xlabel="recall", ylabel="precision", xlim=(0, 1), ylim=(0, 1.02))
    ax.set_title("Precision-recall, test split")
    ax.legend(loc="lower left", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def plot_calibration(y: np.ndarray, score: np.ndarray, name: str, path: Path) -> None:
    """Reliability diagram: mean score against the share of news, per score bin."""
    fig, ax = plt.subplots(figsize=(4.5, 4.5))
    ax.plot([0, 1], [0, 1], color="grey", linestyle=":", label="perfect")
    if len(np.unique(score)) > 1:
        share, mean_score = calibration_curve(y, score, n_bins=10, strategy="quantile")
        ax.plot(mean_score, share, marker="o", label=name)
    ax.set(xlabel="mean score", ylabel="share that is news", xlim=(0, 1), ylim=(0, 1))
    ax.set_title(f"Calibration, test split: {name}")
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def plot_importance(rows: list[dict[str, Any]], name: str, path: Path, top: int = 15) -> None:
    shown = list(reversed(rows[:top]))
    fig, ax = plt.subplots(figsize=(6, 0.3 * len(shown) + 1.2))
    ax.barh(
        [r["input"] for r in shown], [r["ap_drop"] for r in shown], xerr=[r["std"] for r in shown]
    )
    ax.set(xlabel="drop in average precision when shuffled")
    ax.set_title(f"Permutation importance, test split: {name}")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


# --- one model --------------------------------------------------------------------------


@dataclass
class Evaluation:
    """Everything measured about one model; `metrics` is flat, for MLflow."""

    name: str
    threshold: float
    threshold_rule: str
    metrics: dict[str, float] = field(default_factory=dict)
    importance: list[dict[str, Any]] = field(default_factory=list)
    test_scores: np.ndarray | None = None
    valid_scores: np.ndarray | None = None  # for choosing among models on valid


def latency_ms(predict: Predict, x: ModelInput, repeats: int = 3) -> float:
    """Milliseconds per spike to score the rows of `x` (best of `repeats`, in one batch)."""
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        predict(x)
        best = min(best, time.perf_counter() - start)
    return 1000 * best / max(len(x), 1)


def evaluate_model(
    name: str,
    predict: Predict,
    splits: dict[str, tuple[ModelInput, np.ndarray]],
    target_precision: float,
    out_dir: Path,
    seed: int,
    fixed_threshold: float | None = None,
) -> Evaluation:
    """Metrics on every split (prefixed `train_`, `valid_`, `test_`), the threshold from
    the validation split (unless the model has a `fixed_threshold`, like the rule), the
    test bootstrap interval, latency, and the plots."""
    out_dir.mkdir(parents=True, exist_ok=True)
    scores = {split: predict(x) for split, (x, _) in splits.items()}
    labels = {split: y for split, (_, y) in splits.items()}
    if fixed_threshold is not None:
        threshold, how = fixed_threshold, "fixed"
    else:
        threshold, how = choose_threshold(labels["valid"], scores["valid"], target_precision)
    result = Evaluation(
        name, threshold, how, test_scores=scores["test"], valid_scores=scores.get("valid")
    )
    for split in splits:
        y, score = labels[split], scores[split]
        values = {**ranking_metrics(y, score), **threshold_metrics(y, score, threshold)}
        result.metrics.update({f"{split}_{k}": v for k, v in values.items()})
    y_test, s_test = labels["test"], scores["test"]
    low, high = bootstrap_interval(y_test, s_test, average_precision_score, seed)
    result.metrics.update(
        threshold=threshold,
        test_average_precision_low=low,
        test_average_precision_high=high,
        latency_ms_per_spike=latency_ms(predict, splits["test"][0]),
    )
    result.importance = permutation_importance(predict, splits["test"][0], y_test, seed)
    write_artifacts(result, y_test, s_test, out_dir)
    return result


def write_artifacts(result: Evaluation, y: np.ndarray, score: np.ndarray, out_dir: Path) -> None:
    plot_pr_curves({result.name: (y, score)}, out_dir / "pr_curve.png")
    plot_calibration(y, score, result.name, out_dir / "calibration.png")
    plot_importance(result.importance, result.name, out_dir / "feature_importance.png")
    (out_dir / "feature_importance.json").write_text(
        json.dumps(result.importance, indent=2), encoding="utf-8", newline="\n"
    )
    summary = {
        "model": result.name,
        "threshold": result.threshold,
        "threshold_rule": result.threshold_rule,
        "metrics": result.metrics,
    }
    (out_dir / "metrics.json").write_text(
        json.dumps(summary, indent=2, default=float), encoding="utf-8", newline="\n"
    )


def finite(metrics: dict[str, float]) -> dict[str, float]:
    """MLflow rejects NaN in some backends; leave those metrics out."""
    return {k: float(v) for k, v in metrics.items() if v is not None and np.isfinite(v)}


def log_evaluation(result: Evaluation, out_dir: Path) -> None:
    """Log the metrics and the artifact folder to the active MLflow run."""
    mlflow.log_metrics(finite(result.metrics))
    mlflow.log_param("threshold_rule", result.threshold_rule)
    mlflow.log_artifacts(str(out_dir), artifact_path="evaluation")
