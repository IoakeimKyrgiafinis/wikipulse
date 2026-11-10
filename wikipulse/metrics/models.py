"""Model health metrics, read from Postgres at scrape time.

The drift check (`wikipulse.ml.drift`) runs once a week as a job and stores its report in
`drift_reports`; it is not running when Prometheus scrapes. So instead of the job pushing
numbers somewhere, a long-running service (the API) *reads* the stored results whenever
Prometheus asks: a custom collector. The same goes for the scores in `spike_scores`, which
come from the live scorer or a backfill.

Exported (all gauges):

- per model and detector run, from the newest drift report:
  `wikipulse_drift_detected` (1 = status drift), `wikipulse_drift_report_rows`,
  `wikipulse_drift_score_psi`, `wikipulse_drift_score_noise_floor`,
  `wikipulse_drift_features_drifted`, `wikipulse_drift_report_period_end_timestamp_seconds`,
  and `wikipulse_drift_feature_psi` per feature;
- per model version and run, from the stored scores: `wikipulse_model_stored_scores`,
  `wikipulse_model_stored_score_mean`, `wikipulse_model_stored_news_share`;
- `wikipulse_model_health_up`: 1 when Postgres answered, 0 when it didn't (then nothing
  else is exported for that scrape, rather than stale numbers).
"""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
from prometheus_client import REGISTRY
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

LATEST_DRIFT_REPORTS = """
SELECT DISTINCT ON (model_name, run_id)
       model_name, model_version, run_id, status, rows, period_end, report
FROM drift_reports
ORDER BY model_name, run_id, period_end DESC, created_at DESC
"""

STORED_SCORES = """
SELECT model_name, model_version, run_id, count(*) AS scores, avg(score) AS mean_score,
       avg(is_news::int) AS news_share
FROM spike_scores
GROUP BY model_name, model_version, run_id
"""

DRIFT_LABELS = ["model_name", "model_version", "run_id"]


@dataclass
class ModelHealth:
    drift_reports: list[dict[str, Any]]  # rows of LATEST_DRIFT_REPORTS
    scores: list[dict[str, Any]]  # rows of STORED_SCORES


def read_model_health(dsn: str) -> ModelHealth:
    """Both queries, on a short-lived connection (one scrape every 15 to 30 s)."""
    with psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row) as conn:
        drift = conn.execute(LATEST_DRIFT_REPORTS).fetchall()
        scores = conn.execute(STORED_SCORES).fetchall()
    return ModelHealth(drift, scores)


def _number(value: Any) -> float:
    """A JSON number from a report, NaN for null (the report stores NaN as null)."""
    return float("nan") if value is None else float(value)


class ModelHealthCollector(Collector):
    """Turns the stored drift reports and scores into gauges at every scrape."""

    def __init__(self, read: Callable[[], ModelHealth]) -> None:
        self._read = read

    def collect(self) -> Iterator[GaugeMetricFamily]:
        up = GaugeMetricFamily(
            "wikipulse_model_health_up", "1 if the drift reports and scores could be read."
        )
        try:
            health = self._read()
        except psycopg.Error as exc:
            logger.warning("Could not read model health from Postgres: %s", exc)
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        yield up
        yield from drift_metrics(health.drift_reports)
        yield from score_metrics(health.scores)


def drift_metrics(reports: list[dict[str, Any]]) -> Iterator[GaugeMetricFamily]:
    def family(name: str, doc: str, extra: list[str] | None = None) -> GaugeMetricFamily:
        return GaugeMetricFamily(name, doc, labels=DRIFT_LABELS + (extra or []))

    detected = family("wikipulse_drift_detected", "1 if the newest drift report says drift.")
    rows = family("wikipulse_drift_report_rows", "Scored spikes in the newest drift report.")
    score_psi = family("wikipulse_drift_score_psi", "PSI of the score distribution.")
    floor = family(
        "wikipulse_drift_score_noise_floor", "PSI the score reaches by chance at these sizes."
    )
    drifted = family("wikipulse_drift_features_drifted", "Features that drifted.")
    period_end = family(
        "wikipulse_drift_report_period_end_timestamp_seconds", "End of the report's period."
    )
    feature_psi = family("wikipulse_drift_feature_psi", "PSI of one feature.", ["feature"])
    for row in reports:
        labels = [row["model_name"], row["model_version"], row["run_id"]]
        report = row["report"] or {}
        detected.add_metric(labels, 1 if row["status"] == "drift" else 0)
        rows.add_metric(labels, row["rows"])
        score = report.get("score") or {}
        score_psi.add_metric(labels, _number(score.get("psi")))
        floor.add_metric(labels, _number(score.get("noise_floor")))
        drifted.add_metric(labels, len(report.get("drifted_features") or []))
        period_end.add_metric(labels, row["period_end"].timestamp())
        for feature in report.get("features") or []:
            feature_psi.add_metric([*labels, feature["name"]], _number(feature.get("psi")))
    yield from (detected, rows, score_psi, floor, drifted, period_end, feature_psi)


def score_metrics(rows: list[dict[str, Any]]) -> Iterator[GaugeMetricFamily]:
    stored = GaugeMetricFamily(
        "wikipulse_model_stored_scores", "Spike scores stored.", labels=DRIFT_LABELS
    )
    mean = GaugeMetricFamily(
        "wikipulse_model_stored_score_mean", "Mean stored score.", labels=DRIFT_LABELS
    )
    news = GaugeMetricFamily(
        "wikipulse_model_stored_news_share",
        "Share of stored scores classified as news.",
        labels=DRIFT_LABELS,
    )
    for row in rows:
        labels = [row["model_name"], row["model_version"], row["run_id"]]
        stored.add_metric(labels, row["scores"])
        mean.add_metric(labels, float(row["mean_score"]))
        news.add_metric(labels, float(row["news_share"]))
    yield from (stored, mean, news)


def register_model_health(dsn: str) -> ModelHealthCollector:
    """Add the collector to the default registry (once per process: the API's start)."""
    collector = ModelHealthCollector(lambda: read_model_health(dsn))
    REGISTRY.register(collector)
    return collector
