"""Backfill the lake from the stream's history: `uv run python -m wikipulse.lake.backfill`.

    uv run python -m wikipulse.lake.backfill --since 2026-11-07T13:00:00Z
        --until 2026-11-09T21:00:00Z [--prefix history/edits]

Wikimedia's EventStreams keeps recent history (about two weeks for `recentchange`).
Connecting with `?since=<time>` replays it from that moment, much faster than real
time. This command reads that history over one stream connection, keeps the same edits
as stage 1 (`is_wanted`, `normalize`), and writes them straight into the lake, hour by
hour, without a recording file or a Redis Stream in between (a full-stream recording
is about 230 MB per hour; the lake keeps the same hour's English edits in about 0.4 MB).

Every hour is *merged* into its file (`LakeStore.merge_hour`, duplicates removed by
`event_id`), so overlapping runs, a restart with an earlier `--since`, or history that
is already archived never produce duplicates. If the process dies, run it again with
`--since` set to the last hour it logged as written.

Only one writer may change a lake at a time, so the backfill holds the same Redis lock
as the archiver (`lake:writer:<bucket>/<prefix>`).
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import redis

from wikipulse.config import Settings, get_settings
from wikipulse.lake.lock import LockLostError, WriterLock, writer_lock_key
from wikipulse.lake.partitions import hour_of
from wikipulse.lake.store import LakeStore
from wikipulse.lake.table import events_to_table
from wikipulse.logging_setup import setup_logging
from wikipulse.stream.consume import warn_if_placeholder_user_agent
from wikipulse.stream.filters import WANTED_WIKI, is_wanted, normalize
from wikipulse.stream.models import EditEvent
from wikipulse.stream.resilience import (
    Backoff,
    install_stop_handlers,
    run_with_reconnect,
    until_stop,
)
from wikipulse.stream.sources import LiveSource, Source

logger = logging.getLogger(__name__)

# The stream mixes two data centres' topics, so event times are only roughly ordered.
# An hour is written once the newest event time is this far past the hour's end.
SETTLE = timedelta(minutes=5)


@dataclass
class HourCollector:
    """Buffers kept edits by event-time hour and says which hours are complete.

    Pure logic (no I/O): `add()` takes one edit, `ready()` returns the hours whose end
    lies more than `settle` behind the newest event time seen (the watermark).
    """

    until: datetime
    settle: timedelta = SETTLE
    buffers: dict[datetime, list[EditEvent]] = field(default_factory=lambda: defaultdict(list))
    watermark: datetime | None = None

    def add(self, event: EditEvent) -> None:
        if self.watermark is None or event.event_time > self.watermark:
            self.watermark = event.event_time
        if event.event_time < self.until:
            self.buffers[hour_of(event.event_time)].append(event)

    def ready(self) -> list[datetime]:
        """Hours that can be written now, oldest first."""
        if self.watermark is None:
            return []
        return sorted(
            h for h in self.buffers if h + timedelta(hours=1) + self.settle <= self.watermark
        )

    def done(self) -> bool:
        """True once the watermark is far enough past `until` that nothing more is kept."""
        return self.watermark is not None and self.watermark >= self.until + self.settle

    def pop(self, hour: datetime) -> list[EditEvent]:
        return self.buffers.pop(hour, [])


def parse_kept_edit(data: str) -> EditEvent | None:
    """The edit in a raw stream message, or None if stage 1 wouldn't keep it."""
    if WANTED_WIKI not in data:  # cheap pre-filter; `is_wanted` still decides
        return None
    try:
        raw = json.loads(data)
        if not isinstance(raw, dict) or not is_wanted(raw):
            return None
        return normalize(raw)
    except (ValueError, KeyError, TypeError):
        return None


class Backfill:
    """Reads the stream's history and writes complete hours to the lake."""

    def __init__(self, source: Source, store: LakeStore, lock: WriterLock, until: datetime):
        self.source = source
        self.store = store
        self.lock = lock
        self.collector = HourCollector(until=until)
        self.last_id: str | None = None
        self.messages = 0
        self.kept = 0
        self.hours_written = 0
        self.started = time.monotonic()
        self._renewed = time.monotonic()

    async def read_once(self, stop: asyncio.Event, backoff: Backoff) -> None:
        """One connection: read until `until` is passed, the server closes, or `stop`."""
        async for sse_id, data in until_stop(self.source.messages(self.last_id), stop):
            self.last_id = sse_id or self.last_id
            self.messages += 1
            backoff.reset()
            self.keep_lock()
            event = parse_kept_edit(data)
            if event is None:
                continue
            self.kept += 1
            self.collector.add(event)
            self.write_ready()
            if self.collector.done():
                stop.set()
                return

    def write_ready(self, everything: bool = False) -> None:
        hours = sorted(self.collector.buffers) if everything else self.collector.ready()
        for hour in hours:
            self.write_hour(hour, self.collector.pop(hour))

    def keep_lock(self, force: bool = False) -> None:
        """Renew the writer lock every third of its TTL (also while only reading).

        If it expired anyway (a long reconnect), take it again; if another writer has
        it by now, stop: `LockLostError`.
        """
        if not force and time.monotonic() - self._renewed < self.lock.ttl_ms / 3000:
            return
        if not (self.lock.renew() or self.lock.acquire()):
            raise LockLostError(f"another writer holds {self.lock.key}")
        self._renewed = time.monotonic()

    def write_hour(self, hour: datetime, events: list[EditEvent]) -> None:
        self.keep_lock(force=True)
        rows = self.store.merge_hour(hour, events_to_table(events))
        self.hours_written += 1
        logger.info(
            "Wrote %s: %d new edits, %d in the file (%d messages read, %.0f s)",
            f"{hour:%Y-%m-%dT%H:00Z}",
            len(events),
            rows,
            self.messages,
            time.monotonic() - self.started,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", required=True, help="ISO time to start from")
    parser.add_argument("--until", required=True, help="ISO time to stop at (exclusive)")
    parser.add_argument("--prefix", default="history/edits", help="key prefix in the bucket")
    return parser.parse_args(argv)


def parse_time(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


async def run(args: argparse.Namespace, settings: Settings) -> Backfill:
    since, until = parse_time(args.since), parse_time(args.until)
    if until <= since:
        raise ValueError("--until must be after --since")
    store = LakeStore.from_settings(settings, args.prefix)
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    lock = WriterLock(client, writer_lock_key(store.root), settings.lake_lock_ttl_s)
    if not lock.acquire():
        raise LockLostError(f"another writer holds {lock.key}; is an archiver running?")
    source = LiveSource(
        settings.stream_url, settings.user_agent, since=since.isoformat().replace("+00:00", "Z")
    )
    backfill = Backfill(source, store, lock, until)
    stop = asyncio.Event()
    install_stop_handlers(stop)
    backoff = Backoff(max_s=settings.reconnect_max_s)
    logger.info("Backfilling %s to %s into %s", since, until, store.root)
    try:
        await run_with_reconnect(
            lambda: backfill.read_once(stop, backoff),
            stop,
            backoff,
            name="backfill stream",
        )
        backfill.write_ready(everything=True)  # the last hours (or what a stop left)
    finally:
        lock.release()
        client.close()
    logger.info(
        "Done: %d messages read, %d edits kept, %d hour writes, newest event time %s",
        backfill.messages,
        backfill.kept,
        backfill.hours_written,
        backfill.collector.watermark,
    )
    return backfill


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    warn_if_placeholder_user_agent(settings)
    try:
        asyncio.run(run(args, settings))
    except LockLostError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
