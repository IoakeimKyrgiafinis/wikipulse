"""Online event grouping: each new spike joins an active event or starts a new one.

    spike arrives ──► close events whose spikes are all over (event time)
                  ──► score it against every page of every active event
                        (an event's score = its best page: single linkage)
                  ──► no event reaches EVENTS_JOIN_THRESHOLD:  new event
                      one event reaches it:                     join it
                      several reach it:                         merge them, then join

Merging is what makes this *online connected components*: if spike C is similar to
event 1 (via A) and to event 2 (via B), then A, B and C are one story, so the two events
become one. The oldest event (lowest number) survives; the other is marked `merged`
with `merged_into` pointing at the survivor.

- An event ends when all its spikes have ended. A spike counts as over
  `spike_close_min` minutes after its last edit (when the detector declares it ended),
  so a replay closes events at the same moments as a live run.
- An event's provisional title is its highest-scoring page (by spike score) until
  stage 4 writes better titles.
- Everything uses event time (spike timestamps). The same spikes in the same order
  always give the same events.
- An event's correlation id (spec 09b) is the one of its earliest spike: the edit that
  started the story. A merge keeps the survivor's, which started earlier.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from wikipulse.config import EventsConfig
from wikipulse.events.similarity import SpikePage, pair_score

logger = logging.getLogger(__name__)

ACTIVE = "active"
ENDED = "ended"
MERGED = "merged"


@dataclass(eq=False)
class Member:
    """A spike in an event, with the score it joined with (1.0 for the first)."""

    page: SpikePage
    joined_score: float


@dataclass(eq=False)
class Event:
    """A group of spikes about one story."""

    number: int  # 1, 2, 3, ... per grouping run, in order of creation
    members: list[Member] = field(default_factory=list)
    status: str = ACTIVE
    merged_into: int | None = None
    ended_at: datetime | None = None
    # Summary of the members, kept as of the last change (`refresh()`), so a merged
    # event (which hands its members to the survivor) still shows what it was.
    title: str = ""  # provisional title: the canonical title of the highest-scoring page
    page_count: int = 0
    started_at: datetime | None = None  # earliest spike start
    last_spike_at: datetime | None = None  # latest spike start
    score: float = 0.0  # highest spike score
    correlation_id: str | None = None  # the earliest spike's (spec 09b)

    @property
    def pages(self) -> list[SpikePage]:
        return [member.page for member in self.members]

    def refresh(self) -> None:
        """Recompute the summary fields from the members."""
        pages = self.pages
        top = max(pages, key=lambda p: (p.score, -p.started_at.timestamp(), p.title))
        self.title = top.canonical_title
        self.page_count = len(pages)
        self.started_at = min(p.started_at for p in pages)
        self.last_spike_at = max(p.started_at for p in pages)
        self.score = top.score
        first = min(pages, key=lambda p: (p.started_at, p.spike_id))
        self.correlation_id = first.correlation_id

    def is_over(self, now: datetime, close_after: timedelta) -> bool:
        """True if every spike of the event has ended and is past its close time."""
        return all(p.ended_at is not None and p.ended_at + close_after <= now for p in self.pages)


@dataclass(frozen=True)
class Assignment:
    """What happened to one spike."""

    spike_id: int
    event_number: int
    score: float  # best pair score with the event it joined (1.0 for a new event)
    created: bool  # True if the spike started a new event
    merged: tuple[int, ...] = ()  # events merged into `event_number` because of it


class Grouper:
    """Keeps the active events in memory and assigns spikes to them, one at a time."""

    def __init__(
        self,
        config: EventsConfig,
        next_number: int = 1,
        events: list[Event] | None = None,
        clock: datetime | None = None,
    ) -> None:
        self.config = config
        self.close_after = timedelta(minutes=config.spike_close_min)
        self.next_number = next_number
        self.clock = clock
        self.active: dict[int, Event] = {}
        self.event_of: dict[int, int] = {}  # spike id -> number of its active event
        self.changed: dict[int, Event] = {}  # events changed since `take_changes()`
        for event in sorted(events or [], key=lambda e: e.number):
            self.active[event.number] = event
            for page in event.pages:
                self.event_of[page.spike_id] = event.number

    # --- time ---------------------------------------------------------------------

    def advance(self, now: datetime) -> list[Event]:
        """Move the clock to `now` (never backwards); end events that are over."""
        if self.clock is None or now > self.clock:
            self.clock = now
        ended = [e for e in self.active.values() if e.is_over(self.clock, self.close_after)]
        for event in ended:
            event.status = ENDED
            event.ended_at = max(p.ended_at for p in event.pages if p.ended_at is not None)
            self._forget(event)
            logger.info(
                "Event %d ended: %s",
                event.number,
                event.title,
                extra={"correlation_id": event.correlation_id, "event_number": event.number},
            )
        return ended

    def update_end(self, spike_id: int, ended_at: datetime | None) -> None:
        """A spike of an active event has (or no longer has) an end time."""
        number = self.event_of.get(spike_id)
        if number is None:
            return
        for page in self.active[number].pages:
            if page.spike_id == spike_id and page.ended_at != ended_at:
                page.ended_at = ended_at
                self.changed[number] = self.active[number]

    # --- assignment ---------------------------------------------------------------

    def best_score(self, page: SpikePage, event: Event) -> float:
        """Single linkage: the best pair score between the spike and the event's pages."""
        return max(pair_score(page, other, self.config).total for other in event.pages)

    def candidates(self, page: SpikePage) -> list[tuple[int, float]]:
        """Active events the spike could join, as (number, score), oldest first."""
        found = []
        for number, event in self.active.items():
            score = self.best_score(page, event)
            if score >= self.config.join_threshold:
                found.append((number, score))
        return sorted(found)

    def add(self, page: SpikePage) -> Assignment:
        """Assign a new spike: join, merge or start an event."""
        if page.spike_id in self.event_of:
            raise ValueError(f"spike {page.spike_id} is already in an event")
        self.advance(page.started_at)
        found = self.candidates(page)
        if not found:
            event = Event(self.next_number)
            self.next_number += 1
            self.active[event.number] = event
            self._join(event, page, 1.0)
            return Assignment(page.spike_id, event.number, 1.0, created=True)
        survivor = self.active[found[0][0]]
        merged = tuple(number for number, _ in found[1:])
        for number in merged:
            self._merge(self.active[number], survivor)
        score = max(score for _, score in found)
        self._join(survivor, page, score)
        return Assignment(page.spike_id, survivor.number, score, created=False, merged=merged)

    def take_changes(self) -> list[Event]:
        """Events changed (created, grown, merged, ended) since the last call."""
        changed = sorted(self.changed.values(), key=lambda e: e.number)
        self.changed = {}
        return changed

    # --- internals ----------------------------------------------------------------

    def _join(self, event: Event, page: SpikePage, score: float) -> None:
        event.members.append(Member(page, score))
        event.refresh()
        self.event_of[page.spike_id] = event.number
        self.changed[event.number] = event

    def _merge(self, event: Event, survivor: Event) -> None:
        logger.info(
            "Merging event %d (%s) into %d",
            event.number,
            event.title,
            survivor.number,
            extra={
                "correlation_id": survivor.correlation_id,
                "merged_correlation_id": event.correlation_id,
            },
        )
        survivor.members.extend(event.members)
        survivor.refresh()
        for page in event.pages:
            self.event_of[page.spike_id] = survivor.number
        event.members = []
        event.status = MERGED
        event.merged_into = survivor.number
        del self.active[event.number]
        self.changed[event.number] = event
        self.changed[survivor.number] = survivor

    def _forget(self, event: Event) -> None:
        del self.active[event.number]
        for page in event.pages:
            self.event_of.pop(page.spike_id, None)
        self.changed[event.number] = event
