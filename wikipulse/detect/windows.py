"""Sliding-window edit counts per page, in event time.

For every page we keep the (time, user) of its recent human edits, sorted by time, for
as long as the *long* window (60 minutes by default). From that list we can answer
"how many edits, and from how many different people, in the last 10 (or 60) minutes?"
at any moment.

Times are plain floats: seconds since 1970 in UTC, taken from each event's own
timestamp (event time). Nothing here reads the clock.

When an edit becomes older than the long window it is *evicted* and handed back to the
caller, which feeds it into the page's baseline (see `baseline.py`). So the baseline
learns from an edit only an hour after it happened, and a burst can't raise its own
"normal" level while it is still going on.
"""

from bisect import bisect_right, insort
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

Edit = tuple[float, str]  # (event time in seconds, user name)


@dataclass(frozen=True)
class WindowCounts:
    """Activity of one page in the short and the long window, ending at `now`."""

    edits_short: int
    editors_short: int
    edits_long: int
    editors_long: int
    first_edit_short: float | None  # time of the oldest edit in the short window
    first_edit_long: float | None  # time of the oldest edit in the long window


def _first_after(edits: list[Edit], start: float) -> int:
    """Index of the first edit strictly after `start`. A window ending at `now` is
    the half-open interval (now - length, now]."""
    return bisect_right(edits, start, key=lambda edit: edit[0])


class PageWindows:
    """Recent edits of every page, for sliding-window counts."""

    def __init__(self, short_s: float, long_s: float) -> None:
        if short_s > long_s:
            raise ValueError("the short window can't be longer than the long window")
        self.short_s = short_s
        self.long_s = long_s
        self._edits: dict[str, list[Edit]] = {}

    def __len__(self) -> int:
        """Number of pages that have at least one edit in the long window."""
        return len(self._edits)

    def add(self, title: str, time: float, user: str) -> None:
        """Remember one edit. Slightly late events are inserted in time order."""
        insort(self._edits.setdefault(title, []), (time, user))

    def evict(self, title: str, now: float) -> list[float]:
        """Drop the page's edits older than the long window; return their times."""
        edits = self._edits.get(title)
        if not edits:
            return []
        cut = _first_after(edits, now - self.long_s)  # edits at or before the window start
        old = [time for time, _ in edits[:cut]]
        del edits[:cut]
        if not edits:
            del self._edits[title]
        return old

    def evict_all(self, now: float) -> Iterator[tuple[str, float]]:
        """Evict old edits of every page (in title order, so it's deterministic)."""
        for title in sorted(self._edits):
            for time in self.evict(title, now):
                yield title, time

    def counts(self, title: str, now: float) -> WindowCounts:
        """Edits and distinct editors in the short and long windows ending at `now`."""
        edits = self._edits.get(title, [])
        long_part = edits[_first_after(edits, now - self.long_s) :]
        short_part = edits[_first_after(edits, now - self.short_s) :]
        return WindowCounts(
            edits_short=len(short_part),
            editors_short=len({user for _, user in short_part}),
            edits_long=len(long_part),
            editors_long=len({user for _, user in long_part}),
            first_edit_short=short_part[0][0] if short_part else None,
            first_edit_long=long_part[0][0] if long_part else None,
        )

    def editors(self, title: str, now: float, window_s: float) -> set[str]:
        """The distinct users who edited the page in the `window_s` seconds up to `now`."""
        edits = self._edits.get(title, [])
        return {user for _, user in edits[_first_after(edits, now - window_s) :]}

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready copy of the state (for snapshots)."""
        return {
            title: [[time, user] for time, user in edits] for title, edits in self._edits.items()
        }

    def load_dict(self, data: dict[str, Any]) -> None:
        """Replace the state with a snapshot made by `to_dict`."""
        self._edits = {
            title: [(float(time), str(user)) for time, user in edits]
            for title, edits in data.items()
        }
