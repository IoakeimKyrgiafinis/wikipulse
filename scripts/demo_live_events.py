"""Replay a recorded day's grouping slowly, so the live page has something to show.

    uv run python scripts/demo_live_events.py --spike-run 02b-loose --run-id 05e-live-demo \
        --since 2026-11-05T16:00:00+00:00 --until 2026-11-05T20:00:00+00:00 \
        --step-min 15 --pause-s 10 --reset

Groups the spikes of `--spike-run` into the grouping run `--run-id`, `--step-min` minutes
of event time at a time, pausing `--pause-s` seconds of wall-clock time between steps:
events appear, grow and end in Postgres the way they would during a live run, only
faster. Start the API on the same run (`uv run python -m wikipulse.api --run-id
05e-live-demo`) and watch the page. `--reset` deletes the run's events first; `--delete`
deletes them and exits.

Writes only the grouping run named by `--run-id` (tables `events` and `event_pages`).
"""

import argparse
import logging
import sys
import time
from datetime import timedelta

import psycopg

from wikipulse.config import get_settings
from wikipulse.db.events import delete_grouping_run
from wikipulse.enrich.run import parse_day_or_time
from wikipulse.events.embed import Embedder
from wikipulse.events.run import GroupingRunner
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger("demo_live_events")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spike-run", default="02b-loose")
    parser.add_argument("--run-id", default="05e-live-demo")
    parser.add_argument("--since", type=parse_day_or_time, default="2026-11-05T16:00:00+00:00")
    parser.add_argument("--until", type=parse_day_or_time, default="2026-11-05T20:00:00+00:00")
    parser.add_argument("--step-min", type=float, default=15)
    parser.add_argument("--pause-s", type=float, default=10)
    parser.add_argument("--reset", action="store_true", help="delete the run's events first")
    parser.add_argument("--delete", action="store_true", help="delete the run's events, exit")
    args = parser.parse_args(argv)
    setup_logging()
    settings = get_settings()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        if args.reset or args.delete:
            deleted = delete_grouping_run(conn, args.run_id)
            logger.info("Deleted %d events of run %s", deleted, args.run_id)
            if args.delete:
                return 0
        embedder = Embedder(settings.events_embed_model)
        runner = GroupingRunner(
            conn, embedder, settings.events_config(), args.run_id, args.spike_run, args.since
        )
        until = args.since
        while until < args.until:
            until = min(until + timedelta(minutes=args.step_min), args.until)
            runner.until = until
            grouped = runner.step()
            logger.info("Event time %s: grouped %d spike(s)", until.isoformat(), grouped)
            if until < args.until:
                time.sleep(args.pause_s)
        logger.info(runner.stats.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
