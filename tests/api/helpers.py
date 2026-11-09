"""Fakes for the API tests: grouping rows in memory, the MCP tests' story graph with the
overview read, and a scripted asker. No Postgres, no Neo4j, no LLM, no network."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
from neo4j.exceptions import ServiceUnavailable

from tests.mcp_server.helpers import CRISIS, DARTS, ScriptedAsker, ToolGraph
from wikipulse.api.app import create_app
from wikipulse.api.events import EventReader
from wikipulse.db.events import FeedEvent, FeedPage
from wikipulse.mcp_server.explore import Explorer
from wikipulse.qa.graph import EventOverview

RUN = "run"  # the grouping run; the story graph's keys are "run:341" and "run:407"
T0 = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)
BRIEFING = (
    "Santiago Peña resigned after Ueno Bank collapsed [1][2].\n\nSources:\n"
    "[1] Ueno Bank, revision 101: https://en.wikipedia.org/w/index.php?oldid=101\n"
    "[2] WikiPulse event run:341: title and description"
)


def page(title: str, minutes: float, score: float, canonical: str | None = None, ended=True):
    started = T0 + timedelta(minutes=minutes)
    return FeedPage(
        title=title,
        canonical_title=canonical or title,
        started_at=started,
        ended_at=started + timedelta(minutes=30) if ended else None,
        score=score,
        joined_score=1.0,
    )


def feed_event(number: int, title: str, pages: Sequence[FeedPage], status: str = "ended", **kw):
    started = min(p.started_at for p in pages)
    last = max(p.started_at for p in pages)
    ends = [p.ended_at for p in pages]
    return FeedEvent(
        number=number,
        status=status,
        title=title,
        started_at=started,
        last_spike_at=last,
        ended_at=None if status == "active" or None in ends else max(ends),
        page_count=len(pages),
        score=max(p.score for p in pages),
        pages=tuple(pages),
        **kw,
    )


def crisis_row(status: str = "ended") -> FeedEvent:
    return feed_event(
        341,
        "Ueno Bank",
        [
            page("Ueno Bank", 150, 9.19),
            page("Santiago Peña", 60, 6.4),
            page("Santiago Pena", 70, 5.0, canonical="Santiago Peña"),  # a redirect
        ],
        status,
    )


def darts_row() -> FeedEvent:
    return feed_event(407, "PDC", [page("PDC", 120, 4.0)])


def fresh_row(number: int = 500, status: str = "active") -> FeedEvent:
    """An event grouping has just created; not extracted into the graph."""
    return feed_event(
        number, "Storm Amy", [page("Storm Amy", 400, 7.0, ended=False)], status=status
    )


@dataclass
class FakeRows:
    """`EventRows` over a list, with the SQL's filters and order."""

    events: list[FeedEvent] = field(default_factory=lambda: [crisis_row(), darts_row()])
    down: bool = False
    calls: int = 0

    def _check(self) -> None:
        self.calls += 1
        if self.down:
            raise psycopg.OperationalError("connection refused")

    def recent(self, run_id, limit, statuses=("active", "ended"), min_pages=1, number=None):
        self._check()
        if run_id != RUN:
            return []
        found = [
            e
            for e in self.events
            if e.status in statuses
            and (number is None or e.number == number)
            and len({p.canonical_title for p in e.pages}) >= min_pages
        ]
        found.sort(key=lambda e: (e.status != "active", -e.last_spike_at.timestamp(), -e.number))
        return found[:limit]

    def as_of(self, run_id: str) -> datetime | None:
        self._check()
        times = [t for e in self.events for t in (e.last_spike_at, e.ended_at) if t]
        return max(times) if times else None

    def ping(self) -> None:
        self._check()

    def replace(self, event: FeedEvent) -> None:
        self.events = [e for e in self.events if e.number != event.number] + [event]


@dataclass
class OverviewGraph(ToolGraph):
    """The MCP tests' graph plus `event_overviews`."""

    overviews: dict[str, EventOverview] = field(default_factory=dict)
    down: bool = False

    def event_overviews(self, keys: list[str]) -> dict[str, EventOverview]:
        if self.down:
            raise ServiceUnavailable("Couldn't connect to localhost:7687")
        return {key: self.overviews[key] for key in keys if key in self.overviews}

    def events(self, keys: list[str]):
        if self.down:
            raise ServiceUnavailable("Couldn't connect to localhost:7687")
        return super().events(keys)

    def clock(self):
        if self.down:
            raise ServiceUnavailable("Couldn't connect to localhost:7687")
        return super().clock()


def story_graph() -> OverviewGraph:
    """Event 341 extracted with a briefing, 407 extracted without, 500 not at all."""
    return OverviewGraph(
        briefings={CRISIS.key: BRIEFING},
        overviews={
            CRISIS.key: EventOverview(
                CRISIS.key,
                CRISIS.title,
                CRISIS.description,
                entity_count=4,
                extracted_at=T0,
                briefing_written_at=T0 + timedelta(days=1),
                briefing_verified=True,
                briefing_model="fake:m",
                briefing_sources=("Ueno Bank", "WikiPulse event run:341: title and description"),
                briefing_urls=("https://en.wikipedia.org/w/index.php?oldid=101", ""),
            ),
            DARTS.key: EventOverview(DARTS.key, DARTS.title, None, entity_count=1),
        },
    )


@dataclass
class Parts:
    rows: FakeRows
    graph: OverviewGraph
    asker: ScriptedAsker
    reader: EventReader


def make_parts(asker: ScriptedAsker | None = None) -> Parts:
    rows, graph = FakeRows(), story_graph()
    asker = asker or ScriptedAsker()
    reader = EventReader(rows, graph, Explorer(graph, asker), RUN)
    return Parts(rows, graph, asker, reader)


def make_app(parts: Parts, **options):
    options.setdefault("poll_interval_s", 0.05)
    options.setdefault("allowed_hosts", ("127.0.0.1", "localhost"))
    return create_app(parts.reader, parts.reader.explorer, **options)
