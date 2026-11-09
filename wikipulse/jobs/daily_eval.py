"""The daily detection evaluation: one day's spikes against that day's news.

    uv run python -m wikipulse.jobs.daily_eval fetch --date 2026-11-06
    uv run python -m wikipulse.jobs.daily_eval evaluate --date 2026-11-06 [--run-id live]

`fetch` downloads the "Current events" portal pages the evaluation of a day needs (the
day itself and `PORTAL_MARGIN_DAYS` on either side, as far as they exist yet) into the
cache in `data/ground_truth/`, politely (see `WikiClient`). It fails when the day's own
page is missing, so a scheduler retries later.

`evaluate` runs the spec 02b evaluation (`wikipulse.eval.detection.build_report`) for that
one day, stores the report in Postgres (`detection_eval_daily`, keyed by run and day) and
writes it as Markdown to `data/daily_eval/<run>/<day>.md`. Re-running a day replaces both;
a later run sees a more complete portal (pages are edited for days), so a backfill is
also how a provisional result is refreshed.
"""

import argparse
import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from wikipulse.config import Settings, get_settings
from wikipulse.db.daily import upsert_detection_eval
from wikipulse.db.spikes import spikes_for_run
from wikipulse.detect.detector import SpikeRecord
from wikipulse.eval.detection import (
    PORTAL_MARGIN_DAYS,
    SPIKE_MARGIN_DAYS,
    build_report,
    config_snapshot,
    git_commit,
    load_portal,
    render_markdown,
    summary_line,
)
from wikipulse.eval.ground_truth import PortalDay
from wikipulse.eval.label_store import DEFAULT_LABELS_PATH, load_labels
from wikipulse.eval.metrics import Label, day_start
from wikipulse.eval.wiki_client import WikiClient
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)


class DayNotOverError(ValueError):
    """The day to evaluate hasn't ended yet (UTC)."""


class GroundTruthMissingError(RuntimeError):
    """The portal page of the evaluated day doesn't exist (yet)."""


def check_day_is_over(day: date, today: date) -> None:
    """Only finished days can be evaluated: their spikes and news are complete."""
    if day >= today:
        raise DayNotOverError(f"{day} is not over yet (today is {today}, UTC)")


def portal_range(day: date) -> tuple[date, date]:
    """The portal days an evaluation of `day` looks at."""
    margin = timedelta(days=PORTAL_MARGIN_DAYS)
    return day - margin, day + margin


def wiki_client(settings: Settings) -> WikiClient:
    cache_dir = settings.data_dir / "ground_truth"
    return WikiClient(settings.user_agent, cache_dir, settings.wiki_api_min_interval_s)


def fetch_ground_truth(day: date, client: WikiClient, api_url: str, today: date) -> list[PortalDay]:
    """Fetch (or reuse from the cache) the portal pages for `day`'s evaluation."""
    check_day_is_over(day, today)
    first, last = portal_range(day)
    portal = load_portal(client, api_url, first, last, today)
    own = next(p for p in portal if p.day == day)
    if own.missing:
        raise GroundTruthMissingError(f"The portal page for {day} doesn't exist (yet)")
    return portal


def evaluate_day(
    day: date,
    run_id: str,
    spikes: list[SpikeRecord],
    portal: list[PortalDay],
    labels: list[Label],
    config: dict[str, Any],
    git: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """The 02b report for one day (pure: everything comes in as arguments)."""
    return build_report(
        f"daily-{run_id}",
        run_id,
        day,
        day,
        spikes,
        portal,
        labels,
        config,
        git,
        now,
        note=f"Daily evaluation of {day} (wikipulse.jobs.daily_eval)",
    )


def report_path(settings: Settings, run_id: str, day: date) -> Path:
    return settings.data_dir / "daily_eval" / run_id / f"{day.isoformat()}.md"


def run_fetch(settings: Settings, day: date, now: datetime) -> list[PortalDay]:
    with wiki_client(settings) as client:
        portal = fetch_ground_truth(day, client, settings.wiki_api_url, now.date())
        logger.info(
            "Portal pages %s to %s: %d requests, %d from cache",
            portal[0].day,
            portal[-1].day,
            client.requests,
            client.cache_hits,
        )
    for page in portal:
        state = "missing" if page.missing else f"{len(page.links)} links, {len(page.leads)} leads"
        logger.info("  %s: %s", page.day, state)
    return portal


def run_evaluate(settings: Settings, day: date, run_id: str, now: datetime) -> dict[str, Any]:
    """Evaluate one day of one detector run; store and write the report."""
    check_day_is_over(day, now.date())
    # The portal pages come from the cache `fetch` filled (fetched again only if stale).
    portal = run_fetch(settings, day, now)
    labels = load_labels(DEFAULT_LABELS_PATH)
    config = config_snapshot(settings, {})
    margin = timedelta(days=SPIKE_MARGIN_DAYS)
    with psycopg.connect(settings.postgres_dsn, autocommit=True, connect_timeout=10) as conn:
        spikes = spikes_for_run(
            conn, run_id, day_start(day) - margin, day_start(day) + timedelta(days=1) + margin
        )
        report = evaluate_day(day, run_id, spikes, portal, labels, config, git_commit(), now)
        upsert_detection_eval(conn, run_id, day, report)
    path = report_path(settings, run_id, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    path.with_suffix(".json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    logger.info("Run %s, %s: %s", run_id, day, summary_line(report))
    logger.info("Stored in detection_eval_daily; report: %s", path)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=["fetch", "evaluate"])
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="UTC day")
    parser.add_argument("--run-id", help="detector run to evaluate (default: JOBS_EVAL_RUN_ID)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    now = datetime.now(UTC)  # only decides which days are over; never the results
    try:
        if args.step == "fetch":
            run_fetch(settings, args.date, now)
        else:
            run_evaluate(settings, args.date, args.run_id or settings.jobs_eval_run_id, now)
    except (DayNotOverError, GroundTruthMissingError) as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
