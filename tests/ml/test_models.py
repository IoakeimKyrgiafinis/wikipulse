"""The models on their own: preprocessing, the training loop, the pyfunc wrapper."""

import numpy as np
import pandas as pd

from tests.ml.synthetic import fake_embedder, synthetic_frames
from wikipulse.ml.models import (
    INPUT_COLUMNS,
    ModelInput,
    NetConfig,
    NewsClassifierModel,
    RuleScorer,
    SklearnScorer,
    features_of,
    fit_torch,
    gradient_boosted_trees,
    logistic_regression,
    signed_log,
)
from wikipulse.ml.summaries import embed_texts


def split(name: str, frames: dict[str, pd.DataFrame]) -> tuple[ModelInput, np.ndarray]:
    frame = frames[name][frames[name]["label"].notna()]
    embeddings = embed_texts(frame["summary_text"].tolist(), fake_embedder())
    x = ModelInput(features_of(frame), frame["default_rule_fired"].to_numpy(bool), embeddings)
    return x, frame["label"].to_numpy(dtype=np.int64)


def test_signed_log_squeezes_tails_and_keeps_nan():
    out = signed_log(np.array([0.0, 9.0, -9.0, np.nan]))
    assert out[0] == 0 and out[1] == -out[2] == np.log(10)
    assert np.isnan(out[3])


def test_rule_scores_are_the_rule():
    x = ModelInput(np.zeros((3, 2)), np.array([True, False, True]))
    assert RuleScorer().predict_proba(x).tolist() == [1.0, 0.0, 1.0]


def test_sklearn_models_handle_missing_values():
    frames = synthetic_frames()
    x, y = split("train", frames)
    assert np.isnan(x.features).any()
    for estimator in (logistic_regression(1.0), gradient_boosted_trees(2, 20, 0.1, seed=0)):
        scores = SklearnScorer(estimator.fit(x.features, y)).predict_proba(x)
        assert scores.shape == y.shape and ((0 <= scores) & (scores <= 1)).all()


def test_training_loop_learns_and_is_reproducible():
    frames = synthetic_frames()
    train, y_train = split("train", frames)
    valid, y_valid = split("valid", frames)
    config = NetConfig(max_epochs=60, patience=60)
    first, history = fit_torch(train, y_train, valid, y_valid, config, seed=5)
    second, _ = fit_torch(train, y_train, valid, y_valid, config, seed=5)
    assert history.train_loss[-1] < history.train_loss[0]  # it learned something
    assert len(history.valid_loss) == 60
    assert np.allclose(first.predict_proba(valid), second.predict_proba(valid), atol=1e-6)
    assert first.needs_embeddings


def test_early_stopping_keeps_the_best_epoch():
    frames = synthetic_frames()
    train, y_train = split("train", frames)
    valid, y_valid = split("valid", frames)
    config = NetConfig(max_epochs=500, patience=5, learning_rate=0.05)
    _, history = fit_torch(train, y_train, valid, y_valid, config, seed=1)
    assert len(history.valid_loss) == history.best_epoch + 6  # stopped 5 epochs later
    assert history.valid_loss[history.best_epoch] == min(history.valid_loss)


def test_wrapper_scores_a_table_and_never_pickles_the_embedder():
    frames = synthetic_frames()
    train, y_train = split("train", frames)
    valid, y_valid = split("valid", frames)
    scorer, _ = fit_torch(train, y_train, valid, y_valid, NetConfig(max_epochs=5), seed=0)
    wrapper = NewsClassifierModel(scorer, 0.5, "fake-model")
    wrapper._embedder = fake_embedder()
    out = wrapper.predict(None, frames["test"][INPUT_COLUMNS].head(3))
    assert out["is_news"].tolist() == (out["score"] >= 0.5).tolist()
    assert wrapper.__getstate__()["_embedder"] is None
