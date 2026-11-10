"""The model health collector: drift reports and stored scores as gauges, at scrape time."""

from datetime import UTC, datetime

import psycopg
from prometheus_client import CollectorRegistry

from wikipulse.metrics.models import ModelHealth, ModelHealthCollector

REPORT = {
    "status": "drift",
    "score": {"psi": 0.16, "noise_floor": 0.35},
    "drifted_features": ["edits_before_start", "editors"],
    "features": [
        {"name": "edits_before_start", "psi": 2.9},
        {"name": "editors", "psi": None},  # NaN is stored as null
    ],
}
DRIFT_ROW = {
    "model_name": "news-classifier",
    "model_version": "3",
    "run_id": "07a-default",
    "status": "drift",
    "rows": 100,
    "period_end": datetime(2026, 11, 10, tzinfo=UTC),
    "report": REPORT,
}
SCORE_ROW = {
    "model_name": "news-classifier",
    "model_version": "3",
    "run_id": "07a-default",
    "scores": 292,
    "mean_score": 0.49,
    "news_share": 0.25,
}
LABELS = {"model_name": "news-classifier", "model_version": "3", "run_id": "07a-default"}


def registry_with(read) -> CollectorRegistry:
    registry = CollectorRegistry()
    registry.register(ModelHealthCollector(read))
    return registry


def test_drift_reports_and_scores_become_gauges() -> None:
    registry = registry_with(lambda: ModelHealth([DRIFT_ROW], [SCORE_ROW]))
    get = registry.get_sample_value
    assert get("wikipulse_model_health_up") == 1
    assert get("wikipulse_drift_detected", LABELS) == 1
    assert get("wikipulse_drift_report_rows", LABELS) == 100
    assert get("wikipulse_drift_score_psi", LABELS) == 0.16
    assert get("wikipulse_drift_score_noise_floor", LABELS) == 0.35
    assert get("wikipulse_drift_features_drifted", LABELS) == 2
    period_end = get("wikipulse_drift_report_period_end_timestamp_seconds", LABELS)
    assert period_end == DRIFT_ROW["period_end"].timestamp()
    feature = {**LABELS, "feature": "edits_before_start"}
    assert get("wikipulse_drift_feature_psi", feature) == 2.9
    unknown = get("wikipulse_drift_feature_psi", {**LABELS, "feature": "editors"})
    assert unknown != unknown  # NaN
    assert get("wikipulse_model_stored_scores", LABELS) == 292
    assert get("wikipulse_model_stored_score_mean", LABELS) == 0.49
    assert get("wikipulse_model_stored_news_share", LABELS) == 0.25


def test_an_ok_report_is_not_drift() -> None:
    row = {**DRIFT_ROW, "status": "ok"}
    registry = registry_with(lambda: ModelHealth([row], []))
    assert registry.get_sample_value("wikipulse_drift_detected", LABELS) == 0


def test_postgres_down_exports_only_up_zero() -> None:
    def down() -> ModelHealth:
        raise psycopg.OperationalError("connection refused")

    registry = registry_with(down)
    assert registry.get_sample_value("wikipulse_model_health_up") == 0
    assert registry.get_sample_value("wikipulse_drift_detected", LABELS) is None


def test_a_malformed_report_is_skipped_not_fatal() -> None:
    other = {**LABELS, "run_id": "other"}
    no_name = {**DRIFT_ROW, "report": {**REPORT, "features": [{"psi": 1.0}]}}
    bad_psi = {**DRIFT_ROW, "run_id": "bad-psi", "report": {"score": {"psi": "high"}}}
    good = {**DRIFT_ROW, "run_id": "other"}
    bad_score = {**SCORE_ROW, "run_id": "bad", "mean_score": "x"}
    registry = registry_with(lambda: ModelHealth([no_name, bad_psi, good], [bad_score, SCORE_ROW]))
    get = registry.get_sample_value
    assert get("wikipulse_model_health_up") == 1
    assert get("wikipulse_drift_detected", LABELS) is None  # the row without a feature name
    assert get("wikipulse_drift_report_rows", LABELS) is None  # nothing half-added
    assert get("wikipulse_drift_detected", {**LABELS, "run_id": "bad-psi"}) is None
    assert get("wikipulse_drift_detected", other) == 1  # the good row survives
    assert get("wikipulse_model_stored_scores", {**LABELS, "run_id": "bad"}) is None
    assert get("wikipulse_model_stored_scores", LABELS) == 292
