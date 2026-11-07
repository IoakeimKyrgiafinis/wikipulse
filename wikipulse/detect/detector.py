"""The spike detector: decides when a spike starts, continues and ends.

`SpikeDetector.process(event)` takes one edit at a time and updates
- the sliding windows (`windows.py`) with the human edits of each page,
- the baselines (`baseline.py`) with edits that left the long window,
- the spike lifecycle of the edited page.

Lifecycle of a spike (one per page at a time):

    no spike --start rule--> ACTIVE --(edits, while hot)--> ACTIVE
                              |
                              +--no hot moment for `quiet_period_min`--> ENDED

- **Start rules.** Short window (10 minutes by default): at least `min_edits` human
  edits from at least `min_editors` different people. Or long window (60 minutes): at
  least `long_min_edits` edits from `long_min_editors` people, for stories that build up
  more slowly. Either way the count must also be at least `min_ratio` times what the
  page's baseline expects in that window at this hour. A page without history expects
  zero, so then only the minimum counts decide (cold start).
- **Hot**: an edit after which the short window still holds `continue_min_edits` edits.
  Starting needs much more than staying active (hysteresis), so one burst that briefly
  slows down stays one spike.
- **End**: no hot moment for `quiet_period_min` minutes. `ended_at` is the spike's last
  counted edit.

The **score** is a Poisson z-score: (edits - expected) / sqrt(expected), with `expected`
raised to at least `expected_floor` per short window. If edits arrived at random at the
normal rate, a z of 3 or more would be rare. The spike keeps the highest score it
reached in either window.

Everything runs on event time: "now" is the *watermark*, the newest event time seen.
Bot edits only move the watermark. Reverts (by their edit comment) don't count toward
detection unless `count_reverts` is set; a running spike counts them in `revert_edits`.
Same events in the same order always give the same spikes.
"""

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from wikipulse.config import DetectorConfig
from wikipulse.detect.baseline import Baselines
from wikipulse.detect.windows import PageWindows, WindowCounts
from wikipulse.stream.models import EditEvent

logger = logging.getLogger(__name__)

SWEEP_EVERY_S = 60  # evict old edits and end quiet spikes once per minute of event time
PRUNE_EVERY_S = 3600  # forget near-zero baselines once per hour of event time

# Edit summaries of reverts: "Reverted edits by X", "Reverted 1 edit by X",
# "Undid revision 123 by X", "Restored revision 123", "rv vandalism", "rvv".
REVERT_PATTERN = re.compile(
    r"^\s*(reverted|undid|undo|revert|restored revision|rvv?)\b", re.IGNORECASE
)


def is_revert(comment: str) -> bool:
    """True if the edit summary says this edit undoes another one."""
    return REVERT_PATTERN.search(comment) is not None


def to_datetime(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


@dataclass(frozen=True)
class SpikeRecord:
    """A spike as stored in Postgres. `ended_at` is None while the spike is active."""

    title: str
    status: str  # "active" or "ended"
    started_at: datetime  # when the start rule first held (event time)
    first_edit_at: datetime  # oldest edit of the burst (in the short window at the start)
    last_edit_at: datetime
    ended_at: datetime | None
    total_edits: int  # human, non-revert edits from first_edit_at to the end
    distinct_editors: int
    revert_edits: int
    peak_edits_10m: int  # most edits seen in one short window (10 minutes by default)
    score: float  # highest Poisson z-score during the spike


@dataclass
class Spike:
    """A spike being tracked (mutable). Times are seconds since 1970."""

    title: str
    started_at: float
    first_edit_at: float
    last_edit_at: float
    last_hot_at: float
    total_edits: int
    editors: set[str]
    peak_edits_10m: int
    score: float
    revert_edits: int = 0
    ended_at: float | None = None

    @property
    def key(self) -> tuple[str, float]:
        return (self.title, self.started_at)

    def record(self) -> SpikeRecord:
        return SpikeRecord(
            title=self.title,
            status="active" if self.ended_at is None else "ended",
            started_at=to_datetime(self.started_at),
            first_edit_at=to_datetime(self.first_edit_at),
            last_edit_at=to_datetime(self.last_edit_at),
            ended_at=None if self.ended_at is None else to_datetime(self.ended_at),
            total_edits=self.total_edits,
            distinct_editors=len(self.editors),
            revert_edits=self.revert_edits,
            peak_edits_10m=self.peak_edits_10m,
            score=self.score,
        )

    def to_dict(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["editors"] = sorted(self.editors)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Spike":
        return cls(**{**data, "editors": set(data["editors"])})


def poisson_z(observed: int, expected: float) -> float:
    """How many standard deviations `observed` is above a Poisson mean of `expected`."""
    return (observed - expected) / math.sqrt(expected)


@dataclass
class _Clock:
    """Event-time bookkeeping that must survive restarts."""

    watermark: float | None = None
    last_sweep: int | None = None  # index of the last minute that was swept
    last_prune: int | None = None  # index of the last hour that was pruned
    stats: dict[str, int] = field(
        default_factory=lambda: {"events": 0, "bots": 0, "reverts": 0, "late": 0}
    )


class SpikeDetector:
    """Turns a sequence of `EditEvent`s into spikes. Pure logic: no I/O, no clock."""

    def __init__(self, config: DetectorConfig) -> None:
        self.config = config
        self.short_s = config.short_window_min * 60
        self.long_s = config.long_window_min * 60
        self.windows = PageWindows(self.short_s, self.long_s)
        self.baselines = Baselines(
            tau_s=config.baseline_tau_hours * 3600,
            rhythm_tau_s=config.rhythm_tau_hours * 3600,
            page_delay_s=self.long_s,
            rhythm_min=config.rhythm_min,
            rhythm_max=config.rhythm_max,
        )
        self.active: dict[str, Spike] = {}
        self.clock = _Clock()
        self._changed: dict[tuple[str, float], Spike] = {}

    @property
    def now(self) -> float | None:
        """The watermark: the newest event time seen so far."""
        return self.clock.watermark

    def process(self, event: EditEvent) -> None:
        """Handle one edit."""
        time = event.event_time.timestamp()
        self.clock.stats["events"] += 1
        self._advance(time)
        if event.is_bot:
            self.clock.stats["bots"] += 1
            return
        now = self.clock.watermark
        assert now is not None
        self._end_if_quiet(event.title, now)
        if not self.config.count_reverts and is_revert(event.comment):
            self._count_revert(event.title)
            return
        self.baselines.record_overall_edit(time)
        self._feed_baseline(event.title, now)
        if time <= now - self.long_s:  # too late for any window: straight to the baseline
            self.clock.stats["late"] += 1
            self.baselines.record_page_edit(event.title, time)
            return
        self.windows.add(event.title, time, event.user)
        counts = self.windows.counts(event.title, now)
        spike = self.active.get(event.title)
        if spike is None:
            self._maybe_start(event.title, now, counts)
        else:
            self._continue(spike, time, event.user, now, counts)

    def pop_changes(self) -> list[SpikeRecord]:
        """Spikes started, updated or ended since the last call, oldest first."""
        changed = sorted(self._changed.values(), key=lambda s: (s.started_at, s.title))
        self._changed.clear()
        return [spike.record() for spike in changed]

    def active_spikes(self) -> list[SpikeRecord]:
        """The currently active spikes, highest score first."""
        spikes = sorted(self.active.values(), key=lambda s: (-s.score, s.title))
        return [spike.record() for spike in spikes]

    # --- lifecycle ---

    def _check(
        self, title: str, now: float, window_s: float, edits: int, editors: int, rule: tuple
    ) -> tuple[bool, float, float]:
        """Test one start rule on one window: (rule holds, expected edits, z-score).

        The ratio test uses the baseline as it is (zero for a page without history, so
        then only the minimum counts decide: cold start). The z-score uses the expected
        count raised to the floor, so it stays finite.
        """
        min_edits, min_editors = rule
        expected = self.baselines.expected(title, now, window_s)
        floor = self.config.expected_floor * window_s / self.short_s
        score = poisson_z(edits, max(expected, floor))
        holds = (
            edits >= min_edits
            and editors >= min_editors
            and edits >= self.config.min_ratio * expected
        )
        return holds, expected, score

    def _evaluate(self, title: str, now: float, counts: WindowCounts) -> dict[str, tuple]:
        c = self.config
        return {
            "short": self._check(
                title,
                now,
                self.short_s,
                counts.edits_short,
                counts.editors_short,
                (c.min_edits, c.min_editors),
            ),
            "long": self._check(
                title,
                now,
                self.long_s,
                counts.edits_long,
                counts.editors_long,
                (c.long_min_edits, c.long_min_editors),
            ),
        }

    def _maybe_start(self, title: str, now: float, counts: WindowCounts) -> None:
        checks = self._evaluate(title, now, counts)
        if checks["short"][0]:
            window, window_s, edits, first = (
                "short",
                self.short_s,
                counts.edits_short,
                counts.first_edit_short,
            )
        elif checks["long"][0]:
            window, window_s, edits, first = (
                "long",
                self.long_s,
                counts.edits_long,
                counts.first_edit_long,
            )
        else:
            return
        assert first is not None
        editors = self.windows.editors(title, now, window_s)
        spike = Spike(
            title=title,
            started_at=now,
            first_edit_at=first,
            last_edit_at=now,
            last_hot_at=now,
            total_edits=edits,
            editors=editors,
            peak_edits_10m=counts.edits_short,
            score=max(checks["short"][2], checks["long"][2]),
        )
        self.active[title] = spike
        self._changed[spike.key] = spike
        logger.info(
            "Spike started (%s window): %s (%d edits, %d editors, expected %.2f, score %.1f)",
            window,
            title,
            edits,
            len(editors),
            checks[window][1],
            spike.score,
        )

    def _continue(
        self, spike: Spike, time: float, user: str, now: float, counts: WindowCounts
    ) -> None:
        spike.total_edits += 1
        spike.editors.add(user)
        spike.last_edit_at = max(spike.last_edit_at, time)
        if counts.edits_short >= self.config.continue_min_edits:
            spike.last_hot_at = now
        spike.peak_edits_10m = max(spike.peak_edits_10m, counts.edits_short)
        checks = self._evaluate(spike.title, now, counts)
        spike.score = max(spike.score, checks["short"][2], checks["long"][2])
        self._changed[spike.key] = spike

    def _count_revert(self, title: str) -> None:
        self.clock.stats["reverts"] += 1
        spike = self.active.get(title)
        if spike is not None:
            spike.revert_edits += 1
            self._changed[spike.key] = spike

    def _end_if_quiet(self, title: str, now: float) -> None:
        spike = self.active.get(title)
        if spike is not None and now - spike.last_hot_at >= self.config.quiet_period_min * 60:
            spike.ended_at = spike.last_edit_at
            del self.active[title]
            self._changed[spike.key] = spike
            logger.info(
                "Spike ended: %s (%d edits, %d editors, peak %d, score %.1f)",
                title,
                spike.total_edits,
                len(spike.editors),
                spike.peak_edits_10m,
                spike.score,
            )

    # --- event time housekeeping ---

    def _advance(self, time: float) -> None:
        """Move the watermark forward; once per new minute, sweep; once per hour, prune."""
        clock = self.clock
        if clock.watermark is None or time > clock.watermark:
            clock.watermark = time
        minute = int(clock.watermark // SWEEP_EVERY_S)
        if minute != clock.last_sweep:
            clock.last_sweep = minute
            self._sweep(clock.watermark)
        hour = int(clock.watermark // PRUNE_EVERY_S)
        if hour != clock.last_prune:
            clock.last_prune = hour
            pruned = self.baselines.prune(clock.watermark, self.config.baseline_prune_per_day)
            logger.debug("Pruned %d idle page baselines", pruned)

    def _sweep(self, now: float) -> None:
        for title, time in self.windows.evict_all(now):
            self.baselines.record_page_edit(title, time)
        for title in sorted(self.active):
            self._end_if_quiet(title, now)

    def _feed_baseline(self, title: str, now: float) -> None:
        for time in self.windows.evict(title, now):
            self.baselines.record_page_edit(title, time)

    # --- snapshots ---

    def state_to_dict(self) -> dict[str, Any]:
        """Everything except per-page baselines, as JSON-ready data."""
        return {
            "watermark": self.clock.watermark,
            "last_sweep": self.clock.last_sweep,
            "last_prune": self.clock.last_prune,
            "stats": self.clock.stats,
            "windows": self.windows.to_dict(),
            "baselines": self.baselines.overall_to_dict(),
            "active": [spike.to_dict() for spike in self.active.values()],
        }

    def load_state(self, data: dict[str, Any], pages: dict[str, list[Any]]) -> None:
        """Restore a snapshot made by `state_to_dict` plus the per-page baselines."""
        self.clock = _Clock(
            watermark=data["watermark"],
            last_sweep=data["last_sweep"],
            last_prune=data["last_prune"],
            stats=dict(data["stats"]),
        )
        self.windows.load_dict(data["windows"])
        self.baselines.load_overall(data["baselines"])
        self.baselines.load_pages(pages)
        self.active = {s["title"]: Spike.from_dict(s) for s in data["active"]}
        self._changed.clear()
