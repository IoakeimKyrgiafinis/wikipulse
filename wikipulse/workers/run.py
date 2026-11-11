"""Event-driven worker: `uv run python -m wikipulse.workers.run [command]`.

Computes per-hour statistics of the data lake (table `hourly_stats`) whenever an hourly
Parquet file is written. Set up the notifications once with
`uv run python -m wikipulse.workers.setup` and apply migrations with
`uv run python -m wikipulse.db.migrate`.

Commands:
  listen        (default) catch up on hours without (current) statistics, then handle
                every "object created" notification until Ctrl+C
  backfill      only the catch-up: handle every hour that is missing or out of date
                (`--force`: every hour), then exit
  dead-letters  show the notifications whose handler failed every attempt
  redrive       send the dead-lettered notifications through the handler again
"""

import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

import redis

from wikipulse.config import Settings, get_settings
from wikipulse.db.hourly_stats import PostgresStats, stored_sources
from wikipulse.detect.run import StopFlag
from wikipulse.lake.store import s3_filesystem
from wikipulse.logging_setup import setup_logging
from wikipulse.workers.backfill import BackfillReport, backfill
from wikipulse.workers.hourly_stats import HandlerContext, handle
from wikipulse.workers.invoker import Invoker, dead_letters, redrive
from wikipulse.workers.listener import ListenStats, NotificationListener
from wikipulse.workers.notifications import ObjectRef

logger = logging.getLogger(__name__)

COMMANDS = ("listen", "backfill", "dead-letters", "redrive")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", nargs="?", default="listen", choices=COMMANDS)
    parser.add_argument("--bucket", help="bucket whose files are handled (default: S3_BUCKET)")
    parser.add_argument(
        "--prefix", default="", help="backfill only keys under this prefix (default: all)"
    )
    parser.add_argument("--force", action="store_true", help="backfill: recompute every hour")
    parser.add_argument(
        "--no-catch-up", action="store_true", help="listen: skip the backfill at start"
    )
    parser.add_argument("--channel", help="Redis channel (default: LAKE_NOTIFY_CHANNEL)")
    parser.add_argument(
        "--dead-letter-key", help="Redis list of failed events (default: WORKER_DEAD_LETTER_KEY)"
    )
    parser.add_argument(
        "--max-events", type=int, help="listen: stop after this many events (for testing)"
    )
    parser.add_argument(
        "--exit-when-idle",
        type=float,
        metavar="SECONDS",
        help="listen: stop after this many seconds without notifications",
    )
    return parser.parse_args(argv)


@dataclass
class Worker:
    """The pieces of one worker process, built once and reused by every invocation."""

    settings: Settings
    bucket: str
    client: redis.Redis
    stats: PostgresStats
    context: HandlerContext
    invoker: Invoker

    def backfill(self, prefix: str = "", force: bool = False) -> BackfillReport:
        return backfill(
            self.context.fs,
            lambda dataset: stored_sources(self.stats.conn, dataset),
            self.invoker,
            self.bucket,
            prefix,
            force,
        )

    def close(self) -> None:
        self.stats.close()
        self.client.close()


def build_worker(
    settings: Settings,
    bucket: str | None = None,
    dead_letter_key: str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Worker:
    """Clients first (like a Lambda's code outside its handler), then the invoker."""
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    stats = PostgresStats.from_dsn(settings.postgres_dsn)
    context = HandlerContext(
        fs=s3_filesystem(settings), stats=stats, top_n=settings.worker_top_pages
    )

    def handler(event: ObjectRef) -> None:
        handle(event, context)

    extra = {"sleep": sleep} if sleep is not None else {}
    invoker = Invoker(
        handler,
        client,
        dead_letter_key or settings.worker_dead_letter_key,
        max_attempts=settings.worker_max_attempts,
        retry_delay_s=settings.worker_retry_delay_s,
        **extra,
    )
    return Worker(settings, bucket or settings.s3_bucket, client, stats, context, invoker)


def listen(
    worker: Worker,
    args: argparse.Namespace,
    should_stop: Callable[[], bool] = lambda: False,
) -> ListenStats:
    """Listen for notifications and run the worker until stopped."""
    catch_up = None if args.no_catch_up else (lambda: worker.backfill(args.prefix))
    listener = NotificationListener(
        worker.client,
        args.channel or worker.settings.lake_notify_channel,
        worker.invoker,
        worker.bucket,
        catch_up=catch_up,
    )
    stats = listener.run(should_stop, args.max_events, args.exit_when_idle)
    logger.info(
        "Stopped: %d notifications, %d events handled (%d ok, %d retries, %d dead-lettered)",
        stats.messages,
        stats.events,
        worker.invoker.stats.succeeded,
        worker.invoker.stats.retries,
        worker.invoker.stats.dead_lettered,
    )
    return stats


def show_dead_letters(worker: Worker) -> int:
    """Print the dead-lettered events; returns how many there are."""
    letters = dead_letters(worker.client, worker.invoker.dead_letter_key)
    print(f"{len(letters)} dead-lettered event(s) in {worker.invoker.dead_letter_key}")
    for letter in letters:
        event = letter.event
        print(f"- {letter.failed_at}  {event.bucket}/{event.key}  ({event.event_name})")
        print(f"    {letter.attempts} attempts, last error: {letter.error}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    worker = build_worker(settings, args.bucket, args.dead_letter_key)
    try:
        if args.command == "listen":
            stop = StopFlag()
            stop.install()
            listen(worker, args, stop)
        elif args.command == "backfill":
            report = worker.backfill(args.prefix, args.force)
            print(
                f"{report.hours} hours in the lake: {report.missing} missing, {report.stale} "
                f"stale; {report.succeeded} handled, {report.failed} dead-lettered"
            )
            return 1 if report.failed else 0
        elif args.command == "dead-letters":
            return show_dead_letters(worker)
        elif args.command == "redrive":
            succeeded, failed = redrive(worker.invoker)
            print(f"Redrive: {succeeded} succeeded, {failed} failed again (back in the list)")
            return 1 if failed else 0
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
