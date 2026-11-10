"""How far behind the stream writer is: the `wikipulse_consumer_lag_seconds` metric.

Lag = "now" minus the event time of the newest edit written to the stream. A live
consumer that keeps up has a lag of a few seconds; one that is stuck or reconnecting sees
its lag grow by one second per second, which is exactly the symptom users would feel
(the page shows old news). Prometheus reads the value at scrape time, so a writer that
stopped receiving still reports a growing lag.

"Now" depends on the writer:

- `WallClockLag`, the live consumer: the wall clock. This is monitoring, not processing
  logic, so the wall clock is right here (the detector never sees this value).
- `ReplayLag`, a replay: the replay's own clock in event time. A replay of a recording
  from last week is not a week behind; it is behind if it writes more slowly than its
  speed asks. Its clock starts at the first edit's event time and runs `speed` times
  faster than the wall clock (exactly how `FileSource` paces the messages).
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime


class WallClockLag:
    """Lag of the live consumer: wall clock minus the newest written event time."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self.last_event: datetime | None = None

    def observe(self, event_time: datetime) -> None:
        """Called for every edit written to the stream."""
        if self.last_event is None or event_time > self.last_event:
            self.last_event = event_time

    def seconds(self) -> float:
        """The lag in seconds; NaN before the first edit (Prometheus shows no value)."""
        if self.last_event is None:
            return float("nan")
        return (self._now() - self.last_event).total_seconds()


class ReplayLag(WallClockLag):
    """Lag of a replay against its own event-time clock.

    `speed=None` (as fast as possible) has no clock to fall behind: the lag is 0.
    """

    def __init__(self, speed: float | None, monotonic: Callable[[], float] = time.monotonic):
        super().__init__()
        self.speed = speed
        self._monotonic = monotonic
        self._first: datetime | None = None
        self._started = 0.0

    def observe(self, event_time: datetime) -> None:
        if self._first is None:
            self._first, self._started = event_time, self._monotonic()
        super().observe(event_time)

    def seconds(self) -> float:
        if self.last_event is None or self._first is None:
            return float("nan")
        if self.speed is None:
            return 0.0
        elapsed = (self._monotonic() - self._started) * self.speed
        replay_now = self._first.timestamp() + elapsed
        return max(0.0, replay_now - self.last_event.timestamp())
