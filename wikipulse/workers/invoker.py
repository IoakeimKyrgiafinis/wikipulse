"""Invoking a handler: retries with backoff, then the dead-letter list.

This plays the part AWS Lambda plays for an S3-triggered function: it calls the handler
with one event, retries a failed call a few times (waiting longer each time), and when
every attempt has failed it records the event and the error in a *dead-letter list*
instead of retrying forever or dropping it silently. A person (or `run.py redrive`)
can inspect the list later and send the events through again once the cause is fixed.

The dead-letter list is a Redis list of JSON records, newest first (LPUSH):

    {"handler": "hourly_stats", "event": {"bucket": ..., "key": ..., ...},
     "attempts": 3, "error": "ArrowInvalid: ...", "failed_at": "2026-11-09T16:02:11+00:00"}
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import redis

from wikipulse.workers.notifications import ObjectRef

logger = logging.getLogger(__name__)

Handler = Callable[[ObjectRef], Any]


@dataclass(frozen=True)
class DeadLetter:
    handler: str
    event: ObjectRef
    attempts: int
    error: str
    failed_at: str  # wall clock, ISO 8601: when the last attempt failed

    def to_json(self) -> str:
        return json.dumps(
            {
                "handler": self.handler,
                "event": self.event.to_dict(),
                "attempts": self.attempts,
                "error": self.error,
                "failed_at": self.failed_at,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str) -> "DeadLetter":
        data = json.loads(text)
        return cls(
            handler=data["handler"],
            event=ObjectRef.from_dict(data["event"]),
            attempts=int(data["attempts"]),
            error=data["error"],
            failed_at=data["failed_at"],
        )


@dataclass
class InvokeStats:
    invoked: int = 0
    succeeded: int = 0
    retries: int = 0
    dead_lettered: int = 0


class Invoker:
    """Calls `handler(event)` with up to `max_attempts` attempts, then dead-letters."""

    def __init__(
        self,
        handler: Handler,
        client: redis.Redis,
        dead_letter_key: str,
        *,
        name: str = "hourly_stats",
        max_attempts: int = 3,
        retry_delay_s: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.handler = handler
        self.client = client
        self.dead_letter_key = dead_letter_key
        self.name = name
        self.max_attempts = max_attempts
        self.retry_delay_s = retry_delay_s
        self._sleep = sleep
        self.stats = InvokeStats()

    def invoke(self, event: ObjectRef) -> bool:
        """Run the handler for `event`. True on success, False if it was dead-lettered."""
        self.stats.invoked += 1
        delay = self.retry_delay_s
        for attempt in range(1, self.max_attempts + 1):
            try:
                self.handler(event)
            except Exception as exc:  # any failure of the handler counts
                error = f"{type(exc).__name__}: {exc}"
                if attempt == self.max_attempts:
                    self._dead_letter(event, attempt, error)
                    return False
                logger.warning(
                    "%s failed for %s/%s (attempt %d of %d): %s; retrying in %.1f s",
                    self.name,
                    event.bucket,
                    event.key,
                    attempt,
                    self.max_attempts,
                    error,
                    delay,
                )
                self.stats.retries += 1
                self._sleep(delay)
                delay *= 2
            else:
                self.stats.succeeded += 1
                return True
        return False  # not reached: the loop returns on the last attempt

    def _dead_letter(self, event: ObjectRef, attempts: int, error: str) -> None:
        letter = DeadLetter(
            handler=self.name,
            event=event,
            attempts=attempts,
            error=error,
            failed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self.client.lpush(self.dead_letter_key, letter.to_json())
        self.stats.dead_lettered += 1
        logger.error(
            "%s gave up on %s/%s after %d attempts (%s); added to dead-letter list %s",
            self.name,
            event.bucket,
            event.key,
            attempts,
            error,
            self.dead_letter_key,
        )


def dead_letters(client: redis.Redis, key: str) -> list[DeadLetter]:
    """Every record in the dead-letter list, newest first."""
    return [DeadLetter.from_json(text) for text in client.lrange(key, 0, -1)]


def redrive(invoker: Invoker) -> tuple[int, int]:
    """Send every dead-lettered event through the handler again (oldest first).

    A record is taken off the old end of the list before its retry; if the retry fails
    too, the invoker pushes a fresh record onto the new end. Only the records present at
    the start are retried, so a failing event is tried once per redrive, not forever.
    Returns (succeeded, failed again).
    """
    succeeded = failed = 0
    for _ in range(invoker.client.llen(invoker.dead_letter_key)):
        text = invoker.client.rpop(invoker.dead_letter_key)  # the oldest
        if text is None:
            break
        if invoker.invoke(DeadLetter.from_json(text).event):
            succeeded += 1
        else:
            failed += 1
    return succeeded, failed
