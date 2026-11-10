"""The processing core: filter, normalize, deduplicate, write to Redis, save the checkpoint.

The core receives `(sse_id, raw_json)` pairs and doesn't know where they come from:
`LiveSource` (the network) and `FileSource` (a recording) look the same to it.

Delivery guarantee: the source delivers at least once, and the core turns that into
"each event exactly once in Redis". For each kept message, one Lua script does all of
this atomically (Redis runs a script as a single step, with no other command in between):

1. `SET <seen key> NX EX 3600` remembers the event id for an hour, and fails if it was
   already seen,
2. `XADD` appends the event to the stream (only if step 1 succeeded),
3. `SET` moves the checkpoint to this message's SSE id,
4. `HINCRBY` updates the counters in the stats hash.

So a crash leaves Redis either before or after a message, never half way. On restart
we resume from the checkpoint; anything the server sends again is skipped by step 1.

The live consumer also holds the stream's writer lease (`wikipulse.stream.lease`), and
both scripts first check that it still does (step 0). A consumer that lost the lease
gets `LeaseLost` and writes nothing, so two consumers never write the stream at once.

Metrics (spec 09a): every handled message counts in `wikipulse_consumer_messages_total`
by outcome, and every written edit moves the lag clock (`wikipulse.stream.lag`). The
replay uses this same class, so a replay exports the same metrics as the live consumer.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import redis.asyncio as aioredis
from pydantic import ValidationError
from redis.exceptions import ResponseError

from wikipulse.metrics.catalog import (
    CONSUMER_LAG,
    CONSUMER_LAST_EVENT,
    CONSUMER_MESSAGES,
    CONSUMER_RECONNECTS,
    start_at_zero,
)
from wikipulse.stream.filters import is_wanted, normalize
from wikipulse.stream.lag import WallClockLag
from wikipulse.stream.lease import LeaseLost, StreamLease
from wikipulse.stream.models import EditEvent
from wikipulse.stream.resilience import Backoff, run_with_reconnect, until_stop
from wikipulse.stream.sources import Source

logger = logging.getLogger(__name__)

DEDUP_TTL_S = 3600  # how long an event id is remembered for deduplication
LIVE_NAMESPACE = "wiki:consumer"

# Step 0 of both scripts: with a lease (owner not ''), stop unless we still hold it.
_FENCE = """
local lease, owner = KEYS[5], ARGV[8]
if owner ~= '' and redis.call('GET', lease) ~= owner then
  return redis.error_reply('LEASE_LOST ' .. lease)
end
"""

# Lua script for one kept event. See the module docstring.
_WRITE_SCRIPT = (
    _FENCE
    + """
local stream, checkpoint, seen, stats = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local sse_id, maxlen, ttl = ARGV[1], ARGV[2], ARGV[3]
local data, title, event_time, is_bot = ARGV[4], ARGV[5], ARGV[6], ARGV[7]
redis.call('HINCRBY', stats, 'received', 1)
local entry_id = false
if redis.call('SET', seen, '1', 'NX', 'EX', ttl) then
  entry_id = redis.call('XADD', stream, 'MAXLEN', '~', maxlen, '*',
                        'data', data, 'title', title, 'event_time', event_time)
  redis.call('HINCRBY', stats, 'kept', 1)
  if is_bot == '1' then redis.call('HINCRBY', stats, 'kept_bots', 1) end
  redis.call('HSET', stats, 'last_event_time', event_time)
else
  redis.call('HINCRBY', stats, 'duplicates', 1)
end
if sse_id ~= '' then redis.call('SET', checkpoint, sse_id) end
return entry_id
"""
)

# Lua script for a message we don't write: count it and move the checkpoint past it.
_SKIP_SCRIPT = (
    _FENCE
    + """
local checkpoint, stats = KEYS[2], KEYS[4]
local sse_id, outcome = ARGV[1], ARGV[2]
redis.call('HINCRBY', stats, 'received', 1)
redis.call('HINCRBY', stats, outcome, 1)
if sse_id ~= '' then redis.call('SET', checkpoint, sse_id) end
return 1
"""
)


class Outcome(StrEnum):
    """What happened to one message. The values are the stats hash counter names."""

    KEPT = "kept"
    DUPLICATE = "duplicates"
    DISCARDED = "discarded"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class RedisKeys:
    """The Redis keys one consumer uses. Namespaces keep live, replay and tests apart."""

    stream: str  # the Redis Stream of edits (XADD target)
    checkpoint: str  # SSE id of the last processed message
    stats: str  # counters hash
    seen_prefix: str  # prefix + event_id = deduplication key

    @classmethod
    def for_namespace(cls, stream: str, namespace: str = LIVE_NAMESPACE) -> "RedisKeys":
        """Keys under `namespace`, e.g. `wiki:consumer:last_id` for the live consumer."""
        return cls(
            stream=stream,
            checkpoint=f"{namespace}:last_id",
            stats=f"{namespace}:stats",
            seen_prefix=f"{namespace}:seen:",
        )


def parse_message(raw_json: str) -> dict[str, Any] | None:
    """Parse a message payload; None if it isn't a JSON object."""
    try:
        raw = json.loads(raw_json)
    except ValueError:
        return None
    return raw if isinstance(raw, dict) else None


class Consumer:
    """Turns raw messages into deduplicated `EditEvent`s in a Redis Stream."""

    def __init__(
        self,
        redis: aioredis.Redis,
        keys: RedisKeys,
        maxlen: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lease: StreamLease | None = None,
        lag: WallClockLag | None = None,
    ) -> None:
        self.redis = redis
        self.keys = keys
        self.maxlen = maxlen
        self.lease = lease  # None: no lease check (replay, tests)
        # The lag metric: wall clock by default, the replay's own clock in a replay.
        self.lag = lag or WallClockLag()
        CONSUMER_LAG.set_function(self.lag.seconds)
        start_at_zero(CONSUMER_MESSAGES, *(outcome.value for outcome in Outcome))
        self._clock = clock  # only for the `received_at` metadata, never for processing logic
        self._write_script = redis.register_script(_WRITE_SCRIPT)
        self._skip_script = redis.register_script(_SKIP_SCRIPT)

    async def load_checkpoint(self) -> str | None:
        """The SSE id of the last processed message, or None on the very first run."""
        value = await self.redis.get(self.keys.checkpoint)
        return value.decode() if isinstance(value, bytes) else value

    async def process(self, sse_id: str | None, raw_json: str) -> Outcome:
        """Handle one message completely, checkpoint included. Bad input never raises."""
        raw = parse_message(raw_json)
        if raw is None:
            logger.warning("Malformed message (not a JSON object), sse_id=%s", sse_id)
            return await self._skip(sse_id, Outcome.MALFORMED)
        try:
            event = normalize(raw, received_at=self._clock()) if is_wanted(raw) else None
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            logger.warning("Malformed message (%s: %s), sse_id=%s", type(exc).__name__, exc, sse_id)
            return await self._skip(sse_id, Outcome.MALFORMED)
        if event is None:
            return await self._skip(sse_id, Outcome.DISCARDED)
        return await self._write(sse_id, event)

    async def _write(self, sse_id: str | None, event: EditEvent) -> Outcome:
        """Deduplicate, append and checkpoint in one atomic Lua script."""
        args = [
            sse_id or "",
            self.maxlen,
            DEDUP_TTL_S,
            event.model_dump_json(),
            event.title,
            event.event_time.isoformat(),
            "1" if event.is_bot else "0",
        ]
        entry_id = await self._run(self._write_script, event.event_id, args)
        if entry_id is None:
            CONSUMER_MESSAGES.labels(Outcome.DUPLICATE.value).inc()
            return Outcome.DUPLICATE
        CONSUMER_MESSAGES.labels(Outcome.KEPT.value).inc()
        self.lag.observe(event.event_time)
        CONSUMER_LAST_EVENT.set(self.lag.last_event.timestamp())  # the newest, not the last
        return Outcome.KEPT

    async def _skip(self, sse_id: str | None, outcome: Outcome) -> Outcome:
        """Count a message we don't write, and still move the checkpoint past it."""
        args = [sse_id or "", outcome.value, "", "", "", "", ""]
        await self._run(self._skip_script, "", args)
        CONSUMER_MESSAGES.labels(outcome.value).inc()
        return outcome

    async def _run(self, script: Any, event_id: str, args: list[Any]) -> Any:
        """Run one of the scripts with all five keys and the lease owner (ARGV[8])."""
        lease_key, owner = (self.lease.key, self.lease.owner) if self.lease else ("", "")
        keys = [
            self.keys.stream,
            self.keys.checkpoint,
            self.keys.seen_prefix + event_id,
            self.keys.stats,
            lease_key,
        ]
        try:
            return await script(keys=keys, args=[*args, owner])
        except ResponseError as exc:
            if "LEASE_LOST" in str(exc):
                raise LeaseLost(f"no longer holding the writer lease {lease_key}") from exc
            raise

    async def count_reconnect(self) -> None:
        """Add one to the `reconnects` counter."""
        await self.redis.hincrby(self.keys.stats, "reconnects", 1)
        CONSUMER_RECONNECTS.inc()

    async def consume(
        self, source: Source, stop: asyncio.Event, last_event_id: str | None = None
    ) -> int:
        """One pass over `source`, until it ends or `stop` is set. Returns messages processed.

        Used by replay. Errors propagate to the caller; there is no reconnecting.
        """
        count = 0
        async for sse_id, raw_json in until_stop(source.messages(last_event_id), stop):
            await self.process(sse_id, raw_json)
            count += 1
        return count

    async def run_live(
        self,
        source: Source,
        stop: asyncio.Event,
        backoff: Backoff,
        heartbeat: Callable[[], None] = lambda: None,
    ) -> None:
        """Consume until `stop`: resume from the checkpoint, reconnect with backoff on failure.

        Each attempt reloads the checkpoint from Redis, so a message whose write failed
        (e.g. Redis was briefly down) is simply read again from the source.
        `heartbeat` is called at every connection attempt and after every message.
        """

        async def attempt() -> None:
            heartbeat()
            last_id = await self.load_checkpoint()
            logger.info("Connecting, resuming from checkpoint: %s", last_id or "none")
            try:
                async for sse_id, raw_json in until_stop(source.messages(last_id), stop):
                    await self.process(sse_id, raw_json)
                    backoff.reset()
                    heartbeat()
            except LeaseLost as exc:  # someone else writes the stream now: stop, don't retry
                logger.error("%s; stopping", exc)
                stop.set()

        await run_with_reconnect(attempt, stop, backoff, self.count_reconnect, name="stream")
