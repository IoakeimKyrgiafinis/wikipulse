"""Daily statistics of the data lake, with data-quality checks.

    uv run python -m wikipulse.jobs.daily_stats aggregate --date 2026-11-06
    uv run python -m wikipulse.jobs.daily_stats check --date 2026-11-06

`aggregate` sums one UTC day of `hourly_stats` (written per hour by the 06b worker) into
one `daily_stats` row, together with the result of the quality checks:

- **missing hours**: hours of the day without a row (the archiver or the worker was down,
  or the stream lost edits);
- **hour drop**: an hour with fewer edits than `JOBS_HOUR_DROP_RATIO` times the day's
  median hour (a sudden volume drop inside the day);
- **day drop**: a day with fewer edits than `JOBS_DAY_DROP_RATIO` times the mean of the
  earlier complete days (a drop compared with normal days).

`aggregate` refuses a day that isn't over yet, plus `LATE_DATA_WAIT` for the last
hours' files, so a partial day is never stored as if it were complete.

`check` reads the stored row and exits with code 1 when it has issues, so a scheduler
shows the failure. Both steps only read and replace rows keyed by (dataset, day), so they
can be re-run for any past day.
"""

import argparse
import logging
import sys
from datetime import UTC, date, datetime, timedelta

import psycopg

from wikipulse.config import Settings, get_settings
from wikipulse.db.daily import baseline_totals, get_daily_stats, hour_counts, upsert_daily_stats
from wikipulse.jobs.stats import DailyStats, QualityRules, aggregate_day, day_start
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)

# How long after a day ends its last hours are complete: the archiver writes a file every
# few minutes and the 06b worker counts it soon after. The DAG's `wait_for_late_data`
# sensor waits the same hour.
LATE_DATA_WAIT = timedelta(hours=1)


class DayNotCompleteError(ValueError):
    """The day hasn't ended yet, or its late data may still be arriving."""


def check_day_is_complete(day: date, now: datetime, wait: timedelta = LATE_DATA_WAIT) -> None:
    """Only a finished day (plus `wait`) can be aggregated: its hours are all in."""
    ready = day_start(day) + timedelta(days=1) + wait
    if now < ready:
        raise DayNotCompleteError(
            f"{day} is not complete yet: its data is in from {ready.isoformat()} (now {now})"
        )


# --- The two steps -------------------------------------------------------------------


def default_dataset(settings: Settings) -> str:
    """The live archive, as the 06b worker names it: '<bucket>/<prefix>'."""
    return f"{settings.s3_bucket}/{settings.lake_prefix.strip('/')}"


def run_aggregate(
    conn: psycopg.Connection, dataset: str, day: date, rules: QualityRules
) -> DailyStats:
    """Compute one day's statistics from `hourly_stats` and store them (replacing any)."""
    hours = hour_counts(conn, dataset, day)
    baseline = baseline_totals(conn, dataset, day, rules.baseline_days)
    stats = aggregate_day(dataset, day, hours, baseline, rules)
    upsert_daily_stats(conn, stats)
    return stats


def run_check(conn: psycopg.Connection, dataset: str, day: date) -> list[str]:
    """The stored row's issues as messages; raises LookupError if there is no row."""
    row = get_daily_stats(conn, dataset, day)
    if row is None:
        raise LookupError(f"No daily_stats row for {dataset} on {day}: run `aggregate` first")
    return [f"{issue['check']}: {issue['message']}" for issue in row["issues"]]


def summary(stats: DailyStats) -> str:
    return (
        f"{stats.dataset} {stats.day}: {stats.edits} edits in {stats.hours_present} hours, "
        f"bots {stats.bot_share:.1%}, median hour {stats.median_hour_edits or 0:.0f}, "
        f"{len(stats.issues)} quality issue(s)"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=["aggregate", "check"])
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="UTC day")
    parser.add_argument(
        "--dataset", help="'<bucket>/<prefix>' as in hourly_stats (default: the live archive)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    dataset = args.dataset or default_dataset(settings)
    if args.step == "aggregate":
        try:
            # The clock only decides whether the day is complete; never the results.
            check_day_is_complete(args.date, datetime.now(UTC))
        except DayNotCompleteError as error:
            logger.error("%s", error)
            return 1
    with psycopg.connect(settings.postgres_dsn, autocommit=True, connect_timeout=10) as conn:
        if args.step == "aggregate":
            stats = run_aggregate(conn, dataset, args.date, QualityRules.from_settings(settings))
            logger.info("Stored %s", summary(stats))
            for issue in stats.issues:
                logger.warning("Quality issue (the check step will fail): %s", issue.message)
            return 0
        try:
            problems = run_check(conn, dataset, args.date)
        except LookupError as error:
            logger.error("%s", error)
            return 2
    if problems:
        logger.error("Data quality check FAILED for %s on %s:", dataset, args.date)
        for problem in problems:
            logger.error("  %s", problem)
        return 1
    logger.info("Data quality check passed for %s on %s", dataset, args.date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
