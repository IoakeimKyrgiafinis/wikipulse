"""Partition paths: the hour of an edit's event time, as Hive-style folders."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from wikipulse.lake.partitions import hour_of, parse_partition, part_path, partition_path


def test_hour_of_floors_to_the_utc_hour() -> None:
    moment = datetime(2026, 11, 7, 13, 59, 59, 999_999, tzinfo=UTC)
    assert hour_of(moment) == datetime(2026, 11, 7, 13, tzinfo=UTC)


def test_hour_of_converts_other_time_zones_to_utc() -> None:
    athens = timezone(timedelta(hours=2))
    moment = datetime(2026, 11, 7, 1, 30, tzinfo=athens)  # 23:30 UTC the day before
    assert hour_of(moment) == datetime(2026, 11, 6, 23, tzinfo=UTC)


def test_hour_of_rejects_naive_times() -> None:
    with pytest.raises(ValueError):
        hour_of(datetime(2026, 11, 7, 13, 0))


def test_partition_path_is_zero_padded_hive_style() -> None:
    hour = datetime(2026, 1, 2, 3, 45, tzinfo=UTC)
    assert partition_path("edits", hour) == "edits/year=2026/month=01/day=02/hour=03"


def test_part_path_names_the_part_file() -> None:
    hour = datetime(2026, 11, 7, 13, tzinfo=UTC)
    assert (
        part_path("bucket/edits/", hour)
        == "bucket/edits/year=2026/month=11/day=07/hour=13/part-0.parquet"
    )
    assert part_path("edits", hour, 2).endswith("hour=13/part-2.parquet")


def test_partition_path_without_prefix() -> None:
    hour = datetime(2026, 11, 7, 13, tzinfo=UTC)
    assert partition_path("", hour) == "year=2026/month=11/day=07/hour=13"


def test_parse_partition_inverts_part_path() -> None:
    hour = datetime(2026, 12, 31, 23, tzinfo=UTC)
    assert parse_partition(part_path("b/edits", hour)) == hour
    assert parse_partition(partition_path("b/edits", hour)) == hour


def test_parse_partition_ignores_other_keys() -> None:
    assert parse_partition("b/edits/year=2026/month=11") is None
    assert parse_partition("b/edits/readme.txt") is None


def test_partition_path_keeps_an_absolute_posix_prefix() -> None:
    hour = datetime(2026, 11, 7, 12, tzinfo=UTC)
    assert partition_path("/tmp/lake/", hour) == "/tmp/lake/year=2026/month=11/day=07/hour=12"
    assert partition_path("/", hour) == "/year=2026/month=11/day=07/hour=12"
