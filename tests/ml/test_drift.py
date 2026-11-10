"""Drift statistics on synthetic data: PSI, KS, and the period's verdict."""

import json
import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from wikipulse.db.scores import ScoredSpike
from wikipulse.ml.drift import (
    bin_edges,
    compare,
    current_table,
    ks_statistic,
    psi,
    render,
    shares,
)
from wikipulse.ml.features import FEATURE_NAMES

RNG = np.random.default_rng(2026)


def table(n: int, shift: float = 0.0, seed: int = 1, score_shift: float = 0.0) -> pd.DataFrame:
    """`n` rows of every feature ~ N(shift, 1), scores ~ Beta, a binary flag."""
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(rng.normal(shift, 1, (n, len(FEATURE_NAMES))), columns=FEATURE_NAMES)
    frame["is_biography"] = (rng.random(n) < 0.3).astype(float)
    score = np.clip(rng.beta(2, 3, n) + score_shift, 0, 1)
    return frame.assign(score=score, is_news=score >= 0.5)


def test_psi_is_zero_for_the_same_sample_and_grows_with_the_shift():
    reference = RNG.normal(0, 1, 2000)
    assert psi(reference, reference) == pytest.approx(0, abs=1e-9)
    small = psi(reference, RNG.normal(0.1, 1, 2000))
    large = psi(reference, RNG.normal(1.0, 1, 2000))
    assert small < 0.1 < 0.25 < large


def test_flags_get_one_bin_per_value():
    reference = np.array([0.0] * 70 + [1.0] * 30)
    assert list(bin_edges(reference)) == [0.5]
    assert list(shares(reference, bin_edges(reference))) == [0.7, 0.3, 0.0]  # + unknown
    current = np.array([0.0] * 30 + [1.0] * 70)
    expected = (0.3 - 0.7) * math.log(0.3 / 0.7) + (0.7 - 0.3) * math.log(0.7 / 0.3)
    assert psi(reference, current) == pytest.approx(expected)


def test_unknown_values_are_a_bin_of_their_own():
    reference = RNG.normal(0, 1, 1000)
    current = reference.copy()
    current[:500] = np.nan  # same known values, but half of them now unknown
    assert psi(reference, current) > 0.25


def test_ks_is_the_largest_gap_between_the_distributions():
    a = np.array([1.0, 2.0, 3.0, 4.0])
    assert ks_statistic(a, a) == 0
    assert ks_statistic(a, a + 10) == 1
    assert ks_statistic(a, np.array([3.0, 4.0, np.nan])) == pytest.approx(0.5)
    assert math.isnan(ks_statistic(a, np.array([np.nan])))


def test_the_same_distribution_is_ok():
    report = compare(table(400, seed=1), table(200, seed=2))
    assert report.status == "ok"
    assert report.drifted_features == [] and not report.score.drifted
    assert report.rows == 200 and report.reference_rows == 400


def test_shifted_features_are_drift():
    report = compare(table(400, seed=1), table(200, shift=1.0, seed=2), max_features=5)
    assert report.status == "drift"
    assert len(report.drifted_features) >= 5
    assert report.features[0].psi >= report.features[-1].psi  # biggest first


def test_a_shifted_score_alone_is_drift():
    report = compare(table(400, seed=1), table(200, seed=2, score_shift=0.4))
    assert report.score.drifted and report.status == "drift"
    assert report.news_share_current > report.news_share_reference


def test_too_few_rows_are_not_judged():
    report = compare(table(400, seed=1), table(10, shift=3.0, seed=2), min_rows=30)
    assert report.status == "too_few_rows"


def test_the_report_is_json_without_nan():
    current = table(50, seed=2)
    current["page_age_days"] = np.nan
    report = compare(table(400, seed=1), current)
    text = json.dumps(report.to_json(), allow_nan=False)
    assert '"drifted_features"' in text
    assert "page_age_days" in render(report, top=50)


def test_stored_scores_become_a_table_like_the_reference():
    cutoff = datetime(2026, 11, 9, tzinfo=UTC)
    features = {name: 1.0 for name in FEATURE_NAMES} | {"page_age_days": None}
    scored = [ScoredSpike(1, "A", cutoff, 0.7, True, features)]
    frame = current_table(scored)
    assert list(frame.columns) == [*FEATURE_NAMES, "score", "is_news"]
    assert math.isnan(frame.loc[0, "page_age_days"]) and frame.loc[0, "edits"] == 1.0
    assert current_table([]).empty
