"""Heartbeats for the long-running workers, and the command Kubernetes probes run.

A worker (consumer, detector, archiver, enricher, grouper, graph runner) has no HTTP
port, so Kubernetes can't ask it "are you OK?" over the network. Instead the worker
*beats*: every time its main loop goes round, it touches a small file (`HEALTH_FILE`).
The probe is a command run inside the container:

    python -m wikipulse.health --max-age 300

It exits 0 when the file was touched within the last 300 seconds and 1 otherwise. A
worker whose loop hangs (a deadlock, a network call without a timeout) stops beating,
the probe fails, and Kubernetes restarts the container.

The age is wall-clock time, on purpose: the question is "is this process still making
progress *now*?", not anything about the edits it processes (those use event time).

When `HEALTH_FILE` is not set (on a laptop, in tests), beating does nothing.
"""

import argparse
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

from wikipulse.config import Settings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_S = 300.0


class Heartbeat:
    """Touches `path` when beaten, at most once every `min_interval_s` seconds.

    The interval keeps a busy loop (the consumer handles dozens of messages a second)
    from touching the file thousands of times a minute. `path=None` disables it.
    """

    def __init__(
        self,
        path: Path | None,
        min_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._last: float | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "Heartbeat":
        """The heartbeat `HEALTH_FILE` asks for (disabled when it is unset)."""
        return cls(settings.health_file)

    def beat(self) -> None:
        """Record that the worker is making progress. Never raises."""
        if self.path is None:
            return
        now = self._clock()
        if self._last is not None and now - self._last < self.min_interval_s:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()
        except OSError as exc:  # a full or read-only disk must not stop the worker
            logger.warning("Could not touch the heartbeat file %s: %s", self.path, exc)
            return
        self._last = now

    def __call__(self) -> None:
        self.beat()


def beat_while(should_stop: Callable[[], bool], heartbeat: Heartbeat) -> Callable[[], bool]:
    """`should_stop` that also beats the heartbeat.

    The synchronous stream loops (detector, archiver) ask "should I stop?" once per
    iteration and once per entry, so answering that question is the natural place to
    say "still alive".
    """

    def check() -> bool:
        heartbeat.beat()
        return should_stop()

    return check


def heartbeat_age(path: Path, now: Callable[[], float] = time.time) -> float | None:
    """Seconds since the heartbeat file was last touched; None if there is none yet."""
    try:
        return now() - path.stat().st_mtime
    except FileNotFoundError:
        return None


def check(path: Path, max_age_s: float, now: Callable[[], float] = time.time) -> tuple[bool, str]:
    """(healthy, explanation) for the heartbeat file at `path`."""
    age = heartbeat_age(path, now)
    if age is None:
        return False, f"no heartbeat yet ({path} does not exist)"
    if age > max_age_s:
        return False, f"last heartbeat {age:.0f} s ago (more than {max_age_s:.0f} s)"
    return True, f"last heartbeat {max(age, 0):.0f} s ago"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe: is the worker's heartbeat fresh?")
    parser.add_argument("--file", type=Path, help="heartbeat file (default: HEALTH_FILE)")
    parser.add_argument(
        "--max-age",
        type=float,
        default=DEFAULT_MAX_AGE_S,
        help=f"seconds a heartbeat stays fresh (default {DEFAULT_MAX_AGE_S:.0f})",
    )
    args = parser.parse_args(argv)
    path = args.file or get_settings().health_file
    if path is None:
        print("No heartbeat file: set HEALTH_FILE or pass --file", file=sys.stderr)
        return 2
    healthy, explanation = check(path, args.max_age)
    print(explanation)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
