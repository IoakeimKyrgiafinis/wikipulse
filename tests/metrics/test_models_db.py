"""The model health queries against the real Postgres (Docker Compose)."""

import pytest

from wikipulse.config import get_settings
from wikipulse.metrics.models import read_model_health

pytestmark = pytest.mark.integration


def test_the_queries_run_on_the_real_schema() -> None:
    health = read_model_health(get_settings().postgres_dsn)
    # One row per model and run in drift_reports, one per model version and run in scores.
    keys = [(r["model_name"], r["run_id"]) for r in health.drift_reports]
    assert len(keys) == len(set(keys))
    for row in health.scores:
        assert row["scores"] > 0
        assert 0 <= row["news_share"] <= 1
