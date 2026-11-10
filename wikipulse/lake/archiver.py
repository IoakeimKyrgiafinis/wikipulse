"""Archives the edit stream into the lake, hour by hour, without losing or doubling edits.

**A second consumer group.** The archiver reads the same Redis Stream as the spike
detector, but through its own consumer group (`LAKE_GROUP`, default `lake:archive`). A
group has its own position and its own pending entries list (PEL), so the two readers
never wait for or take entries from each other: the detector can be minutes behind or
stopped while the archiver catches up, and the other way round.

**Buffer by hour, write, then acknowledge.** Entries are read with `XREADGROUP` and
sorted into one buffer per hour of *event time* (so a late edit joins the hour it
happened in, even if that hour was archived long ago). Every `flush_interval_s` seconds,
when the buffers hold `max_buffer_rows` edits, and when the archiver stops, each
buffered hour is merged into its file in the lake (`LakeStore.merge_hour`), and only
*then* are that hour's entries acknowledged with `XACK`.

**Why nothing is lost or doubled.**
- A crash before a file is written: its entries were never acknowledged, so they are
  still in the group's PEL. On start the archiver first claims the pending entries of
  any *other* consumer name in its group (`XCLAIM`; a restart under another `--consumer`
  name), then re-reads its own PEL and writes them.
- A crash after the file is written but before `XACK`: the entries are read again and
  merged again, but the merge keeps one row per `event_id`, so the file comes out the
  same.
- The file itself is replaced in one step, so a crash mid-upload leaves the old file.
- Only one archiver writes to a lake at a time (`WriterLock`, a Redis key with a TTL).
  A second one waits until the first stops or its lock expires, instead of overwriting
  the first one's files. That is also what makes claiming other consumers' entries safe.
"""

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import redis
from pydantic import ValidationError

from wikipulse.lake.lock import LockLostError, WriterLock, writer_lock_key
from wikipulse.lake.partitions import hour_of
from wikipulse.lake.store import LakeStore
from wikipulse.lake.table import events_to_table
from wikipulse.metrics.catalog import ARCHIVER_EDITS, ARCHIVER_FAILURES
from wikipulse.stream.models import EditEvent

logger = logging.getLogger(__name__)

DEFAULT_CONSUMER = "archiver-1"
ACK_CHUNK = 1000  # ids per XACK command
RETRY_MIN_S = 5.0  # first wait after a failed write to the lake; doubles up to RETRY_MAX_S
RETRY_MAX_S = 300.0
LOCK_POLL_S = 1.0  # how often a waiting archiver checks whether the lock is free

Entry = tuple[str, dict[str, str] | None]


@dataclass
class HourBuffer:
    """Edits of one hour waiting to be written, and the stream entries they came from."""

    events: list[EditEvent] = field(default_factory=list)
    entry_ids: list[str] = field(default_factory=list)


@dataclass
class ArchiveStats:
    entries: int = 0  # stream entries read (including skipped ones)
    skipped: int = 0  # entries without valid data (acknowledged, nothing archived)
    flushes: int = 0  # times the buffers were written
    hour_writes: int = 0  # hour files written (one flush can write several hours)


class Archiver:
    """Reads one stream with a consumer group and archives it into a `LakeStore`."""

    def __init__(
        self,
        client: redis.Redis,
        stream: str,
        group: str,
        store: LakeStore,
        consumer: str = DEFAULT_CONSUMER,
        flush_interval_s: float = 300.0,
        max_buffer_rows: int = 50_000,
        batch_size: int = 1000,
        block_ms: int = 2000,
        lock_ttl_s: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.stream = stream
        self.group = group
        self.store = store
        self.consumer = consumer
        self.flush_interval_s = flush_interval_s
        self.max_buffer_rows = max_buffer_rows
        self.batch_size = batch_size
        self.block_ms = block_ms
        self._monotonic = monotonic
        self._sleep = sleep
        self.lock = WriterLock(client, writer_lock_key(store.root), lock_ttl_s)
        self._lock_renew_s = lock_ttl_s / 3  # renew well before the lock expires
        self._lock_renewed_at = 0.0
        self.buffers: dict[datetime, HourBuffer] = defaultdict(HourBuffer)
        self._skipped_ids: list[str] = []  # acknowledged with the next flush
        self._last_flush = monotonic()
        self._recovered = False  # pending entries re-read yet?
        self._retry_at = 0.0  # no flush attempt before this time (after a failed write)
        self._retry_delay_s = RETRY_MIN_S
        self.stats = ArchiveStats()

    # --- setup ---

    def ensure_group(self, start_id: str = "0") -> None:
        """Create the consumer group if needed. `start_id` '0' = from the stream's start."""
        try:
            self.client.xgroup_create(self.stream, self.group, id=start_id, mkstream=True)
            logger.info("Created consumer group %s on %s at %s", self.group, self.stream, start_id)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    # --- main loop ---

    def run(
        self,
        should_stop: Callable[[], bool] = lambda: False,
        idle_exit_s: float | None = None,
        max_entries: int | None = None,
        final_flush: bool = True,
    ) -> ArchiveStats:
        """Archive pending, then new entries until stopped.

        `idle_exit_s`: stop after that many seconds without new entries (after a replay).
        `max_entries`: stop after reading that many entries (for tests).
        `final_flush=False` leaves the buffers unwritten, as a crash would (for tests).

        Nothing is read before the lake's writer lock is held: if another archiver holds
        it, this one waits (until it is stopped). The lock is released on return.
        """

        def done() -> bool:
            return should_stop() or (max_entries is not None and self.stats.entries >= max_entries)

        if not self._wait_for_lock(done):
            return self.stats
        try:
            if not self._recovered:  # once per process: later entries are already buffered
                self._recovered = self._read_pending(done)
            last_activity = self._monotonic()
            while not done():
                self._renew_lock_if_due()
                entries = self._read(">", block=True, limit=self._limit(max_entries))
                if entries:
                    self._add(entries)
                    last_activity = self._monotonic()
                elif idle_exit_s is not None and self._monotonic() - last_activity >= idle_exit_s:
                    logger.info("No new entries for %.0f s, stopping", idle_exit_s)
                    break
                self._maybe_flush()
            if final_flush:
                self.flush()
            return self.stats
        finally:
            self.lock.release()

    # --- the single-writer lock ---

    def _wait_for_lock(self, done: Callable[[], bool]) -> bool:
        """Take the writer lock, waiting while another archiver holds it.

        Returns False if stopped before the lock was free.
        """
        waiting = False
        while not self.lock.acquire():
            if not waiting:
                logger.warning(
                    "Another archiver is writing to %s (lock %s); waiting until it stops "
                    "or its lock expires",
                    self.store.root,
                    self.lock.key,
                )
                waiting = True
            if done():
                return False
            self._sleep(LOCK_POLL_S)
        if waiting:
            logger.info("Got the writer lock %s", self.lock.key)
        self._lock_renewed_at = self._monotonic()
        return True

    def _renew_lock_if_due(self) -> None:
        if self._monotonic() - self._lock_renewed_at >= self._lock_renew_s:
            self._hold_lock()

    def _hold_lock(self) -> None:
        """Make sure this archiver still owns the lock, with a fresh TTL; else raise."""
        if self.lock.owned:
            held = self.lock.renew()
        else:  # flush() called outside run()
            held = self.lock.acquire()
        if not held:
            raise LockLostError(
                f"Lake writer lock {self.lock.key} is held by another archiver; "
                "stopping without writing (buffered edits stay unacknowledged)"
            )
        self._lock_renewed_at = self._monotonic()

    def _limit(self, max_entries: int | None) -> int:
        """Entries to ask for, so `max_entries` is never overshot (unread = not pending)."""
        if max_entries is None:
            return self.batch_size
        return max(1, min(self.batch_size, max_entries - self.stats.entries))

    def _read_pending(self, done: Callable[[], bool]) -> bool:
        """Re-read the group's unacknowledged entries (left over from a crash).

        Returns True when all of them were read (False if stopped half-way).
        """
        self._claim_other_consumers()
        cursor = "0"
        while not done():
            entries = self._read(cursor, block=False, limit=self.batch_size)
            if not entries:
                return True
            logger.info("Re-reading %d pending entries after %s", len(entries), cursor)
            self._add(entries)
            cursor = entries[-1][0]
            self._renew_lock_if_due()
            self._maybe_flush()
        return False

    def _claim_other_consumers(self) -> int:
        """Move entries pending under other consumer names of the group to this one.

        A restart under a different `--consumer` name would otherwise never see them (a
        permanent gap). Safe to do immediately (min idle time 0) because this archiver
        holds the lake's writer lock, so no other archiver is working on them.
        """
        claimed = 0
        for consumer in self.client.xpending(self.stream, self.group).get("consumers") or []:
            name = consumer["name"]
            if name == self.consumer:
                continue
            claimed += self._claim_from(name)
            self.client.xgroup_delconsumer(self.stream, self.group, name)
        return claimed

    def _claim_from(self, name: str) -> int:
        claimed, previous = 0, None
        while True:
            pending = self.client.xpending_range(
                self.stream, self.group, "-", "+", self.batch_size, consumername=name
            )
            ids = [item["message_id"] for item in pending]
            if not ids or ids == previous:  # done, or nothing could be moved
                break
            self.client.xclaim(self.stream, self.group, self.consumer, 0, ids, justid=True)
            claimed += len(ids)
            previous = ids
        if claimed:
            logger.warning(
                "Claimed %d pending entries of consumer %s (an earlier archiver under "
                "another name); they are archived now",
                claimed,
                name,
            )
        return claimed

    def _read(self, from_id: str, block: bool, limit: int) -> list[Entry]:
        response = self.client.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: from_id},
            count=limit,
            block=self.block_ms if block else None,
        )
        return response[0][1] if response else []

    def _add(self, entries: list[Entry]) -> None:
        """Put each entry's edit into the buffer of its event-time hour."""
        for entry_id, fields in entries:
            self.stats.entries += 1
            event = parse_entry(entry_id, fields)
            if event is None:
                self.stats.skipped += 1
                self._skipped_ids.append(entry_id)
                continue
            buffer = self.buffers[hour_of(event.event_time)]
            buffer.events.append(event)
            buffer.entry_ids.append(entry_id)

    # --- writing ---

    def buffered_rows(self) -> int:
        return sum(len(buffer.events) for buffer in self.buffers.values())

    def _maybe_flush(self) -> None:
        """Flush when due. If the lake is unreachable, keep buffering and retry later."""
        now = self._monotonic()
        if now < self._retry_at:
            return
        interval_passed = now - self._last_flush >= self.flush_interval_s
        if not (interval_passed or self.buffered_rows() >= self.max_buffer_rows):
            return
        try:
            self.flush()
            self._retry_delay_s = RETRY_MIN_S
        except OSError as exc:  # pyarrow reports S3 errors as OSError
            ARCHIVER_FAILURES.inc()
            logger.error(
                "Writing to the lake failed (%s); %d edits stay buffered and unacknowledged, "
                "retrying in %.0f s",
                exc,
                self.buffered_rows(),
                self._retry_delay_s,
            )
            self._retry_at = now + self._retry_delay_s
            self._retry_delay_s = min(self._retry_delay_s * 2, RETRY_MAX_S)

    def flush(self) -> None:
        """Write every buffered hour to the lake, acknowledging each hour after its write."""
        for hour in sorted(self.buffers):
            buffer = self.buffers[hour]
            self._hold_lock()  # never write an hour without owning the lake
            rows = self.store.merge_hour(hour, events_to_table(buffer.events))
            self._ack(buffer.entry_ids)  # only now: the hour's file is safely written
            ARCHIVER_EDITS.inc(len(buffer.entry_ids))
            del self.buffers[hour]
            self.stats.hour_writes += 1
            logger.info(
                "Archived %d edits into %s (%d rows)",
                len(buffer.entry_ids),
                self.store.path(hour),
                rows,
            )
        self._ack(self._skipped_ids)
        self._skipped_ids = []
        self.stats.flushes += 1
        self._last_flush = self._monotonic()

    def _ack(self, entry_ids: list[str]) -> None:
        for start in range(0, len(entry_ids), ACK_CHUNK):
            self.client.xack(self.stream, self.group, *entry_ids[start : start + ACK_CHUNK])


def parse_entry(entry_id: str, fields: dict[str, str] | None) -> EditEvent | None:
    """The edit in a stream entry, or None (logged) if the entry has no valid data."""
    if not fields or "data" not in fields:  # trimmed from the stream, or a foreign entry
        logger.warning("Skipping stream entry %s without data", entry_id)
        return None
    try:
        return EditEvent.model_validate_json(fields["data"])
    except ValidationError as exc:
        logger.warning("Skipping malformed stream entry %s: %s", entry_id, exc)
        return None


def reset_group(client: redis.Redis, stream: str, group: str) -> None:
    """Delete the archiver's consumer group (the lake itself is left alone)."""
    try:
        client.xgroup_destroy(stream, group)
    except redis.ResponseError:
        pass  # the stream doesn't exist yet
