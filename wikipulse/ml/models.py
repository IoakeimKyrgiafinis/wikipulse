"""The four news classifiers of spec 07b, behind one small interface.

Every model answers the same question, "how likely is this spike real news?", with a
score between 0 and 1 for each row of a `ModelInput`:

- `RuleScorer`: the current rule-based detector. Score 1 if the live default thresholds
  had fired by the cutoff (`default_rule_fired`), else 0. The baseline to beat.
- `SklearnScorer` with `logistic_regression()`: a weighted sum of the (scaled) features
  through a sigmoid. With `gradient_boosted_trees()`: many small decision trees, each
  correcting the previous ones' errors.
- `TorchScorer`: a small neural network (`NewsNet`) over the features plus the summary
  embedding, trained by `train_net()`, a plain PyTorch training loop.

`NewsClassifierModel` wraps any of them as an MLflow `pyfunc` model, so the registry
holds one kind of thing whatever won, and serving (spec 07c) can load it with
`mlflow.pyfunc.load_model(...)` and call `predict(frame)` on a table with the columns
`INPUT_COLUMNS`.
"""

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import mlflow.pyfunc
import numpy as np
import pandas as pd
import torch
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler
from torch import nn

from wikipulse.events.embed import Embedder
from wikipulse.ml.features import FEATURE_NAMES

logger = logging.getLogger(__name__)

RULE_COLUMN = "default_rule_fired"
TEXT_COLUMN = "summary_text"
INPUT_COLUMNS = [*FEATURE_NAMES, RULE_COLUMN, TEXT_COLUMN]


@dataclass
class ModelInput:
    """What a model may look at for n spikes (all known at the cutoff)."""

    features: np.ndarray  # (n, len(FEATURE_NAMES)) floats, NaN = unknown
    rule: np.ndarray  # (n,) bools: had the live default rule fired by the cutoff?
    embeddings: np.ndarray | None = None  # (n, d) unit vectors of the summary text

    def __len__(self) -> int:
        return len(self.features)

    def take(self, rows: np.ndarray) -> "ModelInput":
        embeddings = None if self.embeddings is None else self.embeddings[rows]
        return ModelInput(self.features[rows], self.rule[rows], embeddings)


def features_of(frame: pd.DataFrame) -> np.ndarray:
    return frame[FEATURE_NAMES].to_numpy(dtype=np.float64)


class Scorer(Protocol):
    """A fitted model: a score in [0, 1] per row."""

    needs_embeddings: bool

    def predict_proba(self, x: ModelInput) -> np.ndarray: ...


# --- 1. the rule ------------------------------------------------------------------------


class RuleScorer:
    needs_embeddings = False

    def predict_proba(self, x: ModelInput) -> np.ndarray:
        return x.rule.astype(np.float64)


# --- 2 and 3. scikit-learn models -------------------------------------------------------


def signed_log(values: np.ndarray) -> np.ndarray:
    """sign(x) * log(1 + |x|): squeezes long tails (page age, bytes) and keeps NaN."""
    return np.sign(values) * np.log1p(np.abs(values))


def preprocessing() -> list[Any]:
    """For models that need numbers on one scale: signed log, then fill the unknowns
    with the training median (plus a 0/1 "was unknown" column each), then standardize."""
    return [
        FunctionTransformer(signed_log),
        SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True),
        StandardScaler(),
    ]


def logistic_regression(c: float) -> Pipeline:
    """Logistic regression; `c` is the inverse strength of the L2 penalty."""
    return make_pipeline(*preprocessing(), LogisticRegression(C=c, max_iter=5000))


def gradient_boosted_trees(
    max_depth: int, max_iter: int, learning_rate: float, seed: int
) -> HistGradientBoostingClassifier:
    """Gradient-boosted trees. They split on raw values and send NaN down a learned
    branch, so they need no scaling and no imputation."""
    return HistGradientBoostingClassifier(
        max_depth=max_depth,
        max_iter=max_iter,
        learning_rate=learning_rate,
        min_samples_leaf=10,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=seed,
    )


class SklearnScorer:
    needs_embeddings = False

    def __init__(self, estimator: Any) -> None:
        self.estimator = estimator

    def predict_proba(self, x: ModelInput) -> np.ndarray:
        return self.estimator.predict_proba(x.features)[:, 1]


# --- 4. the neural network --------------------------------------------------------------


class NewsNet(nn.Module):
    """features (scaled) + summary embedding -> one hidden layer -> one logit.

    The 384 embedding numbers would swamp ~90 feature columns and a few hundred training
    rows, so they first pass through a narrow linear layer (`embed_dim` numbers): the
    network learns which directions of meaning matter.
    """

    def __init__(
        self, n_features: int, n_embed: int, embed_dim: int, hidden: int, dropout: float
    ) -> None:
        super().__init__()
        self.project = nn.Linear(n_embed, embed_dim) if n_embed and embed_dim else None
        width = n_features + (embed_dim if self.project is not None else 0)
        self.body = nn.Sequential(
            nn.Linear(width, hidden),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, 1),
        )

    def forward(self, features: torch.Tensor, embeddings: torch.Tensor) -> torch.Tensor:
        parts = [features]
        if self.project is not None:
            parts.append(self.project(embeddings))
        return self.body(torch.cat(parts, dim=1)).squeeze(1)  # logits, one per row


@dataclass
class NetConfig:
    embed_dim: int = 8
    hidden: int = 32
    dropout: float = 0.2
    learning_rate: float = 1e-3
    weight_decay: float = 1e-3
    batch_size: int = 32
    max_epochs: int = 300
    patience: int = 30  # stop after this many epochs without a better validation loss


@dataclass
class TrainHistory:
    train_loss: list[float] = field(default_factory=list)
    valid_loss: list[float] = field(default_factory=list)
    best_epoch: int = 0


def tensors(features: np.ndarray, embeddings: np.ndarray | None) -> tuple[torch.Tensor, ...]:
    f = torch.as_tensor(features, dtype=torch.float32)
    if embeddings is None:
        embeddings = np.zeros((len(features), 0), dtype=np.float32)
    return f, torch.as_tensor(embeddings, dtype=torch.float32)


def train_net(
    net: NewsNet,
    train: tuple[torch.Tensor, torch.Tensor, torch.Tensor],
    valid: tuple[torch.Tensor, torch.Tensor, torch.Tensor],
    config: NetConfig,
    seed: int,
) -> TrainHistory:
    """The training loop: mini-batches, binary cross-entropy, Adam, early stopping.

    `train` and `valid` are (features, embeddings, labels). At the end the network holds
    the weights of the epoch with the lowest validation loss.
    """
    features, embeddings, labels = train
    loss_fn = nn.BCEWithLogitsLoss()  # sigmoid + cross-entropy in one, numerically stable
    optimizer = torch.optim.Adam(
        net.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay
    )
    shuffler = torch.Generator().manual_seed(seed)
    history = TrainHistory()
    best_loss, best_state = float("inf"), copy.deepcopy(net.state_dict())
    for epoch in range(config.max_epochs):
        net.train()  # dropout on
        order = torch.randperm(len(labels), generator=shuffler)
        total = 0.0
        for start in range(0, len(order), config.batch_size):
            batch = order[start : start + config.batch_size]
            optimizer.zero_grad()  # forget the previous batch's gradients
            logits = net(features[batch], embeddings[batch])  # forward pass
            loss = loss_fn(logits, labels[batch])
            loss.backward()  # gradients of the loss for every weight
            optimizer.step()  # move every weight a little against its gradient
            total += loss.item() * len(batch)
        history.train_loss.append(total / len(order))

        net.eval()  # dropout off
        with torch.no_grad():
            valid_loss = loss_fn(net(valid[0], valid[1]), valid[2]).item()
        history.valid_loss.append(valid_loss)
        if valid_loss < best_loss - 1e-6:  # the best so far: remember these weights
            best_loss = valid_loss
            best_state = copy.deepcopy(net.state_dict())
            history.best_epoch = epoch
        elif epoch - history.best_epoch >= config.patience:
            break
    net.load_state_dict(best_state)
    net.eval()
    return history


class TorchScorer:
    """The fitted preprocessing (as for logistic regression) plus the trained network."""

    def __init__(self, prep: Pipeline, net: NewsNet, uses_embeddings: bool) -> None:
        self.prep = prep
        self.net = net
        self.needs_embeddings = uses_embeddings

    def predict_proba(self, x: ModelInput) -> np.ndarray:
        embeddings = x.embeddings if self.needs_embeddings else None
        features, embedded = tensors(self.prep.transform(x.features), embeddings)
        self.net.eval()
        with torch.no_grad():
            return torch.sigmoid(self.net(features, embedded)).numpy().astype(np.float64)


def fit_torch(
    train: ModelInput,
    y_train: np.ndarray,
    valid: ModelInput,
    y_valid: np.ndarray,
    config: NetConfig,
    seed: int,
    use_embeddings: bool = True,
) -> tuple[TorchScorer, TrainHistory]:
    """Fit the preprocessing on the training rows, then train a fresh `NewsNet`."""
    torch.manual_seed(seed)  # the same seed gives the same initial weights
    prep = make_pipeline(*preprocessing()).fit(train.features)
    use_embeddings = use_embeddings and train.embeddings is not None
    n_embed = train.embeddings.shape[1] if use_embeddings else 0

    def as_tensors(x: ModelInput, y: np.ndarray) -> tuple[torch.Tensor, ...]:
        f, e = tensors(prep.transform(x.features), x.embeddings if use_embeddings else None)
        return f, e, torch.as_tensor(y, dtype=torch.float32)

    train_t = as_tensors(train, y_train)
    net = NewsNet(train_t[0].shape[1], n_embed, config.embed_dim, config.hidden, config.dropout)
    history = train_net(net, train_t, as_tensors(valid, y_valid), config, seed)
    return TorchScorer(prep, net, use_embeddings), history


# --- the MLflow model -------------------------------------------------------------------


class NewsClassifierModel(mlflow.pyfunc.PythonModel):
    """Any scorer as an MLflow pyfunc model: a table in, `score` and `is_news` out.

    The input needs the columns `INPUT_COLUMNS`: the 43 features, `default_rule_fired`
    and `summary_text` (only the neural network reads the text; it embeds it with
    `embed_model`, loaded on first use).
    """

    def __init__(self, scorer: Scorer, threshold: float, embed_model: str | None) -> None:
        self.scorer = scorer
        self.threshold = threshold
        self.embed_model = embed_model
        self._embedder: Embedder | None = None

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        state["_embedder"] = None  # never pickle a loaded embedding model
        return state

    def model_input(self, frame: pd.DataFrame) -> ModelInput:
        embeddings = None
        if self.scorer.needs_embeddings:
            if self._embedder is None:
                self._embedder = Embedder(self.embed_model or "")
            texts = frame[TEXT_COLUMN].fillna("").astype(str).tolist()
            embeddings = np.vstack(self._embedder.embed_many(texts)) if texts else None
        rule = frame[RULE_COLUMN].fillna(False).to_numpy(dtype=bool)
        return ModelInput(features_of(frame), rule, embeddings)

    def predict(self, context: Any, model_input: pd.DataFrame, params: Any = None) -> Any:
        scores = self.scorer.predict_proba(self.model_input(model_input))
        return pd.DataFrame({"score": scores, "is_news": scores >= self.threshold})
