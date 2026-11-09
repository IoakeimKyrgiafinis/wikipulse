"""The daily batch tables (migration 0007): `daily_stats` and `detection_eval_daily`.

Every write is an upsert of a whole row keyed by the day, so the jobs in
`wikipulse/jobs/` can be re-run for any day without creating duplicates.
"""

import json
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from wikipulse.jobs.stats import HOURS_PER_DAY, DailyStats, HourCounts, day_start

UPSERT_DAILY_STATS = """
INSERT INTO daily_stats (dataset, day, hours_present, missing_hours, edits, bot_edits,
                         bot_share, min_hour_edits, median_hour_edits, max_hour_edits,
                         peak_hour, baseline_edits, baseline_days, issues, passed, computed_at)
VALUES (%(dataset)s, %(day)s, %(hours_present)s, %(missing_hours)s, %(edits)s, %(bot_edits)s,
        %(bot_share)s, %(min_hour_edits)s, %(median_hour_edits)s, %(max_hour_edits)s,
        %(peak_hour)s, %(baseline_edits)s, %(baseline_days)s, %(issues)s, %(passed)s, now())
ON CONFLICT (dataset, day) DO UPDATE SET
    hours_present = EXCLUDED.hours_present, missing_hours = EXCLUDED.missing_hours,
    edits = EXCLUDED.edits, bot_edits = EXCLUDED.bot_edits, bot_share = EXCLUDED.bot_share,
    min_hour_edits = EXCLUDED.min_hour_edits, median_hour_edits = EXCLUDED.median_hour_edits,
    max_hour_edits = EXCLUDED.max_hour_edits, peak_hour = EXCLUDED.peak_hour,
    baseline_edits = EXCLUDED.baseline_edits, baseline_days = EXCLUDED.baseline_days,
    issues = EXCLUDED.issues, passed = EXCLUDED.passed, computed_at = now()
"""

UPSERT_DETECTION_EVAL = """
INSERT INTO detection_eval_daily (run_id, day, metrics, report, portal_missing, evaluated_at)
VALUES (%(run_id)s, %(day)s, %(metrics)s, %(report)s, %(portal_missing)s, now())
ON CONFLICT (run_id, day) DO UPDATE SET
    metrics = EXCLUDED.metrics, report = EXCLUDED.report,
    portal_missing = EXCLUDED.portal_missing, evaluated_at = now()
"""


def hour_counts(conn: psycopg.Connection, dataset: str, day: date) -> list[HourCounts]:
    """The `hourly_stats` rows of one UTC day, in hour order."""
    start = day_start(day)
    rows = conn.execute(
        "SELECT hour, edits, bot_edits FROM hourly_stats"
        " WHERE dataset = %s AND hour >= %s AND hour < %s ORDER BY hour",
        (dataset, start, start + timedelta(days=1)),
    ).fetchall()
    return [HourCounts(hour, edits, bot_edits) for hour, edits, bot_edits in rows]


def baseline_totals(conn: psycopg.Connection, dataset: str, day: date, days: int) -> list[int]:
    """Edit totals of the complete days (all 24 hours present) in the `days` before `day`."""
    start = day_start(day)
    rows = conn.execute(
        "SELECT (hour AT TIME ZONE 'UTC')::date AS d, sum(edits) FROM hourly_stats"
        " WHERE dataset = %s AND hour >= %s AND hour < %s"
        " GROUP BY d HAVING count(*) = %s ORDER BY d",
        (dataset, start - timedelta(days=days), start, HOURS_PER_DAY),
    ).fetchall()
    return [int(total) for _, total in rows]


def upsert_daily_stats(conn: psycopg.Connection, stats: DailyStats) -> None:
    params = asdict(stats)
    params["issues"] = json.dumps([asdict(issue) for issue in stats.issues], ensure_ascii=False)
    params["passed"] = stats.passed
    conn.execute(UPSERT_DAILY_STATS, params)


def get_daily_stats(conn: psycopg.Connection, dataset: str, day: date) -> dict[str, Any] | None:
    """The stored row of (dataset, day) as a dict, or None."""
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT * FROM daily_stats WHERE dataset = %s AND day = %s", (dataset, day))
        return cursor.fetchone()


def upsert_detection_eval(
    conn: psycopg.Connection, run_id: str, day: date, report: dict[str, Any]
) -> None:
    """Store one day's evaluation report (replacing an earlier one of the same day)."""
    missing = [date.fromisoformat(p["day"]) for p in report["portal_days"] if p["missing"]]
    conn.execute(
        UPSERT_DETECTION_EVAL,
        {
            "run_id": run_id,
            "day": day,
            "metrics": json.dumps(report["metrics"], ensure_ascii=False),
            "report": json.dumps(report, ensure_ascii=False),
            "portal_missing": missing,
        },
    )


def get_detection_eval(conn: psycopg.Connection, run_id: str, day: date) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT * FROM detection_eval_daily WHERE run_id = %s AND day = %s", (run_id, day)
        )
        return cursor.fetchone()


def delete_daily_rows(conn: psycopg.Connection, dataset: str, run_id: str) -> None:
    """Remove one dataset's daily_stats and one run's evaluations (for tests)."""
    conn.execute("DELETE FROM daily_stats WHERE dataset = %s", (dataset,))
    conn.execute("DELETE FROM detection_eval_daily WHERE run_id = %s", (run_id,))
