"""Metrics of the detection evaluation, as pure functions (no network, no database).

Time windows. A portal date D stands for the UTC day [D 00:00, D+1 00:00). A spike
stands for its active period [first_edit_at, ended_at] (or `last_edit_at` while it is
still active). The *gap* between the two is 0 if they overlap, otherwise the hours
between the nearer ends. So "a spike within 24 hours of the portal date" means a spike
active at some point between the day before and the day after D.

- Recall: share of (portal day, article) pairs with a spike of that article within
  `RECALL_WINDOW_H`. Over all links, and over the lead links only.
- Portal precision: share of spikes whose title is on some portal day within
  `PRECISION_WINDOW_H`. A *lower bound*: much real news never reaches the portal.
- Label precision: share of labeled spikes labeled `news` (`unsure` left out).
- Time to detection: `started_at - first_edit_at` of the spikes matching the portal.
"""

import math
import random
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from wikipulse.detect.detector import SpikeRecord
from wikipulse.eval.ground_truth import PortalDay

RECALL_WINDOW_H = 24.0
PRECISION_WINDOW_H = 48.0
LABELS = ("news", "not news", "unsure")
# The labeled sample of a run: this many spikes, drawn with this seed.
DEFAULT_SAMPLE_SIZE = 50
DEFAULT_SEED = 2026


def day_start(day: date) -> datetime:
    """Midnight UTC at the start of `day`."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def spike_end(spike: SpikeRecord) -> datetime:
    """The end of a spike's active period (its last edit while it is still active)."""
    return spike.ended_at or spike.last_edit_at


def gap_hours(start: datetime, end: datetime, day: date) -> float:
    """Hours between the period [start, end] and the UTC day `day` (0 if they overlap)."""
    first, after = day_start(day), day_start(day) + timedelta(days=1)
    if end < first:
        return (first - end).total_seconds() / 3600
    if start >= after:
        return (start - after).total_seconds() / 3600
    return 0.0


def spike_gap_hours(spike: SpikeRecord, day: date) -> float:
    """Hours between the spike's active period and `day` (0 if they overlap)."""
    return gap_hours(spike.first_edit_at, spike_end(spike), day)


def ratio(part: int, whole: int) -> float | None:
    """part / whole, or None when there is nothing to divide (shown as "n/a")."""
    return part / whole if whole else None


def by_title(spikes: Iterable[SpikeRecord]) -> dict[str, list[SpikeRecord]]:
    """Spikes grouped by page title."""
    index: dict[str, list[SpikeRecord]] = {}
    for spike in spikes:
        index.setdefault(spike.title, []).append(spike)
    return index


# --- Recall -----------------------------------------------------------------------


@dataclass
class DayRecall:
    """Recall on one portal day: the articles it links and those that had a spike."""

    day: date
    articles: list[str]
    found: list[str]

    @property
    def recall(self) -> float | None:
        return ratio(len(self.found), len(self.articles))


def recall_for_day(
    portal: PortalDay,
    spikes: dict[str, list[SpikeRecord]],
    leads_only: bool = False,
    window_h: float = RECALL_WINDOW_H,
) -> DayRecall:
    """Which of the day's linked articles had a spike within `window_h` of the day."""
    articles = portal.articles(leads_only)
    found = [
        title
        for title in articles
        if any(spike_gap_hours(s, portal.day) <= window_h for s in spikes.get(title, []))
    ]
    return DayRecall(portal.day, articles, found)


def pooled_recall(days: Sequence[DayRecall]) -> float | None:
    """Recall over all (day, article) pairs together."""
    return ratio(sum(len(d.found) for d in days), sum(len(d.articles) for d in days))


# --- Precision against the portal ---------------------------------------------------


@dataclass
class SpikeMatch:
    """A spike and the portal days (within the window) that link to its article."""

    spike: SpikeRecord
    portal_days: list[date]

    @property
    def matched(self) -> bool:
        return bool(self.portal_days)


def match_spikes(
    spikes: Iterable[SpikeRecord],
    portal: Sequence[PortalDay],
    window_h: float = PRECISION_WINDOW_H,
) -> list[SpikeMatch]:
    """Each spike with the portal days that list its page within `window_h` hours."""
    return [
        SpikeMatch(
            spike,
            [
                p.day
                for p in portal
                if p.mentions(spike.title) and spike_gap_hours(spike, p.day) <= window_h
            ],
        )
        for spike in spikes
    ]


def portal_precision(matches: Sequence[SpikeMatch]) -> float | None:
    """Share of spikes found on the portal: a LOWER BOUND on the true precision."""
    return ratio(sum(m.matched for m in matches), len(matches))


# --- Time to detection --------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float | None:
    """The q-th percentile (0-100) with linear interpolation between ranks."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@dataclass
class Distribution:
    count: int
    median: float | None
    p90: float | None
    minimum: float | None
    maximum: float | None


def distribution(values: Sequence[float]) -> Distribution:
    """Count, median, 90th percentile, minimum and maximum of `values`."""
    return Distribution(
        count=len(values),
        median=percentile(values, 50),
        p90=percentile(values, 90),
        minimum=min(values) if values else None,
        maximum=max(values) if values else None,
    )


def detection_delay_minutes(spike: SpikeRecord) -> float:
    """Minutes between the first edit of the burst and the moment the spike started."""
    return (spike.started_at - spike.first_edit_at).total_seconds() / 60


def time_to_detection(matches: Sequence[SpikeMatch]) -> Distribution:
    """Detection delays (minutes) of the spikes the portal lists."""
    return distribution([detection_delay_minutes(m.spike) for m in matches if m.matched])


# --- Precision from labels ----------------------------------------------------------


def sample_spikes(spikes: Sequence[SpikeRecord], size: int, seed: int) -> list[SpikeRecord]:
    """A reproducible random sample: same spikes and seed, same sample, in time order.

    The label tool labels this sample, and the evaluation measures label precision on
    exactly this sample again, so the estimate is over a *random* sample of the run's
    spikes even when some of its labels were made for another run's sample.
    """
    ordered = sorted(spikes, key=lambda s: (s.started_at, s.title))
    chosen = random.Random(seed).sample(ordered, min(size, len(ordered)))
    return sorted(chosen, key=lambda s: (s.started_at, s.title))


@dataclass(frozen=True)
class Label:
    """A judgement on one spike: was it news? `labeler` is "llm" or "human"."""

    title: str
    first_edit_at: datetime
    end_at: datetime
    label: str
    labeler: str
    model: str | None = None

    def covers(self, spike: SpikeRecord) -> bool:
        """Same article and overlapping active periods (works across detector runs)."""
        return (
            self.title == spike.title
            and self.first_edit_at <= spike_end(spike)
            and spike.first_edit_at <= self.end_at
        )


@dataclass
class LabelPrecision:
    labeled: int  # spikes of the run with a label
    counts: dict[str, int] = field(default_factory=dict)  # per label value
    labelers: dict[str, int] = field(default_factory=dict)  # per labeler (llm/human)
    models: dict[str, int] = field(default_factory=dict)

    @property
    def precision(self) -> float | None:
        """news / (news + not news); `unsure` labels are left out."""
        news, not_news = self.counts.get("news", 0), self.counts.get("not news", 0)
        return ratio(news, news + not_news)


def pick_label(labels: Sequence[Label]) -> Label | None:
    """The label that counts: a human label beats an LLM label; later beats earlier."""
    human = [label for label in labels if label.labeler == "human"]
    chosen = human or list(labels)
    return chosen[-1] if chosen else None


def label_precision(spikes: Iterable[SpikeRecord], labels: Sequence[Label]) -> LabelPrecision:
    """Precision on the spikes that have a label (labels in file order, oldest first)."""
    index: dict[str, list[Label]] = {}
    for label in labels:
        index.setdefault(label.title, []).append(label)
    counts: Counter[str] = Counter()
    labelers: Counter[str] = Counter()
    models: Counter[str] = Counter()
    for spike in spikes:
        chosen = pick_label([lb for lb in index.get(spike.title, []) if lb.covers(spike)])
        if chosen is None:
            continue
        counts[chosen.label] += 1
        labelers[chosen.labeler] += 1
        if chosen.model:
            models[chosen.model] += 1
    return LabelPrecision(sum(counts.values()), dict(counts), dict(labelers), dict(models))
