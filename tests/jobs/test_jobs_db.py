"""The daily jobs against the real Postgres (`-m integration`).

Every test writes `hourly_stats` rows under its own random dataset and its own detector
run id, and deletes all its rows afterwards; nothing else in the database is touched.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from wikipulse.config import get_settings
from wikipulse.db.daily import (
    delete_daily_rows,
    get_daily_stats,
    get_detection_eval,
    upsert_detection_eval,
)
from wikipulse.db.hourly_stats import delete_dataset, upsert_hourly_stats
from wikipulse.db.migrate import migrate
from wikipulse.jobs import daily_stats
from wikipulse.jobs.stats import QualityRules, day_hours
from wikipulse.workers.hourly_stats import HourStats

pytestmark = pytest.mark.integration

DAY = date(2026, 11, 6)
RULES = QualityRules()


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as connection:
        migrate(connection)
        yield connection


@pytest.fixture
def dataset(conn: psycopg.Connection) -> Iterator[str]:
    name = f"test-jobs-{uuid.uuid4().hex[:8]}/edits"
    yield name
    delete_dataset(conn, name)
    delete_daily_rows(conn, name, run_id=name)


def write_day(
    conn: psycopg.Connection, dataset: str, day: date, edits: int, skip: tuple[int, ...] = ()
) -> None:
    for n, hour in enumerate(day_hours(day)):
        if n in skip:
            continue
        stats = HourStats(
            dataset, hour, edits, edits // 2, edits // 3, edits // 4, 0.25, (), 1, 1000,
            datetime(2026, 11, 8, tzinfo=UTC),
        )  # fmt: skip
        upsert_hourly_stats(conn, stats)


def test_aggregate_stores_one_row_and_rerunning_replaces_it(conn, dataset) -> None:
    write_day(conn, dataset, DAY - timedelta(days=1), 100)  # complete: the baseline
    write_day(conn, dataset, DAY - timedelta(days=2), 80, skip=(5,))  # incomplete: ignored
    write_day(conn, dataset, DAY, 120)
    first = daily_stats.run_aggregate(conn, dataset, DAY, RULES)
    again = daily_stats.run_aggregate(conn, dataset, DAY, RULES)
    assert first == again
    row = get_daily_stats(conn, dataset, DAY)
    assert row["edits"] == 24 * 120
    assert row["bot_edits"] == 24 * 30
    assert row["hours_present"] == 24
    assert row["missing_hours"] == []
    assert row["baseline_edits"] == 24 * 100
    assert row["baseline_days"] == 1
    assert row["peak_hour"] == datetime(2026, 11, 6, tzinfo=UTC)
    assert row["passed"] is True
    count = conn.execute("SELECT count(*) FROM daily_stats WHERE dataset = %s", (dataset,))
    assert count.fetchone()[0] == 1
    assert daily_stats.run_check(conn, dataset, DAY) == []


def test_a_later_rerun_sees_hours_that_arrived_late(conn, dataset) -> None:
    write_day(conn, dataset, DAY, 100, skip=(22, 23))
    stats = daily_stats.run_aggregate(conn, dataset, DAY, RULES)
    assert stats.missing_hours == [22, 23]
    assert daily_stats.run_check(conn, dataset, DAY) == [
        "missing_hours: 2 of 24 hours have no statistics: 22:00, 23:00 UTC"
    ]
    write_day(conn, dataset, DAY, 100)  # the late hours arrive; re-run the day
    daily_stats.run_aggregate(conn, dataset, DAY, RULES)
    assert daily_stats.run_check(conn, dataset, DAY) == []


def test_check_without_a_stored_row_raises(conn, dataset) -> None:
    with pytest.raises(LookupError, match="run `aggregate` first"):
        daily_stats.run_check(conn, dataset, DAY)


def test_the_command_exits_1_when_the_check_fails(conn, dataset) -> None:
    write_day(conn, dataset, DAY, 100, skip=(3,))
    day = DAY.isoformat()
    assert daily_stats.main(["aggregate", "--date", day, "--dataset", dataset]) == 0
    assert daily_stats.main(["check", "--date", day, "--dataset", dataset]) == 1
    other_day = (DAY + timedelta(days=1)).isoformat()
    assert daily_stats.main(["check", "--date", other_day, "--dataset", dataset]) == 2


def test_detection_eval_upsert_replaces_the_days_row(conn, dataset) -> None:
    run_id = dataset  # unique, and removed by the fixture
    report = {
        "metrics": {"spikes": 3},
        "portal_days": [
            {"day": "2026-11-06", "missing": False},
            {"day": "2026-11-07", "missing": True},
        ],
    }
    upsert_detection_eval(conn, run_id, DAY, report)
    report["metrics"] = {"spikes": 4}
    report["portal_days"][1]["missing"] = False  # the page exists by the second run
    upsert_detection_eval(conn, run_id, DAY, report)
    row = get_detection_eval(conn, run_id, DAY)
    assert row["metrics"] == {"spikes": 4}
    assert row["portal_missing"] == []
    count = conn.execute("SELECT count(*) FROM detection_eval_daily WHERE run_id = %s", (run_id,))
    assert count.fetchone()[0] == 1
