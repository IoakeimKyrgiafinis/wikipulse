"""Recorder: `uv run python -m wikipulse.stream.record --minutes 60 [--since <ISO timestamp>]`.

Saves the *raw, unfiltered* stream (SSE id and data of every message) to
`data/recordings/<start time>.jsonl`, one JSON object per line. Replay it later with
`wikipulse.stream.replay`, without the network.
"""

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from wikipulse.config import Settings, get_settings
from wikipulse.logging_setup import setup_logging
from wikipulse.stream.consume import warn_if_placeholder_user_agent
from wikipulse.stream.resilience import (
    Backoff,
    install_stop_handlers,
    run_with_reconnect,
    until_stop,
)
from wikipulse.stream.sources import LiveSource, Source, format_recording_line

logger = logging.getLogger(__name__)


def default_output_path(data_dir: Path, started: datetime) -> Path:
    """`data/recordings/20261107T031500Z.jsonl` (no colons, so it is valid on Windows)."""
    return data_dir / "recordings" / f"{started:%Y%m%dT%H%M%SZ}.jsonl"


class Recorder:
    """Writes every message from a source to a file, reconnecting after failures."""

    def __init__(self, source: Source, out: TextIO) -> None:
        self.source = source
        self.out = out
        self.last_id: str | None = None
        self.count = 0
        self.reconnects = 0

    async def record_once(self, stop: asyncio.Event, backoff: Backoff) -> None:
        """One connection: append messages until it ends or `stop` is set."""
        async for sse_id, data in until_stop(self.source.messages(self.last_id), stop):
            self.out.write(format_recording_line(sse_id, data, datetime.now(UTC)) + "\n")
            self.out.flush()  # a crash loses at most the line being written
            self.last_id = sse_id or self.last_id
            self.count += 1
            backoff.reset()
            if self.count % 10_000 == 0:
                logger.info("Recorded %d messages", self.count)

    async def run(self, stop: asyncio.Event, backoff: Backoff) -> None:
        async def count_reconnect() -> None:
            self.reconnects += 1

        await run_with_reconnect(
            lambda: self.record_once(stop, backoff), stop, backoff, count_reconnect, "recorder"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, required=True, help="how long to record")
    parser.add_argument("--since", help="ISO timestamp to start from (recent history)")
    parser.add_argument("--output", type=Path, help="output file (default: data/recordings/...)")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace, settings: Settings) -> Path:
    """Record with the command-line options until stopped; returns the file."""
    started = datetime.now(UTC)
    path = args.output or default_output_path(settings.data_dir, started)
    path.parent.mkdir(parents=True, exist_ok=True)
    stop = asyncio.Event()
    install_stop_handlers(stop)
    # The duration is an operational limit on this process, not processing logic,
    # so wall-clock time is the right clock here.
    asyncio.get_running_loop().call_later(args.minutes * 60, stop.set)
    source = LiveSource(settings.stream_url, settings.user_agent, since=args.since)
    with path.open("w", encoding="utf-8", newline="\n") as out:
        recorder = Recorder(source, out)
        logger.info("Recording for %.1f minutes to %s", args.minutes, path)
        await recorder.run(stop, Backoff(max_s=settings.reconnect_max_s))
    logger.info(
        "Recorded %d messages to %s (%d reconnects)", recorder.count, path, recorder.reconnects
    )
    return path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    warn_if_placeholder_user_agent(settings)
    asyncio.run(run(args, settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
