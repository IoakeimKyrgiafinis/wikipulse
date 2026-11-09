"""The daily_stats command's guard against unfinished days (no database needed)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from wikipulse.jobs import daily_stats

DAY = date(2026, 11, 6)
DAY_END = datetime(2026, 11, 7, tzinfo=UTC)


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 11, 6, 14, tzinfo=UTC),  # the day is still going on
        DAY_END,  # just over, but the last hours' files may still arrive
        DAY_END + daily_stats.LATE_DATA_WAIT - timedelta(seconds=1),
    ],
)
def test_an_unfinished_day_cannot_be_aggregated(now: datetime) -> None:
    with pytest.raises(daily_stats.DayNotCompleteError):
        daily_stats.check_day_is_complete(DAY, now)


@pytest.mark.parametrize("now", [DAY_END + daily_stats.LATE_DATA_WAIT, DAY_END + timedelta(days=3)])
def test_a_finished_day_plus_the_late_data_wait_can_be_aggregated(now: datetime) -> None:
    daily_stats.check_day_is_complete(DAY, now)


def test_aggregating_today_exits_1_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_database(*args, **kwargs):
        raise AssertionError("the guard must stop before connecting")

    monkeypatch.setattr(daily_stats.psycopg, "connect", no_database)
    today = datetime.now(UTC).date().isoformat()
    assert daily_stats.main(["aggregate", "--date", today, "--dataset", "test/edits"]) == 1
