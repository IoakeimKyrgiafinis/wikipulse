"""Daily statistics and data-quality checks, as pure functions (no database, no clock).

`aggregate_day()` turns one UTC day of `hourly_stats` rows into a `DailyStats` with its
quality issues; `wikipulse.jobs.daily_stats` loads the rows and stores the result.
"""

import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from wikipulse.config import Settings

HOURS_PER_DAY = 24


@dataclass(frozen=True)
class HourCounts:
    """The two numbers of one `hourly_stats` row that the daily statistics need."""

    hour: datetime
    edits: int
    bot_edits: int


@dataclass(frozen=True)
class QualityRules:
    """Thresholds of the quality checks (from the `JOBS_*` settings)."""

    hour_drop_ratio: float = 0.3
    day_drop_ratio: float = 0.5
    baseline_days: int = 7

    @classmethod
    def from_settings(cls, settings: Settings) -> "QualityRules":
        return cls(
            settings.jobs_hour_drop_ratio, settings.jobs_day_drop_ratio, settings.jobs_baseline_days
        )


@dataclass(frozen=True)
class Issue:
    """One failed quality check: which check, and a sentence a human can act on."""

    check: str
    message: str


@dataclass
class DailyStats:
    """One `daily_stats` row."""

    dataset: str
    day: date
    hours_present: int
    missing_hours: list[int]
    edits: int
    bot_edits: int
    bot_share: float
    min_hour_edits: int | None
    median_hour_edits: float | None
    max_hour_edits: int | None
    peak_hour: datetime | None
    baseline_edits: float | None
    baseline_days: int
    issues: list[Issue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues


def day_start(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def day_hours(day: date) -> list[datetime]:
    """The 24 hours of a UTC day."""
    start = day_start(day)
    return [start + timedelta(hours=n) for n in range(HOURS_PER_DAY)]


def find_issues(stats: DailyStats, rules: QualityRules, hours: list[HourCounts]) -> list[Issue]:
    """The quality checks of one day (pure: everything comes in as arguments)."""
    issues = []
    if stats.missing_hours:
        listed = ", ".join(f"{h:02d}:00" for h in stats.missing_hours)
        issues.append(
            Issue(
                "missing_hours",
                f"{len(stats.missing_hours)} of {HOURS_PER_DAY} hours have no statistics: "
                f"{listed} UTC",
            )
        )
    if stats.median_hour_edits:
        limit = rules.hour_drop_ratio * stats.median_hour_edits
        for counts in hours:
            if counts.edits < limit:
                issues.append(
                    Issue(
                        "hour_drop",
                        f"{counts.hour:%H}:00 UTC has {counts.edits} edits, fewer than "
                        f"{rules.hour_drop_ratio:.0%} of the day's median hour "
                        f"({stats.median_hour_edits:.0f})",
                    )
                )
    if stats.baseline_edits and stats.edits < rules.day_drop_ratio * stats.baseline_edits:
        issues.append(
            Issue(
                "day_drop",
                f"{stats.edits} edits, fewer than {rules.day_drop_ratio:.0%} of the mean of "
                f"the {stats.baseline_days} earlier complete day(s) ({stats.baseline_edits:.0f})",
            )
        )
    return issues


def aggregate_day(
    dataset: str,
    day: date,
    hours: list[HourCounts],
    baseline_totals: list[int],
    rules: QualityRules,
) -> DailyStats:
    """Sum one day's hours and run the quality checks (pure).

    `hours` are the day's `hourly_stats` rows; `baseline_totals` the edit totals of
    earlier complete days, for the day-drop check (may be empty).
    """
    present = {counts.hour.astimezone(UTC).hour for counts in hours}
    edits = sum(counts.edits for counts in hours)
    bot_edits = sum(counts.bot_edits for counts in hours)
    per_hour = sorted(counts.edits for counts in hours)
    peak = max(hours, key=lambda counts: (counts.edits, -counts.hour.timestamp()), default=None)
    stats = DailyStats(
        dataset=dataset,
        day=day,
        hours_present=len(present),
        missing_hours=[h for h in range(HOURS_PER_DAY) if h not in present],
        edits=edits,
        bot_edits=bot_edits,
        bot_share=bot_edits / edits if edits else 0.0,
        min_hour_edits=per_hour[0] if per_hour else None,
        median_hour_edits=statistics.median(per_hour) if per_hour else None,
        max_hour_edits=per_hour[-1] if per_hour else None,
        peak_hour=peak.hour if peak else None,
        baseline_edits=statistics.fmean(baseline_totals) if baseline_totals else None,
        baseline_days=len(baseline_totals),
    )
    stats.issues = find_issues(stats, rules, hours)
    return stats
