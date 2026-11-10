"""Spike detection: `uv run python -m wikipulse.detect.run [--stream-key KEY] [--run-id ID]`.

Reads edits from a Redis Stream with a consumer group, detects spikes, and stores them in
Postgres (table `spikes`, apply migrations first with `python -m wikipulse.db.migrate`).

- Live: `uv run python -m wikipulse.detect.run` reads STREAM_KEY (`wiki:edits`) as run "live".
- Replay: `uv run python -m wikipulse.stream.replay --file <rec>` writes REPLAY_STREAM_KEY;
  then `uv run python -m wikipulse.detect.run --stream-key wiki:replay:edits --run-id myrun
  --reset --exit-when-idle 10` detects spikes in it from scratch.

Stop with Ctrl+C: the detector takes a final snapshot before exiting, and resumes from it
next time. `--reset` forgets a run's state, its consumer group and its spikes first.
"""

import argparse
import logging
import signal
import sys
from typing import Any

import psycopg
import redis

from wikipulse.config import Settings, get_settings
from wikipulse.db.spikes import delete_run, upsert_spikes
from wikipulse.detect.detector import SpikeDetector, SpikeRecord
from wikipulse.detect.runner import DEFAULT_CONSUMER, DetectionRunner, DetectKeys, reset_run
from wikipulse.health import Heartbeat, beat_while
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)


class PostgresSink:
    """Writes changed spikes to the `spikes` table for one run."""

    def __init__(self, conn: psycopg.Connection, run_id: str) -> None:
        self.conn = conn
        self.run_id = run_id

    def write(self, spikes: list[SpikeRecord]) -> None:
        upsert_spikes(self.conn, self.run_id, spikes)


class StopFlag:
    """Set by Ctrl+C (SIGINT), SIGTERM or Ctrl+Break; a second Ctrl+C aborts at once."""

    def __init__(self) -> None:
        self.stopped = False

    def install(self) -> None:
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK exists only on Windows
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), self._handle)

    def _handle(self, signum: int, frame: Any) -> None:
        if self.stopped:
            raise KeyboardInterrupt
        logger.info("Received %s, stopping after the current batch", signal.Signals(signum).name)
        self.stopped = True

    def __call__(self) -> bool:
        return self.stopped


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stream-key", help="Redis Stream to read (default: STREAM_KEY)")
    parser.add_argument("--run-id", default="live", help="names the group, state and spikes")
    parser.add_argument("--consumer", default=DEFAULT_CONSUMER, help="consumer name in the group")
    parser.add_argument(
        "--start-id",
        default="0",
        help="where a new consumer group starts: '0' = whole stream (default), '$' = only new",
    )
    parser.add_argument("--reset", action="store_true", help="start this run from scratch")
    parser.add_argument(
        "--exit-when-idle",
        type=float,
        metavar="SECONDS",
        help="stop after this many seconds without new entries (useful after a replay)",
    )
    parser.add_argument(
        "--max-entries", type=int, help="stop after processing this many entries (for testing)"
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace, settings: Settings, should_stop=lambda: False) -> int:
    """Run detection with the given arguments; returns the number of entries processed."""
    stream = args.stream_key or settings.stream_key
    keys = DetectKeys.for_run(args.run_id)
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
            if args.reset:
                reset_run(client, stream, keys)
                deleted = delete_run(conn, args.run_id)
                logger.info("Reset run %s (deleted %d spikes)", args.run_id, deleted)
            runner = DetectionRunner(
                client,
                stream,
                keys,
                SpikeDetector(settings.detector_config()),
                PostgresSink(conn, args.run_id),
                consumer=args.consumer,
                snapshot_interval_s=settings.detect_snapshot_interval_s,
            )
            runner.ensure_group(args.start_id)
            runner.restore()
            logger.info("Detecting spikes in %s as run %s", stream, args.run_id)
            processed = runner.run(should_stop, args.exit_when_idle, args.max_entries)
            logger.info(
                "Stopped after %d entries; %d spikes active; detector stats %s",
                processed,
                len(runner.detector.active),
                runner.detector.clock.stats,
            )
            return processed
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    stop = StopFlag()
    stop.install()
    run(args, settings, beat_while(stop, Heartbeat.from_settings(settings)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
