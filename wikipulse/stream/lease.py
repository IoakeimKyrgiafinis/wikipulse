"""The writer lease: at most one live consumer writes a Redis Stream at a time.

Two consumers writing `wiki:edits` would put every edit in twice (each has its own
dedup keys when their namespaces differ), and the detector would count it twice. So a
consumer must hold the lease before it writes:

- the lease is the Redis key `<stream>:writer`, whose value names the holder
  (`<host>:<pid>:<random>`; in Kubernetes the host is the pod name),
- it is taken with `SET key owner NX PX ttl`, which succeeds only if nobody holds it,
- the holder renews it every third of the TTL while it runs, and deletes it on a clean
  stop; a killed holder simply stops renewing, and the key expires after the TTL,
- a second consumer waits, checking every few seconds, until it can take the lease.

Renewal alone isn't enough: a holder that stalls for longer than the TTL (a frozen
laptop, Redis unreachable) could come back after someone else took over. So the
consumer's write script also checks, atomically with each write, that it still holds
the lease (a "fencing" check, see `wikipulse.stream.consumer`).
"""

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Callable

import redis.asyncio as aioredis

from wikipulse.stream.resilience import wait_or_stop

logger = logging.getLogger(__name__)

# Renew the TTL if we still hold the lease; take it again if it expired meanwhile and
# nobody else took it. Returns 1 if we hold it afterwards, 0 if someone else does.
_RENEW_SCRIPT = """
local holder = redis.call('GET', KEYS[1])
if holder == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
end
if not holder then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
  return 1
end
return 0
"""

# Delete the lease only if we hold it (never someone else's).
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


class LeaseLost(RuntimeError):
    """Raised when a write finds that this consumer no longer holds the lease."""


def lease_key(stream: str) -> str:
    """The Redis key of the writer lease of `stream`, e.g. `wiki:edits:writer`."""
    return f"{stream}:writer"


def default_owner() -> str:
    """A name unique to this process: host (the pod name in Kubernetes), pid, random."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _text(value: bytes | str | None) -> str | None:
    return value.decode() if isinstance(value, bytes) else value


class StreamLease:
    """The writer lease of one Redis Stream, held by one owner."""

    def __init__(
        self,
        redis: aioredis.Redis,
        stream: str,
        ttl_s: float,
        owner: str | None = None,
    ) -> None:
        self.redis = redis
        self.key = lease_key(stream)
        self.ttl_ms = int(ttl_s * 1000)
        self.owner = owner or default_owner()
        self._renew = redis.register_script(_RENEW_SCRIPT)
        self._release = redis.register_script(_RELEASE_SCRIPT)

    @property
    def renew_every_s(self) -> float:
        return self.ttl_ms / 3000

    async def try_acquire(self) -> bool:
        """Take the lease if nobody holds it (or we do already). True if we hold it."""
        return bool(await self._renew(keys=[self.key], args=[self.owner, self.ttl_ms]))

    async def holder(self) -> str | None:
        """Who holds the lease now, or None."""
        return _text(await self.redis.get(self.key))

    async def renew(self) -> bool:
        """Extend the lease. False if someone else holds it now."""
        return await self.try_acquire()

    async def release(self) -> None:
        """Give the lease up, so a waiting consumer can start at once."""
        await self._release(keys=[self.key], args=[self.owner])

    async def acquire(
        self,
        stop: asyncio.Event,
        retry_s: float = 5.0,
        on_wait: Callable[[], None] = lambda: None,
    ) -> bool:
        """Wait until we hold the lease (True) or `stop` is set (False).

        `on_wait` is called on every check, e.g. to beat the heartbeat: a consumer waiting
        for a killed pod's lease to expire is healthy, just not writing yet.
        """
        last_holder: str | None = None
        while not stop.is_set():
            on_wait()
            try:
                if await self.try_acquire():
                    logger.info("Holding the writer lease %s as %s", self.key, self.owner)
                    return True
                holder = await self.holder()
            except Exception as exc:  # Redis not reachable yet: keep trying
                logger.warning("Could not take the writer lease: %s: %s", type(exc).__name__, exc)
                holder = last_holder
            if holder != last_holder:
                logger.warning(
                    "Another consumer (%s) is writing this stream: waiting until its lease "
                    "%s is released or expires (at most %.0f s after it stops renewing)",
                    holder,
                    self.key,
                    self.ttl_ms / 1000,
                )
                last_holder = holder
            if await wait_or_stop(stop, retry_s):
                return False
        return False

    async def keep(self, stop: asyncio.Event) -> bool:
        """Renew the lease until `stop` is set. Sets `stop` and returns False if it is lost.

        A Redis error doesn't end it (the fencing check protects the writes meanwhile);
        the next renewal tries again.
        """
        while not await wait_or_stop(stop, self.renew_every_s):
            try:
                held = await self.renew()
            except Exception as exc:
                logger.warning("Could not renew the writer lease: %s: %s", type(exc).__name__, exc)
                continue
            if not held:
                logger.error(
                    "Lost the writer lease %s to %s; stopping", self.key, await self.holder()
                )
                stop.set()
                return False
        return True
