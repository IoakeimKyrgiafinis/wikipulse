"""The daily statistics and quality checks (pure functions, no database)."""

from datetime import UTC, date, datetime, timedelta

from wikipulse.jobs.stats import HourCounts, QualityRules, aggregate_day, day_hours

DAY = date(2026, 11, 6)
RULES = QualityRules(hour_drop_ratio=0.3, day_drop_ratio=0.5, baseline_days=7)


def hours(edits: list[int], bots: int = 10, skip: tuple[int, ...] = ()) -> list[HourCounts]:
    """One row per hour of DAY (hour n gets edits[n]), leaving out the hours in `skip`."""
    return [
        HourCounts(hour, edits[n], bots) for n, hour in enumerate(day_hours(DAY)) if n not in skip
    ]


def checks(stats) -> list[str]:
    return [issue.check for issue in stats.issues]


def test_day_hours_are_the_24_utc_hours() -> None:
    result = day_hours(DAY)
    assert len(result) == 24
    assert result[0] == datetime(2026, 11, 6, tzinfo=UTC)
    assert result[-1] - result[0] == timedelta(hours=23)


def test_a_complete_normal_day_sums_up_and_passes() -> None:
    edits = [100 + n for n in range(24)]
    stats = aggregate_day("lake/edits", DAY, hours(edits), [2400, 2500], RULES)
    assert stats.hours_present == 24
    assert stats.missing_hours == []
    assert stats.edits == sum(edits)
    assert stats.bot_edits == 240
    assert stats.bot_share == 240 / sum(edits)
    assert (stats.min_hour_edits, stats.max_hour_edits) == (100, 123)
    assert stats.median_hour_edits == 111.5
    assert stats.peak_hour == datetime(2026, 11, 6, 23, tzinfo=UTC)
    assert stats.baseline_edits == 2450
    assert stats.baseline_days == 2
    assert stats.passed


def test_missing_hours_are_listed_and_fail_the_check() -> None:
    stats = aggregate_day("lake/edits", DAY, hours([100] * 24, skip=(3, 14, 15)), [], RULES)
    assert stats.hours_present == 21
    assert stats.missing_hours == [3, 14, 15]
    assert checks(stats) == ["missing_hours"]
    assert "03:00, 14:00, 15:00" in stats.issues[0].message


def test_a_day_without_any_data_fails_with_all_hours_missing() -> None:
    stats = aggregate_day("lake/edits", DAY, [], [2400], RULES)
    assert stats.edits == 0
    assert stats.bot_share == 0.0
    assert stats.median_hour_edits is None
    assert stats.peak_hour is None
    assert stats.missing_hours == list(range(24))
    # 0 edits is also far below the earlier days: two different problems, both reported.
    assert checks(stats) == ["missing_hours", "day_drop"]


def test_a_sudden_drop_within_the_day_fails() -> None:
    edits = [100] * 24
    edits[9] = 29  # below 30% of the median hour (100)
    edits[10] = 30  # exactly 30%: still fine
    stats = aggregate_day("lake/edits", DAY, hours(edits), [], RULES)
    assert checks(stats) == ["hour_drop"]
    assert stats.issues[0].message.startswith("09:00 UTC has 29 edits")


def test_a_day_far_below_the_earlier_days_fails() -> None:
    stats = aggregate_day("lake/edits", DAY, hours([40] * 24), [2000, 2000, 2000], RULES)
    assert stats.edits == 960
    assert checks(stats) == ["day_drop"]
    assert "3 earlier complete day(s) (2000)" in stats.issues[0].message


def test_without_earlier_days_there_is_no_day_drop_check() -> None:
    stats = aggregate_day("lake/edits", DAY, hours([1] * 24), [], RULES)
    assert stats.baseline_edits is None
    assert stats.passed


def test_aggregation_does_not_depend_on_row_order() -> None:
    rows = hours([100 + 7 * n % 50 for n in range(24)])
    forward = aggregate_day("lake/edits", DAY, rows, [3000], RULES)
    backward = aggregate_day("lake/edits", DAY, list(reversed(rows)), [3000], RULES)
    assert forward == backward


def test_peak_hour_ties_go_to_the_earlier_hour() -> None:
    edits = [100] * 24
    edits[5] = edits[17] = 300
    stats = aggregate_day("lake/edits", DAY, hours(edits), [], RULES)
    assert stats.peak_hour == datetime(2026, 11, 6, 5, tzinfo=UTC)


def test_rules_come_from_the_settings() -> None:
    from wikipulse.config import Settings

    settings = Settings(
        _env_file=None, jobs_hour_drop_ratio=0.2, jobs_day_drop_ratio=0.4, jobs_baseline_days=3
    )
    assert QualityRules.from_settings(settings) == QualityRules(0.2, 0.4, 3)
