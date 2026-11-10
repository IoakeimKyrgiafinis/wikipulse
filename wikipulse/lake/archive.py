"""Data lake archiver: `uv run python -m wikipulse.lake.archive [--stream-key KEY]`.

Reads the edit stream (STREAM_KEY) with its own consumer group (LAKE_GROUP) and archives
every edit as hourly Parquet files in S3: `<S3_BUCKET>/<LAKE_PREFIX>/year=YYYY/month=MM/
day=DD/hour=HH/part-0.parquet`. Start the object store first (`docker compose up -d`).

- Live: `uv run python -m wikipulse.lake.archive` (stop with Ctrl+C; it writes what it
  has buffered before exiting, and resumes from its consumer group next time).
- A replay: `uv run python -m wikipulse.lake.archive --stream-key wiki:replay:edits
  --group lake:replay --prefix replay/edits --exit-when-idle 10`.

Only one archiver writes to a lake (bucket and prefix) at a time: a second one waits for
the first one's Redis lock (LAKE_LOCK_TTL_S) instead of overwriting its files.

`--reset` deletes the consumer group first, so the stream is read again from `--start-id`;
the lake is not touched (re-archived hours come out the same, without duplicates).
"""

import argparse
import logging
import sys

import redis

from wikipulse.config import Settings, get_settings
from wikipulse.detect.run import StopFlag
from wikipulse.health import Heartbeat, beat_while
from wikipulse.lake.archiver import DEFAULT_CONSUMER, Archiver, ArchiveStats, reset_group
from wikipulse.lake.lock import LockLostError
from wikipulse.lake.store import LakeStore
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stream-key", help="Redis Stream to read (default: STREAM_KEY)")
    parser.add_argument("--group", help="consumer group (default: LAKE_GROUP)")
    parser.add_argument(
        "--consumer",
        default=DEFAULT_CONSUMER,
        help="consumer name in the group (on start, entries left pending under other names "
        "are claimed, so renaming loses nothing)",
    )
    parser.add_argument("--prefix", help="key prefix in the bucket (default: LAKE_PREFIX)")
    parser.add_argument(
        "--start-id",
        default="0",
        help="where a new consumer group starts: '0' = whole stream (default), '$' = only new",
    )
    parser.add_argument("--reset", action="store_true", help="delete the consumer group first")
    parser.add_argument(
        "--exit-when-idle",
        type=float,
        metavar="SECONDS",
        help="stop after this many seconds without new entries (useful after a replay)",
    )
    parser.add_argument(
        "--max-entries", type=int, help="stop after reading this many entries (for testing)"
    )
    parser.add_argument(
        "--flush-interval",
        type=float,
        metavar="SECONDS",
        help="seconds between writes to the lake (default: LAKE_FLUSH_INTERVAL_S)",
    )
    parser.add_argument(
        "--max-buffer-rows",
        type=int,
        help="buffered edits that force a write (default: LAKE_MAX_BUFFER_ROWS)",
    )
    return parser.parse_args(argv)


def build_archiver(args: argparse.Namespace, settings: Settings, client: redis.Redis) -> Archiver:
    """The archiver the arguments describe (consumer group created if needed)."""
    stream = args.stream_key or settings.stream_key
    group = args.group or settings.lake_group
    if args.reset:
        reset_group(client, stream, group)
        logger.info("Deleted consumer group %s on %s", group, stream)
    archiver = Archiver(
        client,
        stream,
        group,
        LakeStore.from_settings(settings, args.prefix),
        consumer=args.consumer,
        flush_interval_s=args.flush_interval or settings.lake_flush_interval_s,
        max_buffer_rows=args.max_buffer_rows or settings.lake_max_buffer_rows,
        lock_ttl_s=settings.lake_lock_ttl_s,
    )
    archiver.ensure_group(args.start_id)
    return archiver


def run(args: argparse.Namespace, settings: Settings, should_stop=lambda: False) -> ArchiveStats:
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        archiver = build_archiver(args, settings, client)
        logger.info(
            "Archiving %s (group %s) into %s at %s",
            archiver.stream,
            archiver.group,
            archiver.store.root,
            settings.s3_endpoint,
        )
        stats = archiver.run(should_stop, args.exit_when_idle, args.max_entries)
        logger.info(
            "Stopped: %d entries read, %d skipped, %d hour files written in %d flushes",
            stats.entries,
            stats.skipped,
            stats.hour_writes,
            stats.flushes,
        )
        return stats
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    stop = StopFlag()
    stop.install()
    try:
        run(args, settings, beat_while(stop, Heartbeat.from_settings(settings)))
    except LockLostError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
