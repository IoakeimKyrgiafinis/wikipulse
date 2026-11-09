"""Where an edit lives in the lake: hourly, Hive-style partitions of event time.

An edit whose `event_time` is 2026-11-07 13:42:05 UTC belongs to the hour 13:00 and is
stored under

    edits/year=2026/month=11/day=07/hour=13/part-0.parquet

The `name=value` folder names are the *Hive* convention. Query engines (DuckDB, Spark,
Trino, pyarrow) read them as columns, so `WHERE day = 7 AND hour = 13` opens only the
files of that folder instead of the whole archive ("partition pruning").

The hour comes from the event's own timestamp, never from the clock, so a late event
still lands in the hour it happened in, and a replay builds exactly the same lake.
"""

import re
from datetime import UTC, datetime

# Every hour is written as one object, replaced as a whole (see LakeStore.write_hour).
# The part number stays in the name so that a future writer could split very large
# hours into part-0, part-1, ... without changing the layout readers expect.
PART_NAME = "part-{n}.parquet"

_PARTITION_RE = re.compile(r"year=(\d{4})/month=(\d{2})/day=(\d{2})/hour=(\d{2})(?:/|$)")


def hour_of(moment: datetime) -> datetime:
    """The start of the UTC hour `moment` falls in (an aware datetime)."""
    if moment.tzinfo is None:
        raise ValueError("event times must be timezone-aware")
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def partition_path(prefix: str, hour: datetime) -> str:
    """'edits/year=2026/month=11/day=07/hour=13' for the hour starting at 13:00 UTC."""
    hour = hour_of(hour)
    folder = f"year={hour:%Y}/month={hour:%m}/day={hour:%d}/hour={hour:%H}"
    return f"{prefix.strip('/')}/{folder}" if prefix.strip("/") else folder


def part_path(prefix: str, hour: datetime, n: int = 0) -> str:
    """The key of part `n` of an hour: '<partition_path>/part-<n>.parquet'."""
    return f"{partition_path(prefix, hour)}/{PART_NAME.format(n=n)}"


def parse_partition(path: str) -> datetime | None:
    """The hour a key or folder belongs to, or None if it holds no hour partition."""
    match = _PARTITION_RE.search(path)
    if match is None:
        return None
    year, month, day, hour = (int(part) for part in match.groups())
    return datetime(year, month, day, hour, tzinfo=UTC)
