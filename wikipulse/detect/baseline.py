"""Baselines: how much a page is *normally* edited, and the overall edit rate.

Both use an exponentially decaying count, a continuous-time cousin of the exponential
moving average. Every edit adds 1 to a "level", and the level shrinks by a factor
e^(-dt/tau) as time passes, so an edit from one `tau` ago counts 0.37, from two `tau`
ago 0.14, and so on. The rate is the level divided by `tau`: with tau = 24 hours a page
edited 12 times a day settles at a level of about 12, i.e. 0.5 edits per hour.

Why this form:
- **O(1) memory per page**: just (level, time of last update). We never store history.
- **Lazy updates**: the decay is applied only when the page is touched, so idle pages
  cost nothing until they are pruned.
- **Order-independent result**: the level at time T is the sum of e^(-(T - t_i)/tau)
  over all fed edits, whenever they were fed. Replays and restarts agree.

**Cold start.** When the detector has run for less than a few `tau`, a plain decaying
count underestimates the rate (it hasn't seen a full day yet). `_rate` corrects this by
dividing by the *observed* time instead: tau * (1 - e^(-observed/tau)), which is about
`observed` for short runs and `tau` for long ones. A page the detector has never seen
edited still has a baseline of zero; the detector's floor and minimum counts handle it.

**Daily rhythm.** `rhythm()` compares the overall (all pages) human edit rate of the last
hour with the rate over the baseline's time constant. At a busy hour it is above 1, at
night below 1. Multiplying a page's baseline by it means "normal for this time of day".

Times are seconds since 1970 (event time).
"""

import math
from typing import Any

SECONDS_PER_DAY = 86_400


class DecayingCount:
    """An exponentially decaying event count with time constant `tau_s` seconds."""

    def __init__(self, tau_s: float, level: float = 0.0, updated_at: float | None = None) -> None:
        self.tau_s = tau_s
        self.level = level
        self.updated_at = updated_at

    def level_at(self, now: float) -> float:
        """The level decayed to `now` (never grows backwards for out-of-order times)."""
        if self.updated_at is None:
            return 0.0
        return self.level * math.exp(-max(0.0, now - self.updated_at) / self.tau_s)

    def add(self, time: float) -> None:
        """Count one event at `time`."""
        if self.updated_at is None or time >= self.updated_at:
            self.level = self.level_at(time) + 1.0
            self.updated_at = time
        else:  # a late event: add its already-decayed weight
            self.level += math.exp(-(self.updated_at - time) / self.tau_s)

    def rate(self, now: float, observed_s: float) -> float:
        """Events per second, corrected for an observation period of `observed_s`."""
        return _rate(self.level_at(now), self.tau_s, observed_s)

    def to_list(self) -> list[float | None]:
        return [self.level, self.updated_at]

    @classmethod
    def from_list(cls, tau_s: float, data: list[Any]) -> "DecayingCount":
        level, updated_at = data
        return cls(tau_s, float(level), None if updated_at is None else float(updated_at))


def _rate(level: float, tau_s: float, observed_s: float) -> float:
    """Level to events per second, dividing by the effective observed time (cold start)."""
    if observed_s <= 0:
        return 0.0
    return level / (tau_s * -math.expm1(-observed_s / tau_s))


class Baselines:
    """Per-page baselines plus the overall edit rate used for the daily rhythm.

    `page_delay_s` is the long window: pages are fed edits that left it, so per-page
    observation starts that much after the first event.
    """

    def __init__(
        self,
        tau_s: float,
        rhythm_tau_s: float,
        page_delay_s: float,
        rhythm_min: float = 0.5,
        rhythm_max: float = 2.0,
    ) -> None:
        self.tau_s = tau_s
        self.rhythm_tau_s = rhythm_tau_s
        self.page_delay_s = page_delay_s
        self.rhythm_min = rhythm_min
        self.rhythm_max = rhythm_max
        self.started_at: float | None = None  # event time of the first edit seen
        self.overall_long = DecayingCount(tau_s)
        self.overall_short = DecayingCount(rhythm_tau_s)
        self.pages: dict[str, DecayingCount] = {}
        self.changed: set[str] = set()  # pages updated since the last snapshot
        self.removed: set[str] = set()  # pages pruned since the last snapshot

    def record_overall_edit(self, time: float) -> None:
        """Count one human edit (any page) in the overall rate."""
        if self.started_at is None or time < self.started_at:
            self.started_at = time
        self.overall_long.add(time)
        self.overall_short.add(time)

    def record_page_edit(self, title: str, time: float) -> None:
        """Feed one edit (that has left the long window) into the page's baseline."""
        self.pages.setdefault(title, DecayingCount(self.tau_s)).add(time)
        self.changed.add(title)
        self.removed.discard(title)

    def page_rate(self, title: str, now: float) -> float:
        """The page's normal edit rate (edits per second) at `now`, before the rhythm."""
        count = self.pages.get(title)
        if count is None or self.started_at is None:
            return 0.0
        observed = now - self.started_at - self.page_delay_s
        # Until a full long window of history has been fed, assume one window was seen,
        # so a single early edit isn't read as a huge rate.
        return count.rate(now, max(observed, self.page_delay_s))

    def rhythm(self, now: float) -> float:
        """Overall edit rate now / overall rate over the baseline period, clamped."""
        if self.started_at is None:
            return 1.0
        observed = now - self.started_at
        long_rate = self.overall_long.rate(now, observed)
        if long_rate <= 0:
            return 1.0
        ratio = self.overall_short.rate(now, observed) / long_rate
        return min(self.rhythm_max, max(self.rhythm_min, ratio))

    def expected(self, title: str, now: float, window_s: float) -> float:
        """Edits this page normally gets in a window of `window_s` seconds at this hour."""
        return self.page_rate(title, now) * window_s * self.rhythm(now)

    def prune(self, now: float, min_per_day: float) -> int:
        """Forget pages whose (uncorrected) rate fell below `min_per_day`. Returns how many."""
        threshold = min_per_day * self.tau_s / SECONDS_PER_DAY  # as a level
        gone = [title for title, count in self.pages.items() if count.level_at(now) < threshold]
        for title in gone:
            del self.pages[title]
            self.changed.discard(title)
            self.removed.add(title)
        return len(gone)

    def overall_to_dict(self) -> dict[str, Any]:
        """Everything except the pages (those are snapshotted one by one)."""
        return {
            "started_at": self.started_at,
            "overall_long": self.overall_long.to_list(),
            "overall_short": self.overall_short.to_list(),
        }

    def load_overall(self, data: dict[str, Any]) -> None:
        self.started_at = data["started_at"]
        self.overall_long = DecayingCount.from_list(self.tau_s, data["overall_long"])
        self.overall_short = DecayingCount.from_list(self.rhythm_tau_s, data["overall_short"])

    def load_pages(self, pages: dict[str, list[Any]]) -> None:
        self.pages = {title: DecayingCount.from_list(self.tau_s, v) for title, v in pages.items()}
        self.changed.clear()
        self.removed.clear()
