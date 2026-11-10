"""Train the news classifiers and track them with MLflow: `uv run python -m wikipulse.ml.train`.

    docker compose --profile mlflow up -d            # the tracking server (once)
    uv run python -m wikipulse.ml.train              # newest dataset version
    uv run python -m wikipulse.ml.train --dataset v2 --no-register

What one run does:

1. **Data.** Reads a dataset version (spec 07a) from the object store. Rows whose label
   is unknown are left out of training *and* evaluation (counted per split). The labels
   are mostly LLM labels (`label_source` = `llm`), plus portal links; the numbers measure
   agreement with that labeler, not with human judgement.
2. **Summaries.** For the neural model: the page's short description and lead at the
   cutoff revision, embedded with `EVENTS_EMBED_MODEL` (`wikipulse.ml.summaries`).
3. **Four models**, each an MLflow run nested under one parent run (`models.py`): the
   current rule, logistic regression, gradient-boosted trees, and a small PyTorch network
   on the features plus the summary embedding. Hyperparameters are chosen on the
   validation split (each candidate is a nested run of its own); the test split is only
   used to report.
4. **Evaluation** (`evaluate.py`) of each model: metrics on every split, threshold from
   the validation split, precision-recall and calibration curves, permutation importance,
   latency, logged as metrics and artifacts; the fitted model is logged as a pyfunc model.
5. **Choice and registration.** The comparison table goes to the parent run. The model
   registered as `ML_MODEL_NAME` is chosen by `ML_SELECT_RULE`, fixed in config and logged
   before any model is trained (`select_model`). The default, `simplest_not_worse`, takes
   the *simplest* one (rule, logistic regression, trees, network) that the test set can't
   tell from the best: the paired-bootstrap interval of its gap in test average precision
   reaches `ML_SELECT_MARGIN`. The new version is tagged with the dataset version, the
   fingerprint and the selection rule, and older versions are tagged as superseded by it
   (`supersede_older_versions`).
"""

import argparse
import logging
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mlflow
import numpy as np
import pandas as pd
from mlflow.models import infer_signature

from wikipulse.config import Settings, get_settings
from wikipulse.events.embed import Embedder
from wikipulse.logging_setup import setup_logging
from wikipulse.ml.evaluate import (
    Evaluation,
    evaluate_model,
    finite,
    log_evaluation,
    paired_gap_interval,
    plot_pr_curves,
    ranking_metrics,
)
from wikipulse.ml.models import (
    INPUT_COLUMNS,
    RULE_COLUMN,
    TEXT_COLUMN,
    ModelInput,
    NetConfig,
    NewsClassifierModel,
    RuleScorer,
    Scorer,
    SklearnScorer,
    features_of,
    fit_torch,
    gradient_boosted_trees,
    logistic_regression,
)
from wikipulse.ml.summaries import embed_texts

logger = logging.getLogger(__name__)

SPLITS = ("train", "valid", "test")
# Simplest first: the order in which `select_model` breaks near-ties.
MODELS = ("rule", "logreg", "gbt", "torch")
MODEL_TITLES = {
    "rule": "rule (live default detector)",
    "logreg": "logistic regression",
    "gbt": "gradient-boosted trees",
    "torch": "neural network + summary embedding",
}
# How `select_model` may choose (`ML_SELECT_RULE`); the text is logged with every run.
SELECT_RULES = {
    "simplest_not_worse": (
        "the simplest model (rule < logreg < gbt < torch) whose 95% paired-bootstrap "
        "interval of the gap in test average precision to the best reaches the margin"
    ),
    "best_test_ap": "the model with the highest test average precision",
}
LOGREG_C = (0.003, 0.01, 0.03, 0.1, 0.3, 1.0)
GBT_GRID = tuple(
    {"max_depth": depth, "max_iter": rounds, "learning_rate": 0.05}
    for depth in (2, 3)
    for rounds in (50, 150, 300)
)


# --- data -------------------------------------------------------------------------------


@dataclass
class DatasetInfo:
    """Where the data came from, logged with every run."""

    version: str
    fingerprint: str
    commit: str | None = None
    dirty: bool | None = None

    def tags(self) -> dict[str, str]:
        tags = {"dataset_version": self.version, "dataset_fingerprint": self.fingerprint}
        if self.commit:
            tags["dataset_commit"] = self.commit + (" (dirty)" if self.dirty else "")
        return tags


@dataclass
class Split:
    """The rows of one split with a known label, as model input."""

    frame: pd.DataFrame
    x: ModelInput
    y: np.ndarray
    excluded: int  # rows left out because their label is unknown


def known_rows(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """(rows with label 0 or 1, number of rows with an unknown label)."""
    known = frame[frame["label"].notna()].reset_index(drop=True)
    return known, len(frame) - len(known)


def make_splits(
    frames: dict[str, pd.DataFrame], embed: Callable[[list[str]], np.ndarray] | None
) -> dict[str, Split]:
    """Known-label rows of each split, with their summary embeddings (if `embed`)."""
    splits = {}
    for name in SPLITS:
        known, excluded = known_rows(frames[name])
        embeddings = embed(known[TEXT_COLUMN].tolist()) if embed is not None else None
        x = ModelInput(features_of(known), known[RULE_COLUMN].to_numpy(dtype=bool), embeddings)
        splits[name] = Split(known, x, known["label"].to_numpy(dtype=np.int64), excluded)
    return splits


def data_params(frames: dict[str, pd.DataFrame], splits: dict[str, Split]) -> dict[str, Any]:
    """Row counts and label sources per split, for the parent run."""
    params: dict[str, Any] = {}
    for name in SPLITS:
        split = splits[name]
        params[f"{name}_rows"] = len(frames[name])
        params[f"{name}_known"] = len(split.y)
        params[f"{name}_excluded_unknown"] = split.excluded
        params[f"{name}_positives"] = int(split.y.sum())
        sources = split.frame["label_source"].value_counts().to_dict()
        params[f"{name}_label_sources"] = ",".join(f"{k}:{v}" for k, v in sorted(sources.items()))
    return params


# --- training ---------------------------------------------------------------------------


@dataclass
class TrainConfig:
    seed: int = 42
    target_precision: float = 0.8
    select_rule: str = "simplest_not_worse"
    select_margin: float = 0.0
    embed_model: str = "BAAI/bge-small-en-v1.5"
    net: NetConfig = field(default_factory=NetConfig)
    logreg_c: tuple[float, ...] = LOGREG_C
    gbt_grid: tuple[dict[str, Any], ...] = GBT_GRID
    models: tuple[str, ...] = MODELS


@dataclass
class Fitted:
    """A trained model and how it was made."""

    name: str
    scorer: Scorer
    params: dict[str, Any]
    train_seconds: float
    curves: dict[str, list[float]] = field(default_factory=dict)  # per-epoch values
    threshold: float | None = None  # None: chosen on the validation split


def valid_log_loss(scorer: Scorer, valid: Split) -> float:
    return ranking_metrics(valid.y, scorer.predict_proba(valid.x))["log_loss"]


def log_candidate(name: str, params: dict[str, Any], scorer: Scorer, splits: dict[str, Split]):
    """One hyperparameter candidate as a nested run: its parameters and validation scores."""
    with mlflow.start_run(run_name=name, nested=True):
        mlflow.set_tag("role", "candidate")
        mlflow.log_params(params)
        score = scorer.predict_proba(splits["valid"].x)
        mlflow.log_metrics(finite(ranking_metrics(splits["valid"].y, score)))


def fit_rule(splits: dict[str, Split], config: TrainConfig) -> Fitted:
    """The rule needs no training, and its threshold is fixed: it fires (1) or not (0)."""
    params = {"rule": "default DETECT_* thresholds fired by the cutoff"}
    return Fitted("rule", RuleScorer(), params, 0.0, threshold=0.5)


def fit_logreg(splits: dict[str, Split], config: TrainConfig) -> Fitted:
    """Logistic regression; the penalty strength C with the lowest validation log loss."""
    train, valid = splits["train"], splits["valid"]
    start = time.perf_counter()
    best: tuple[float, float, Any] | None = None
    for c in config.logreg_c:
        model = SklearnScorer(logistic_regression(c).fit(train.x.features, train.y))
        log_candidate(f"logreg C={c:g}", {"C": c}, model, splits)
        loss = valid_log_loss(model, valid)
        if best is None or loss < best[0]:
            best = (loss, c, model)
    assert best is not None
    return Fitted("logreg", best[2], {"C": best[1]}, time.perf_counter() - start)


def fit_gbt(splits: dict[str, Split], config: TrainConfig) -> Fitted:
    """Gradient-boosted trees; the depth and number of trees with the lowest validation
    log loss."""
    train, valid = splits["train"], splits["valid"]
    start = time.perf_counter()
    best: tuple[float, dict[str, Any], Any] | None = None
    for params in config.gbt_grid:
        estimator = gradient_boosted_trees(seed=config.seed, **params)
        model = SklearnScorer(estimator.fit(train.x.features, train.y))
        label = ", ".join(f"{k}={v}" for k, v in params.items())
        log_candidate(f"gbt {label}", params, model, splits)
        loss = valid_log_loss(model, valid)
        if best is None or loss < best[0]:
            best = (loss, params, model)
    assert best is not None
    return Fitted("gbt", best[2], dict(best[1]), time.perf_counter() - start)


def fit_net(splits: dict[str, Split], config: TrainConfig) -> Fitted:
    """The neural network on features + summary embedding (early stopping on the
    validation loss). A features-only twin is logged as a nested run for comparison."""
    train, valid = splits["train"], splits["valid"]
    net_params = {k: v for k, v in vars(config.net).items()}
    with mlflow.start_run(run_name="torch features only", nested=True):
        mlflow.set_tag("role", "ablation")
        twin, twin_history = fit_torch(
            train.x, train.y, valid.x, valid.y, config.net, config.seed, use_embeddings=False
        )
        mlflow.log_params({**net_params, "use_embeddings": False})
        for split in ("valid", "test"):
            score = twin.predict_proba(splits[split].x)
            mlflow.log_metrics(
                {
                    f"{split}_{k}": v
                    for k, v in finite(ranking_metrics(splits[split].y, score)).items()
                }
            )
    start = time.perf_counter()
    scorer, history = fit_torch(train.x, train.y, valid.x, valid.y, config.net, config.seed)
    seconds = time.perf_counter() - start
    params = {
        **net_params,
        "use_embeddings": scorer.needs_embeddings,
        "embed_model": config.embed_model,
        "best_epoch": history.best_epoch,
        "epochs_run": len(history.train_loss),
    }
    curves = {"epoch_train_loss": history.train_loss, "epoch_valid_loss": history.valid_loss}
    return Fitted("torch", scorer, params, seconds, curves)


TRAINERS: dict[str, Callable[[dict[str, Split], TrainConfig], Fitted]] = {
    "rule": fit_rule,
    "logreg": fit_logreg,
    "gbt": fit_gbt,
    "torch": fit_net,
}


# --- choice, logging, registration ------------------------------------------------------


def add_gaps(evaluations: dict[str, Evaluation], y_test: np.ndarray, seed: int) -> str:
    """Add each model's paired-bootstrap gap in test AP to the best model (`test_ap_gap`,
    `test_ap_gap_low`, `test_ap_gap_high`) to its metrics; returns the best model's name."""
    best = max(evaluations, key=lambda n: evaluations[n].metrics["test_average_precision"])
    for evaluation in evaluations.values():
        gap, low, high = paired_gap_interval(
            y_test, evaluations[best].test_scores, evaluation.test_scores, seed
        )
        evaluation.metrics.update(test_ap_gap=gap, test_ap_gap_low=low, test_ap_gap_high=high)
    return best


def select_model(
    results: dict[str, dict[str, float]], margin: float, rule: str = "simplest_not_worse"
) -> tuple[str, str]:
    """The model to register, by `rule` (one of `SELECT_RULES`, fixed before training).

    `simplest_not_worse`: the simplest model the test set can't tell from the best one. A
    model qualifies when the 95 % paired-bootstrap interval of its gap in test average
    precision to the best model reaches down to `margin` (0: "not significantly worse");
    names are tried in `MODELS` order, simplest first. `best_test_ap`: the best model.
    `results` maps model name -> metrics (with `test_ap_gap_low` from `add_gaps`).
    Returns (name, reason)."""
    if rule not in SELECT_RULES:
        raise ValueError(f"unknown selection rule {rule!r} (choose from {list(SELECT_RULES)})")
    ap = {name: m["test_average_precision"] for name, m in results.items()}
    best = max(ap, key=lambda name: ap[name])
    if rule == "best_test_ap":
        return best, f"highest test average precision ({ap[best]:.3f})"
    for name in sorted(ap, key=MODELS.index):
        m = results[name]
        if name == best:
            return name, f"highest test average precision ({ap[name]:.3f})"
        if m["test_ap_gap_low"] <= margin:
            return name, (
                f"test average precision {ap[name]:.3f} vs. {ap[best]:.3f} for {best}: the "
                f"gap's 95% interval [{m['test_ap_gap_low']:.3f}, {m['test_ap_gap_high']:.3f}] "
                f"reaches {margin:g}, so not clearly worse, and simpler"
            )
    return best, "highest test average precision"  # unreachable: the best qualifies


TABLE_COLUMNS = [
    ("test_average_precision", "test AP"),
    ("test_average_precision_low", "AP 95% low"),
    ("test_average_precision_high", "AP 95% high"),
    ("test_ap_gap", "gap to best"),
    ("test_ap_gap_low", "gap 95% low"),
    ("test_ap_gap_high", "gap 95% high"),
    ("test_roc_auc", "ROC AUC"),
    ("test_brier", "Brier"),
    ("test_ece", "ECE"),
    ("threshold", "threshold"),
    ("test_precision", "precision"),
    ("test_recall", "recall"),
    ("test_f1", "F1"),
    ("valid_average_precision", "valid AP"),
    ("train_average_precision", "train AP"),
    ("train_seconds", "train s"),
    ("latency_ms_per_spike", "ms/spike"),
]


def comparison_table(results: dict[str, dict[str, float]]) -> pd.DataFrame:
    rows = []
    for name in sorted(results, key=MODELS.index):
        m = results[name]
        rows.append({"model": name, **{title: m.get(key, np.nan) for key, title in TABLE_COLUMNS}})
    return pd.DataFrame(rows)


def markdown_table(table: pd.DataFrame) -> str:
    header = "| " + " | ".join(table.columns) + " |"
    rule = "|" + "|".join("---" if c == "model" else "---:" for c in table.columns) + "|"
    lines = [header, rule]
    for _, row in table.iterrows():
        cells = [row["model"]] + [
            "" if pd.isna(v) else (f"{v:.3f}" if abs(v) < 100 else f"{v:.0f}")
            for v in row.drop("model")
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def pip_requirements() -> list[str]:
    """The packages a loaded model needs (listed, so MLflow doesn't have to guess)."""
    from importlib.metadata import version

    names = ["mlflow", "scikit-learn", "numpy", "pandas", "torch", "sentence-transformers"]
    return [f"{name}=={version(name)}" for name in names]


def log_model(wrapper: NewsClassifierModel, example: pd.DataFrame) -> str:
    """Log the pyfunc model to the active run; returns its model URI."""
    output = pd.DataFrame({"score": [0.5] * len(example), "is_news": [True] * len(example)})
    signature = infer_signature(example[INPUT_COLUMNS], output)
    info = mlflow.pyfunc.log_model(
        name="model",
        python_model=wrapper,
        signature=signature,
        pip_requirements=pip_requirements(),
    )
    return info.model_uri


def ensure_experiment(name: str, artifact_location: str | None = None) -> str:
    """The experiment's id, creating it if needed. On the tracking server leave
    `artifact_location` None (the server stores artifacts in S3); for a local SQLite
    store pass a folder URI."""
    experiment = mlflow.get_experiment_by_name(name)
    if experiment is not None:
        return experiment.experiment_id
    return mlflow.create_experiment(name, artifact_location=artifact_location)


@dataclass
class TrainReport:
    parent_run_id: str
    run_ids: dict[str, str]
    model_uris: dict[str, str]
    results: dict[str, dict[str, float]]
    table: pd.DataFrame
    chosen: str
    reason: str
    registered_version: str | None
    excluded: dict[str, int]


def train_one(
    name: str,
    splits: dict[str, Split],
    config: TrainConfig,
    dataset: DatasetInfo,
    work: Path,
) -> tuple[Evaluation, str, str]:
    """Train, evaluate and log one model in its own nested run; (evaluation, run, uri)."""
    with mlflow.start_run(run_name=MODEL_TITLES[name], nested=True) as run:
        mlflow.set_tags({**dataset.tags(), "role": "model", "model_type": name})
        fitted = TRAINERS[name](splits, config)
        mlflow.log_params({"model_type": name, **fitted.params})
        for key, values in fitted.curves.items():
            for step, value in enumerate(values):
                mlflow.log_metric(key, value, step=step)
        evaluation = evaluate_model(
            name,
            fitted.scorer.predict_proba,
            {s: (splits[s].x, splits[s].y) for s in SPLITS},
            config.target_precision,
            work / name,
            config.seed,
            fixed_threshold=fitted.threshold,
        )
        evaluation.metrics["train_seconds"] = fitted.train_seconds
        log_evaluation(evaluation, work / name)
        embed_model = config.embed_model if fitted.scorer.needs_embeddings else None
        wrapper = NewsClassifierModel(fitted.scorer, evaluation.threshold, embed_model)
        uri = log_model(wrapper, splits["test"].frame.head(5))
        logger.info(
            "%s: test AP %.3f, threshold %.3f (%s)",
            name,
            evaluation.metrics["test_average_precision"],
            evaluation.threshold,
            evaluation.threshold_rule,
        )
        return evaluation, run.info.run_id, uri


def register(
    model_uri: str,
    model_name: str,
    tags: dict[str, str],
    description: str,
    supersede: bool = True,
) -> str:
    """Register the model as a new version of `model_name`; returns the version.

    With `supersede` (a training session by hand), the older versions are marked as
    superseded by it. Retraining (spec 07c) passes False: its new version is only a
    challenger until it has beaten the champion (`wikipulse.ml.registry`)."""
    version = mlflow.register_model(model_uri, model_name, tags=tags)
    mlflow.MlflowClient().update_model_version(model_name, version.version, description)
    if supersede:
        supersede_older_versions(model_name, str(version.version))
    return str(version.version)


def supersede_older_versions(model_name: str, new_version: str) -> list[str]:
    """Tag `new_version` `status=latest` and every older, not yet superseded version
    `status=superseded`, `superseded_by=<new_version>` (with a note in its description).
    Versions are history and stay in the registry; the tags say which one is the newest
    choice. Which version is *served* is spec 07c's business (aliases). Returns the
    versions marked now."""
    client = mlflow.MlflowClient()
    marked = []
    for old in client.search_model_versions(f"name = '{model_name}'"):
        if int(old.version) >= int(new_version) or old.tags.get("status") == "superseded":
            continue
        client.set_model_version_tag(model_name, old.version, "status", "superseded")
        client.set_model_version_tag(model_name, old.version, "superseded_by", new_version)
        note = f"Superseded by version {new_version}."
        description = f"{note} {old.description}" if old.description else note
        client.update_model_version(model_name, old.version, description)
        marked.append(str(old.version))
    client.set_model_version_tag(model_name, new_version, "status", "latest")
    if marked:
        logger.info("%s: versions %s superseded by %s", model_name, marked, new_version)
    return sorted(marked, key=int)


def train_all(
    frames: dict[str, pd.DataFrame],
    dataset: DatasetInfo,
    embedder: Embedder | None,
    config: TrainConfig,
    experiment: str,
    model_name: str,
    register_best: bool = True,
    artifact_location: str | None = None,
    supersede: bool = True,
    extra_tags: dict[str, str] | None = None,
) -> TrainReport:
    """Everything from the dataset's tables to the registered model (see the module
    docstring). Uses the tracking URI set with `mlflow.set_tracking_uri`. `supersede` and
    `extra_tags` (on the parent run and the registered version) are for retraining."""
    needs_text = "torch" in config.models and embedder is not None
    embed_seconds = 0.0

    def embed(texts: list[str]) -> np.ndarray:
        nonlocal embed_seconds
        start = time.perf_counter()
        matrix = embed_texts(texts, embedder)
        embed_seconds += time.perf_counter() - start
        return matrix

    splits = make_splits(frames, embed if needs_text else None)
    if "torch" in config.models and not needs_text:
        raise ValueError("the neural model needs an embedder for the summary texts")
    excluded = {name: splits[name].excluded for name in SPLITS}
    logger.info(
        "Known labels: %s; left out (unknown label): %s",
        {n: len(splits[n].y) for n in SPLITS},
        excluded,
    )
    # The active experiment: nested runs go to it too (without it they land in "Default").
    mlflow.set_experiment(experiment_id=ensure_experiment(experiment, artifact_location))
    with (
        tempfile.TemporaryDirectory() as tmp,
        mlflow.start_run(run_name=f"train {dataset.version}") as parent,
    ):
        work = Path(tmp)
        mlflow.set_tags(
            {
                **dataset.tags(),
                "role": "comparison",
                "labels": "llm+portal",
                "selection_rule": config.select_rule,
                "selection_rule_text": SELECT_RULES[config.select_rule],
                **(extra_tags or {}),
            }
        )
        mlflow.log_params(
            {
                **data_params(frames, splits),
                "seed": config.seed,
                "target_precision": config.target_precision,
                "select_rule": config.select_rule,
                "select_margin": config.select_margin,
                "models": ",".join(config.models),
            }
        )
        known = sum(len(splits[n].y) for n in SPLITS)
        if needs_text:
            mlflow.log_metric("embed_ms_per_spike", 1000 * embed_seconds / max(known, 1))

        evaluations, run_ids, uris = {}, {}, {}
        for name in config.models:
            evaluations[name], run_ids[name], uris[name] = train_one(
                name, splits, config, dataset, work
            )
        add_gaps(evaluations, splits["test"].y, config.seed)
        results = {name: e.metrics for name, e in evaluations.items()}
        for name, e in evaluations.items():
            gaps = ("test_ap_gap", "test_ap_gap_low", "test_ap_gap_high")
            mlflow.log_metrics(finite({f"{name}_{k}": e.metrics[k] for k in gaps}))
        table = comparison_table(results)
        chosen, reason = select_model(results, config.select_margin, config.select_rule)
        (work / "comparison.md").write_text(markdown_table(table), encoding="utf-8", newline="\n")
        table.to_csv(work / "comparison.csv", index=False, lineterminator="\n")
        y_test = splits["test"].y
        plot_pr_curves(
            {n: (y_test, e.test_scores) for n, e in evaluations.items()}, work / "pr_curves.png"
        )
        for file in ("comparison.md", "comparison.csv", "pr_curves.png"):
            mlflow.log_artifact(str(work / file))
        mlflow.set_tags({"chosen_model": chosen, "chosen_reason": reason})

        version = None
        if register_best:
            m = results[chosen]
            tags = {
                **dataset.tags(),
                "model_type": chosen,
                "test_average_precision": f"{m['test_average_precision']:.4f}",
                "threshold": f"{m['threshold']:.4f}",
                "selection_rule": config.select_rule,
                "select_margin": f"{config.select_margin:g}",
                "training_run": parent.info.run_id,
                **(extra_tags or {}),
            }
            description = f"{MODEL_TITLES[chosen]}: {reason}. Trained on {dataset.version}."
            version = register(uris[chosen], model_name, tags, description, supersede)
            mlflow.set_tag("registered_version", f"{model_name}/{version}")
            logger.info("Registered %s as %s version %s (%s)", chosen, model_name, version, reason)
    return TrainReport(
        parent.info.run_id, run_ids, uris, results, table, chosen, reason, version, excluded
    )


# --- the command ------------------------------------------------------------------------


def load_dataset(
    settings: Settings, version: str | None
) -> tuple[dict[str, pd.DataFrame], DatasetInfo]:
    """The three splits of a dataset version (default: the newest) as pandas tables."""
    from wikipulse.ml.dataset import DatasetStore

    store = DatasetStore.from_settings(settings)
    versions = store.versions()
    if not versions:
        raise SystemExit("No dataset yet: uv run python -m wikipulse.ml.dataset build")
    version = version or f"v{versions[-1]}"
    manifest = store.manifest(version)
    frames = {split: store.read(version, split).to_pandas() for split in SPLITS}
    git = manifest.get("git") or {}
    info = DatasetInfo(version, manifest["fingerprint"], git.get("commit"), git.get("dirty"))
    return frames, info


def add_summary_texts(
    settings: Settings, frames: dict[str, pd.DataFrame], offline: bool
) -> dict[str, pd.DataFrame]:
    """Add `summary_text` (title, description, lead at the cutoff revision) to every row.
    Fetches the revisions' wikitext the first time (cached); `offline` uses the cache only."""
    from wikipulse.eval.wiki_client import WikiClient
    from wikipulse.ml.summaries import LeadCache, fetch_leads, summary_texts

    cache = LeadCache(settings.data_dir / "cache" / "ml" / "leads")
    revids = {
        int(r) for frame in frames.values() for r in frame["page_revision_id"].dropna().tolist()
    }
    if offline:
        leads = {r: lead for r in revids if (lead := cache.get(r)) is not None}
    else:
        cache_dir = settings.data_dir / "cache" / "ml"
        with WikiClient(settings.user_agent, cache_dir, settings.wiki_api_min_interval_s) as client:
            leads = fetch_leads(client, settings.wiki_api_url, revids, cache)
    logger.info("Summaries: %d of %d revisions have a lead", len(leads), len(revids))
    result = {}
    for split, frame in frames.items():
        revisions = [None if pd.isna(r) else int(r) for r in frame["page_revision_id"]]
        texts = summary_texts(frame["title"].tolist(), revisions, leads)
        result[split] = frame.assign(**{TEXT_COLUMN: texts})
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", help="dataset version, e.g. v2 (default: the newest)")
    parser.add_argument("--tracking-uri", help="MLflow tracking URI (default MLFLOW_TRACKING_URI)")
    parser.add_argument("--experiment", help="experiment name (default ML_EXPERIMENT)")
    parser.add_argument("--model-name", help="registered model name (default ML_MODEL_NAME)")
    parser.add_argument("--no-register", action="store_true", help="train and log only")
    parser.add_argument(
        "--models", default=",".join(MODELS), help=f"comma-separated subset of {','.join(MODELS)}"
    )
    parser.add_argument(
        "--offline", action="store_true", help="summaries from the cache only (no API requests)"
    )
    return parser.parse_args(argv)


def local_artifact_location(tracking_uri: str, data_dir: Path) -> str | None:
    """Where a local store (`sqlite:` or a folder) keeps new experiments' artifacts:
    `<data_dir>/mlruns`, not MLflow's default `./mlruns` in the working directory (which
    would land in the repository). None for a tracking server, which stores them itself."""
    if tracking_uri.startswith(("http://", "https://", "databricks")):
        return None
    return (Path(data_dir).resolve() / "mlruns").as_uri()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # MLflow prints run links with an emoji; a Windows console or a redirected stdout
    # (cp1252) can't encode it and the run would crash at its end.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    settings = get_settings()
    setup_logging(settings.log_level)
    models = tuple(m.strip() for m in args.models.split(",") if m.strip())
    unknown = [m for m in models if m not in MODELS]
    if unknown:
        raise SystemExit(f"Unknown models: {unknown} (choose from {MODELS})")
    tracking_uri = args.tracking_uri or settings.mlflow_tracking_uri
    mlflow.set_tracking_uri(tracking_uri)
    frames, dataset = load_dataset(settings, args.dataset)
    embedder = None
    if "torch" in models:
        frames = add_summary_texts(settings, frames, args.offline)
        embedder = Embedder(settings.events_embed_model)
    else:
        frames = {k: f.assign(**{TEXT_COLUMN: f["title"]}) for k, f in frames.items()}
    config = TrainConfig(
        seed=settings.ml_seed,
        target_precision=settings.ml_target_precision,
        select_rule=settings.ml_select_rule,
        select_margin=settings.ml_select_margin,
        embed_model=settings.events_embed_model,
        models=tuple(sorted(models, key=MODELS.index)),
    )
    report = train_all(
        frames,
        dataset,
        embedder,
        config,
        args.experiment or settings.ml_experiment,
        args.model_name or settings.ml_model_name,
        register_best=not args.no_register,
        artifact_location=local_artifact_location(tracking_uri, settings.data_dir),
    )
    print(f"Dataset {dataset.version} (fingerprint {dataset.fingerprint[:12]})")
    print(f"Left out, unknown label: {report.excluded}")
    print(markdown_table(report.table))
    print(f"Chosen: {report.chosen} ({report.reason})")
    if report.registered_version:
        print(
            f"Registered: {args.model_name or settings.ml_model_name} v{report.registered_version}"
        )
    print(f"Parent run: {report.parent_run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
