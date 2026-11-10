"""Feeds the Redis Stream to the detector through a consumer group, with restart safety.

**Consumer groups.** `XREADGROUP GROUP <group> <consumer> ... STREAMS <stream> >` hands
out stream entries the group hasn't delivered yet. Redis remembers, per group, which
entries were delivered to which consumer and not yet acknowledged (the *pending entries
list*, PEL). `XACK` removes an entry from the PEL: "done, don't give it to me again".
Different groups read the same stream independently (the detector, later a data-lake
writer, ...), each at its own position. Several consumers *inside* one group share the
work; this detector uses one consumer per group, because all edits of a page must reach
the same detector state.

**State that survives restarts.** Every `snapshot_interval_s` (wall clock; only *when*
to save, never *what* is computed) the runner
1. writes the spikes that changed to the sink (Postgres) with idempotent upserts,
2. in one Redis transaction (`MULTI`/`EXEC`): saves the detector state together with the
   id of the last processed entry, updates the changed page baselines, and `XACK`s every
   entry processed since the previous snapshot.

After a crash, the state is the one from the last snapshot, and every entry processed
after it is still in the PEL (never acknowledged). On start the runner first re-reads
its own PEL (`XREADGROUP ... STREAMS <stream> 0`), skips entries up to the saved id, and
processes the rest again, then continues with new entries (`>`). So each entry affects
the state exactly once, and spikes written twice are simply overwritten.
"""

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import redis
from pydantic import ValidationError

from wikipulse.detect.detector import SpikeDetector, SpikeRecord
from wikipulse.metrics.catalog import (
    DETECTOR_ENTRIES,
    DETECTOR_LAST_EVENT,
    DETECTOR_PENDING,
    DETECTOR_STREAM_LAG,
    SPIKES_ACTIVE,
    SPIKES_STARTED,
    start_at_zero,
)
from wikipulse.stream.models import EditEvent

logger = logging.getLogger(__name__)

DEFAULT_CONSUMER = "detector-1"
CHUNK = 1000  # ids per XACK / fields per HSET, to keep single commands small


class SpikeSink(Protocol):
    """Where changed spikes go (Postgres in production, a list in tests)."""

    def write(self, spikes: list[SpikeRecord]) -> None: ...


@dataclass(frozen=True)
class DetectKeys:
    """Redis names used by one detector run."""

    group: str  # consumer group on the edits stream
    state: str  # String: detector state as JSON, including the last processed entry id
    baselines: str  # Hash: page title -> [level, updated_at] as JSON

    @classmethod
    def for_run(cls, run_id: str) -> "DetectKeys":
        return cls(
            group=f"detect:{run_id}",
            state=f"wiki:detect:{run_id}:state",
            baselines=f"wiki:detect:{run_id}:baselines",
        )


def entry_id_key(entry_id: str) -> tuple[int, int]:
    """'1794014244928-3' -> (1794014244928, 3), so ids compare in stream order."""
    ms, _, seq = entry_id.partition("-")
    return int(ms), int(seq or 0)


def chunks(items: list[Any], size: int = CHUNK) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class DetectionRunner:
    """Reads one stream with a consumer group and drives a `SpikeDetector`."""

    def __init__(
        self,
        client: redis.Redis,
        stream: str,
        keys: DetectKeys,
        detector: SpikeDetector,
        sink: SpikeSink,
        consumer: str = DEFAULT_CONSUMER,
        snapshot_interval_s: float = 60.0,
        batch_size: int = 500,
        block_ms: int = 2000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.stream = stream
        self.keys = keys
        self.detector = detector
        self.sink = sink
        self.consumer = consumer
        self.snapshot_interval_s = snapshot_interval_s
        self.batch_size = batch_size
        self.block_ms = block_ms
        self._monotonic = monotonic
        self.last_id: str | None = None  # last entry applied to the detector state
        self._unacked: list[str] = []
        self._last_snapshot = monotonic()
        self.processed = 0  # entries applied in this process (not counting skipped ones)
        start_at_zero(SPIKES_STARTED, "short", "long")

    # --- setup ---

    def ensure_group(self, start_id: str = "0") -> None:
        """Create the consumer group if needed. `start_id` '0' = from the stream's start."""
        try:
            self.client.xgroup_create(self.stream, self.keys.group, id=start_id, mkstream=True)
            logger.info(
                "Created consumer group %s on %s at %s", self.keys.group, self.stream, start_id
            )
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def restore(self) -> bool:
        """Load the last snapshot into the detector. False if there is none."""
        raw = self.client.get(self.keys.state)
        if raw is None:
            return False
        state = json.loads(raw)
        pages = {
            title: json.loads(value)
            for title, value in self.client.hgetall(self.keys.baselines).items()
        }
        self.detector.load_state(state["detector"], pages)
        self.last_id = state["last_id"]
        logger.info(
            "Restored state: last entry %s, %d active spikes, %d page baselines",
            self.last_id,
            len(self.detector.active),
            len(pages),
        )
        return True

    # --- main loop ---

    def run(
        self,
        should_stop: Callable[[], bool] = lambda: False,
        idle_exit_s: float | None = None,
        max_entries: int | None = None,
    ) -> int:
        """Process pending, then new entries until stopped. Returns entries processed.

        `idle_exit_s`: stop after that many seconds without new entries (for replays).
        `max_entries`: stop after processing that many entries (for restart tests).
        A final snapshot is always taken when the loop ends normally.
        """

        def done() -> bool:
            return should_stop() or (max_entries is not None and self.processed >= max_entries)

        self._replay_pending(done)
        last_activity = self._monotonic()
        while not done():
            entries = self._read(">", block=True)
            if entries:
                self._apply(entries, done)
                last_activity = self._monotonic()
            elif idle_exit_s is not None and self._monotonic() - last_activity >= idle_exit_s:
                logger.info("No new entries for %.0f s, stopping", idle_exit_s)
                break
            self._maybe_snapshot()
        self.snapshot()
        return self.processed

    def _replay_pending(self, done: Callable[[], bool]) -> None:
        """Re-read this consumer's unacknowledged entries (left over from a crash)."""
        cursor = "0"
        while not done():
            entries = self._read(cursor, block=False)
            if not entries:
                return
            logger.info("Re-reading %d pending entries after %s", len(entries), cursor)
            self._apply(entries, done)
            cursor = entries[-1][0]
            self._maybe_snapshot()

    def _read(self, from_id: str, block: bool) -> list[tuple[str, dict[str, str] | None]]:
        response = self.client.xreadgroup(
            self.keys.group,
            self.consumer,
            {self.stream: from_id},
            count=self.batch_size,
            block=self.block_ms if block else None,
        )
        return response[0][1] if response else []

    def _apply(
        self, entries: list[tuple[str, dict[str, str] | None]], done: Callable[[], bool]
    ) -> None:
        for entry_id, fields in entries:
            if done():
                return  # not acknowledged: will be delivered again via the PEL
            self._unacked.append(entry_id)
            if self.last_id is not None and entry_id_key(entry_id) <= entry_id_key(self.last_id):
                continue  # already in the restored state
            self.last_id = entry_id
            self.processed += 1
            DETECTOR_ENTRIES.inc()
            event = self._parse(entry_id, fields)
            if event is not None:
                self.detector.process(event)
                newest = self.detector.now
                if newest is not None:  # the watermark: the newest event time seen
                    DETECTOR_LAST_EVENT.set(newest)
        SPIKES_ACTIVE.set(len(self.detector.active))

    @staticmethod
    def _parse(entry_id: str, fields: dict[str, str] | None) -> EditEvent | None:
        if not fields or "data" not in fields:  # trimmed from the stream, or foreign entry
            logger.warning("Skipping stream entry %s without data", entry_id)
            return None
        try:
            return EditEvent.model_validate_json(fields["data"])
        except ValidationError as exc:
            logger.warning("Skipping malformed stream entry %s: %s", entry_id, exc)
            return None

    # --- snapshots ---

    def _maybe_snapshot(self) -> None:
        if self._monotonic() - self._last_snapshot >= self.snapshot_interval_s:
            self.snapshot()

    def snapshot(self) -> None:
        """Write changed spikes, then save state and acknowledge entries atomically."""
        changes = self.detector.pop_changes()
        if changes:
            self.sink.write(changes)
        baselines = self.detector.baselines
        changed_pages = {
            title: json.dumps(baselines.pages[title].to_list()) for title in baselines.changed
        }
        state = {"last_id": self.last_id, "detector": self.detector.state_to_dict()}
        with self.client.pipeline(transaction=True) as pipe:  # MULTI ... EXEC
            pipe.set(self.keys.state, json.dumps(state))
            items = sorted(changed_pages.items())
            for part in chunks(items):
                pipe.hset(self.keys.baselines, mapping=dict(part))
            for part in chunks(sorted(baselines.removed)):
                pipe.hdel(self.keys.baselines, *part)
            for part in chunks(self._unacked):
                pipe.xack(self.stream, self.keys.group, *part)
            pipe.execute()
        logger.debug(
            "Snapshot: last entry %s, %d spikes written, %d baselines, %d acked",
            self.last_id,
            len(changes),
            len(changed_pages),
            len(self._unacked),
        )
        baselines.changed.clear()
        baselines.removed.clear()
        self._unacked.clear()
        self._last_snapshot = self._monotonic()
        self.update_queue_metrics()

    def update_queue_metrics(self) -> None:
        """The queue in front of the detector, from Redis: entries not yet delivered to the
        group (`lag`) and delivered but not acknowledged (`pending`). Once per snapshot."""
        try:
            groups = self.client.xinfo_groups(self.stream)
        except redis.RedisError as exc:  # metrics must never stop detection
            logger.debug("Could not read the group's lag: %s", exc)
            return
        for group in groups:
            if group.get("name") == self.keys.group:
                if group.get("lag") is not None:  # unknown after deletions in the stream
                    DETECTOR_STREAM_LAG.set(group["lag"])
                DETECTOR_PENDING.set(group.get("pending", 0))


def reset_run(client: redis.Redis, stream: str, keys: DetectKeys) -> None:
    """Forget a run's Redis state: its consumer group, snapshot and baselines."""
    try:
        client.xgroup_destroy(stream, keys.group)
    except redis.ResponseError:
        pass  # the stream doesn't exist yet
    client.delete(keys.state, keys.baselines)
