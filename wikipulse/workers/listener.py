"""The trigger: listen for lake notifications and invoke the handler for each one.

The object store PUBLISHes every notification on a Redis pub/sub channel. Pub/sub is
fire-and-forget: a message reaches the subscribers connected at that moment and is
then gone. So the listener

1. subscribes first,
2. then runs a catch-up (the backfill) for everything written while nobody listened,
3. then handles messages as they arrive.

Subscribing before the catch-up means a file written *during* the catch-up is not lost
either: its message waits in the subscription. Handling it twice is harmless, because
the handler is idempotent. After a lost Redis connection the listener subscribes again
and repeats the catch-up, for the same reason.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import redis

from wikipulse.workers.invoker import Invoker
from wikipulse.workers.notifications import parse_message

logger = logging.getLogger(__name__)

MAX_RECONNECT_DELAY_S = 30.0


@dataclass
class ListenStats:
    messages: int = 0
    events: int = 0  # object-created events for our bucket
    ignored: int = 0  # events for other buckets
    malformed: int = 0
    reconnects: int = 0


class NotificationListener:
    """Subscribes to `channel` and invokes the handler for every event of `bucket`."""

    def __init__(
        self,
        client: redis.Redis,
        channel: str,
        invoker: Invoker,
        bucket: str,
        catch_up: Callable[[], object] | None = None,
        poll_timeout_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.channel = channel
        self.invoker = invoker
        self.bucket = bucket
        self.catch_up = catch_up
        self.poll_timeout_s = poll_timeout_s
        self._sleep = sleep
        self.stats = ListenStats()

    def run(
        self,
        should_stop: Callable[[], bool] = lambda: False,
        max_events: int | None = None,
        idle_exit_s: float | None = None,
    ) -> ListenStats:
        """Listen until `should_stop()`, `max_events` events, or `idle_exit_s` of silence."""
        delay = 1.0
        while not should_stop():
            pubsub = self.client.pubsub(ignore_subscribe_messages=True)
            try:
                pubsub.subscribe(self.channel)
                logger.info(
                    "Listening on Redis channel %s for bucket %s", self.channel, self.bucket
                )
                if self.catch_up is not None:
                    self.catch_up()
                delay = 1.0
                self._loop(pubsub, should_stop, max_events, idle_exit_s)
                return self.stats
            except redis.ConnectionError as exc:
                self.stats.reconnects += 1
                logger.warning("Lost Redis (%s); subscribing again in %.0f s", exc, delay)
                self._sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY_S)
            finally:
                try:
                    pubsub.close()
                except redis.RedisError:
                    pass
        return self.stats

    def _loop(self, pubsub, should_stop, max_events, idle_exit_s) -> None:
        idle_since = time.monotonic()
        while not should_stop():
            if max_events is not None and self.stats.events >= max_events:
                return
            message = pubsub.get_message(timeout=self.poll_timeout_s)
            if message is None or message.get("type") != "message":
                if idle_exit_s is not None and time.monotonic() - idle_since >= idle_exit_s:
                    logger.info("No notifications for %.0f s, stopping", idle_exit_s)
                    return
                continue
            idle_since = time.monotonic()
            self.handle_message(message["data"])

    def handle_message(self, data: str | bytes) -> None:
        """Invoke the handler for every object-created event of our bucket in a message."""
        self.stats.messages += 1
        try:
            events = parse_message(data)
        except ValueError as exc:
            self.stats.malformed += 1
            logger.warning("Ignoring a malformed notification (%s): %.200r", exc, data)
            return
        for event in events:
            if event.bucket != self.bucket:
                self.stats.ignored += 1
                logger.debug("Ignoring an event of bucket %s", event.bucket)
                continue
            self.stats.events += 1
            self.invoker.invoke(event)
