"""The retraining job's pieces that need no registry: the promotion rule, the
champion/challenger comparison on the same rows, and the command line."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from wikipulse.config import Settings
from wikipulse.jobs.retrain import (
    PROMOTE,
    REJECT,
    Comparison,
    compare_models,
    decide,
    existing_challenger,
    extend_window,
    parse_args,
    parse_models,
    retrain_key,
    step_extend,
)

BASE = Comparison(
    dataset="v3",
    rows=132,
    positives=79,
    champion_version="3",
    challenger_version="4",
    champion_ap=0.938,
    challenger_ap=0.958,
    gap=0.020,
    gap_low=-0.001,
    gap_high=0.046,
    champion_precision=0.87,
    champion_recall=0.86,
    challenger_precision=0.82,
    challenger_recall=0.91,
)


def test_a_challenger_better_by_the_margin_is_promoted():
    decision, reason = decide(BASE, margin=0.01, min_rows=50)
    assert decision == PROMOTE
    assert "0.958" in reason and "0.938" in reason and "+0.020" in reason


def test_a_smaller_gain_or_a_tie_keeps_the_champion():
    assert decide(BASE, margin=0.03, min_rows=50)[0] == REJECT
    tie = replace(BASE, challenger_ap=0.938, gap=0.0)
    decision, reason = decide(tie, margin=0.01, min_rows=50)
    assert decision == REJECT and "below the margin" in reason
    worse = replace(BASE, challenger_ap=0.90, gap=-0.038)
    assert decide(worse, margin=0.0, min_rows=50)[0] == REJECT
    assert decide(tie, margin=0.0, min_rows=50)[0] == PROMOTE  # margin 0: a tie is enough


def test_too_few_or_one_class_test_rows_reject():
    assert "too few" in decide(replace(BASE, rows=20), 0.01, 50)[1]
    assert "one class" in decide(replace(BASE, positives=0), 0.01, 50)[1]
    assert decide(replace(BASE, positives=132), 0.01, 50)[0] == REJECT


def test_without_a_champion_the_challenger_is_promoted():
    assert decide(replace(BASE, champion_version=None), 0.01, 50) == (
        PROMOTE,
        "there is no champion yet",
    )


def test_the_champion_itself_is_never_a_challenger():
    assert decide(replace(BASE, challenger_version="3"), 0.0, 1)[0] == REJECT


def test_a_champion_that_saw_the_test_rows_is_mentioned():
    reason = decide(replace(BASE, champion_trained_on_test_rows=7), 0.01, 50)[1]
    assert "trained on 7 of these rows" in reason


class Fixed:
    """A stand-in for a loaded model: scores from a column of the table."""

    def __init__(self, version: str, column: str, threshold: float = 0.5) -> None:
        self.version = version
        self.column = column
        self.threshold = threshold

    def predict(self, frame):
        scores = frame[self.column].to_numpy(dtype=float)
        return pd.DataFrame({"score": scores, "is_news": scores >= self.threshold})


def rows() -> pd.DataFrame:
    rng = np.random.default_rng(5)
    label = rng.integers(0, 2, 200)
    good = np.clip(label * 0.6 + rng.normal(0.2, 0.15, 200), 0, 1)  # separates well
    weak = np.clip(label * 0.2 + rng.normal(0.4, 0.25, 200), 0, 1)
    return pd.DataFrame({"label": label, "good": good, "weak": weak})


def test_both_models_are_compared_on_the_same_rows():
    frame = rows()
    c = compare_models(Fixed("3", "weak"), Fixed("4", "good"), frame, "v3", seed=1, seen=0)
    assert (c.rows, c.positives) == (200, int(frame["label"].sum()))
    assert c.challenger_ap > c.champion_ap
    assert c.gap == pytest.approx(c.challenger_ap - c.champion_ap)
    assert c.gap_low <= c.gap <= c.gap_high
    assert decide(c, margin=0.01, min_rows=50)[0] == PROMOTE
    reverse = compare_models(Fixed("3", "good"), Fixed("4", "weak"), frame, "v3", seed=1)
    assert reverse.gap == pytest.approx(-c.gap)
    assert decide(reverse, margin=0.01, min_rows=50)[0] == REJECT


def test_comparison_without_a_champion():
    c = compare_models(None, Fixed("1", "good"), rows(), "v1", seed=1)
    assert c.champion_version is None and np.isnan(c.champion_ap)
    assert decide(c, 0.01, 50)[0] == PROMOTE


def test_a_retried_training_reuses_the_version_of_this_week_and_dataset():
    versions = [
        SimpleNamespace(
            version=4, tags={"retrain_key": "retrain-2026-11-02", "dataset_version": "v3"}
        ),
        SimpleNamespace(
            version=5, tags={"retrain_key": "retrain-2026-11-09", "dataset_version": "v3"}
        ),
    ]
    client = SimpleNamespace(search_model_versions=lambda query: versions)
    assert existing_challenger(client, "m", "retrain-2026-11-02", "v3") == "4"
    assert existing_challenger(client, "m", "retrain-2026-11-02", "v4") is None
    assert retrain_key(pd.Timestamp("2026-11-02").date()) == "retrain-2026-11-02"


def test_the_command_line_checks_values_from_other_tasks():
    args = parse_args(["evaluate", "--date", "2026-11-02", "--dataset", "v3", "--challenger", "4"])
    assert (args.step, args.dataset, args.challenger) == ("evaluate", "v3", "4")
    assert parse_args(["build", "--date", "2026-11-02", "--lake-prefix", ""]).lake_prefix == ""
    for bad in (
        ["train", "--date", "2026-11-02"],  # no dataset
        ["train", "--date", "2026-11-02", "--dataset", "v3; rm -rf /"],
        ["promote", "--date", "2026-11-02", "--challenger", "None"],  # an empty XCom
    ):
        with pytest.raises(SystemExit):
            parse_args(bad)


def test_the_models_to_retrain_are_checked():
    assert parse_models("logreg, gbt") == ("logreg", "gbt")
    assert parse_models("torch,rule ,logreg") == ("rule", "logreg", "torch")  # MODELS order
    with pytest.raises(ValueError, match="unknown model"):
        parse_models("logreg,xgboost")
    with pytest.raises(ValueError, match="empty"):
        parse_models(" , ")


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def test_the_history_is_extended_from_its_last_hour_to_the_last_complete_hour():
    since, until = extend_window(utc(9, 21), now=utc(16, 3, 30))
    assert (since, until) == (utc(9, 22), utc(16, 3))
    assert extend_window(utc(16, 2), now=utc(16, 3, 5)) is None  # 03:00 is not settled yet
    assert extend_window(utc(16, 2), now=utc(16, 3, 30)) is None  # the lake is current


def test_history_older_than_the_stream_keeps_starts_inside_its_retention():
    since, until = extend_window(utc(1, 0), now=utc(20, 12, 30))
    assert since == utc(8, 13) and until == utc(20, 12)
    assert extend_window(None, now=utc(20, 12, 30))[0] == utc(8, 13)


def test_extend_backfills_the_missing_hours_and_skips_the_live_archive(monkeypatch):
    from wikipulse.lake import backfill
    from wikipulse.lake import store as lake_store

    calls = []

    class FakeStore:
        def __init__(self, prefix):
            self.prefix = prefix

        def hours(self):
            return [utc(9, 20), utc(9, 21)]

    async def fake_run(args, settings):
        calls.append((args.prefix, args.since, args.until))
        return SimpleNamespace(kept=5)

    monkeypatch.setattr(
        lake_store.LakeStore, "from_settings", classmethod(lambda cls, s, p: FakeStore(p))
    )
    monkeypatch.setattr(backfill, "run", fake_run)
    settings = Settings(_env_file=None, ml_lake_prefix="history/edits", lake_prefix="edits")
    assert step_extend(settings, None) == 0
    [(prefix, since, until)] = calls
    assert prefix == "history/edits" and since == utc(9, 22).isoformat()
    assert step_extend(settings, "edits") == 0  # the archiver's lake: not touched
    assert len(calls) == 1
