"""A single-writer lock in Redis: only one archiver may write to a lake at a time.

Every flush reads an hour's file, merges new rows in and replaces the whole object.
If two archivers did that for the same hour at once, the second upload would replace
the first one's rows, and both would acknowledge their entries: rows silently lost.
A consumer group alone can't prevent that (it hands *different* entries to each
consumer, but both may write the *same* hour), so the archiver holds this lock.

**How it works.** `SET key token NX PX ttl` creates the key only if it doesn't exist
(NX) and lets it expire after `ttl` (PX). The random `token` says who owns it. While
running, the owner renews the expiry; renewing and releasing first check that the key
still holds *its* token (with WATCH/MULTI, so the check and the change are one step),
so a process that lost the lock can never extend or delete someone else's.

**Why a TTL.** A process killed hard can't release anything. The TTL makes its lock
disappear by itself, so a restarted archiver waits at most `ttl` seconds.

**Limits.** This is a guard against accidental second starts, not a fencing scheme:
a writer that freezes for longer than the TTL (between its last renewal and its
upload) could still overlap with a new owner. The archiver renews right before every
hour write to keep that window as small as the S3 timeouts allow.
"""

import logging
import uuid

import redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "lake:writer:"


def writer_lock_key(lake_root: str) -> str:
    """The lock key of one lake ('<bucket>/<prefix>' or a local folder)."""
    return f"{KEY_PREFIX}{lake_root}"


class LockLostError(RuntimeError):
    """The lock expired or was taken by another writer; nothing may be written."""


class WriterLock:
    """A Redis lock with an expiry, owned by one process at a time."""

    def __init__(self, client: redis.Redis, key: str, ttl_s: float) -> None:
        if ttl_s <= 0:
            raise ValueError("the lock TTL must be positive")
        self.client = client
        self.key = key
        self.ttl_ms = max(1, int(ttl_s * 1000))
        self.token: str | None = None  # set while this process owns the lock

    @property
    def owned(self) -> bool:
        """True if this process believes it holds the lock (see `renew` to check)."""
        return self.token is not None

    def acquire(self) -> bool:
        """Take the lock if nobody holds it. Returns True on success."""
        token = uuid.uuid4().hex
        if self.client.set(self.key, token, nx=True, px=self.ttl_ms):
            self.token = token
            return True
        return False

    def holder(self) -> str | None:
        """The current owner's token, or None if the lock is free."""
        return _text(self.client.get(self.key))

    def renew(self) -> bool:
        """Reset the expiry if this process still owns the lock; False if it was lost."""
        if self.token is None:
            return False
        if self._if_owned(lambda pipe: pipe.pexpire(self.key, self.ttl_ms)):
            return True
        logger.error("Lost the lake writer lock %s", self.key)
        self.token = None
        return False

    def release(self) -> None:
        """Delete the lock if this process still owns it."""
        if self.token is not None:
            self._if_owned(lambda pipe: pipe.delete(self.key))
            self.token = None

    def _if_owned(self, command) -> bool:
        """Run `command` in a transaction only if the key still holds our token."""
        with self.client.pipeline() as pipe:
            try:
                pipe.watch(self.key)
                if _text(pipe.get(self.key)) != self.token:
                    pipe.unwatch()
                    return False
                pipe.multi()
                command(pipe)
                pipe.execute()
                return True
            except redis.WatchError:  # the key changed between the check and the change
                return False


def _text(value: str | bytes | None) -> str | None:
    return value.decode() if isinstance(value, bytes) else value
