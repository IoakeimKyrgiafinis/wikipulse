"""Events for the web API: grouping's view (Postgres) joined with the graph's (Neo4j).

An event is in Postgres seconds after its first spike is grouped (stage 3), so the page
can show it right away, titled after its strongest page. Once stage 4 has extracted it,
the graph adds a headline, a description, entities and relations; once the analyst agent
(05d) has written a briefing, that too. Nothing here writes: the Postgres queries are
SELECTs, and every graph read goes through `QAGraph` (READ sessions) and `Explorer`
(the MCP server's logic, reused unchanged).
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import quote

import psycopg

from wikipulse.api.models import (
    BriefingOut,
    BriefingSource,
    EventCard,
    EventDetailOut,
    EventList,
    EventPage,
)
from wikipulse.db.events import FeedEvent, FeedPage, latest_event_time, recent_events
from wikipulse.mcp_server.explore import UNREACHABLE, Explorer, ToolInputError, shorten
from wikipulse.mcp_server.models import EventDetail
from wikipulse.qa.graph import EventOverview

logger = logging.getLogger(__name__)

MAX_LIST = 200  # most events one GET /events returns
TOP_PAGES = 5
MAX_EVENT_PAGES = 30  # pages listed in an event's detail
LISTED = ("active", "ended")
ANY_STATUS = ("active", "ended", "merged")
SOURCES_HEADER = "\n\nSources:\n"
WIKIPEDIA_PAGE = "https://en.wikipedia.org/wiki/"
MAX_CACHED_OVERVIEWS = 5000

EVENT_ID = re.compile(r"^(?:(?P<run>[\w.-]{1,100}):)?(?P<number>\d{1,9})$")


class EventRows(Protocol):
    """The Postgres reads (tests pass a fake)."""

    def recent(
        self,
        run_id: str,
        limit: int,
        statuses: Sequence[str] = LISTED,
        min_pages: int = 1,
        number: int | None = None,
    ) -> list[FeedEvent]: ...

    def as_of(self, run_id: str) -> datetime | None: ...

    def ping(self) -> None: ...


class OverviewGraph(Protocol):
    def event_overviews(self, keys: list[str]) -> dict[str, EventOverview]: ...

    def clock(self) -> datetime | None: ...


class PostgresEvents:
    """`EventRows` on the real database: one short connection per call, so a restarted
    Postgres is picked up without restarting the API."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)

    def recent(
        self,
        run_id: str,
        limit: int,
        statuses: Sequence[str] = LISTED,
        min_pages: int = 1,
        number: int | None = None,
    ) -> list[FeedEvent]:
        with self._connect() as conn:
            return recent_events(conn, run_id, limit, statuses, min_pages, number)

    def as_of(self, run_id: str) -> datetime | None:
        with self._connect() as conn:
            return latest_event_time(conn, run_id)

    def ping(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1")


@dataclass(frozen=True)
class PageGroup:
    """The spikes of one page (canonical title) within an event."""

    title: str
    spikes: int
    first_started_at: datetime
    last_ended_at: datetime | None
    score: float


def group_pages(pages: Sequence[FeedPage]) -> list[PageGroup]:
    """One entry per distinct page, strongest spike first (03b counts pages this way)."""
    by_title: dict[str, list[FeedPage]] = {}
    for page in pages:
        by_title.setdefault(page.canonical_title, []).append(page)
    groups = []
    for title, spikes in by_title.items():
        ends = [s.ended_at for s in spikes if s.ended_at is not None]
        groups.append(
            PageGroup(
                title=title,
                spikes=len(spikes),
                first_started_at=min(s.started_at for s in spikes),
                last_ended_at=max(ends) if len(ends) == len(spikes) else None,
                score=max(s.score for s in spikes),
            )
        )
    return sorted(groups, key=lambda g: (-g.score, g.first_started_at, g.title))


def page_url(title: str) -> str:
    return WIKIPEDIA_PAGE + quote(title.replace(" ", "_"), safe="")


def event_key(run_id: str, number: int) -> str:
    """The graph's event key (stage 4 uses the same form)."""
    return f"{run_id}:{number}"


def parse_event_id(event_id: str, default_run: str) -> tuple[str, int]:
    """'run:12' or '12' (in the API's run) -> (run, 12). Raises ValueError otherwise."""
    match = EVENT_ID.match(event_id.strip())
    if not match:
        raise ValueError(
            f"{event_id!r} is not an event id. Event ids look like '{default_run}:12', or "
            "just the number."
        )
    return match.group("run") or default_run, int(match.group("number"))


def make_card(run_id: str, row: FeedEvent, overview: EventOverview | None) -> EventCard:
    pages = group_pages(row.pages)
    return EventCard(
        event_id=event_key(run_id, row.number),
        number=row.number,
        status=row.status,
        title=(overview.title if overview and overview.title else row.title),
        description=shorten(overview.description) if overview else None,
        started_at=row.started_at,
        last_spike_at=row.last_spike_at,
        ended_at=row.ended_at,
        page_count=len(pages),
        spike_count=len(row.pages),
        spike_score=round(row.score, 2),
        top_pages=[page.title for page in pages[:TOP_PAGES]],
        merged_into=event_key(run_id, row.merged_into) if row.merged_into else None,
        extracted=overview is not None,
        entity_count=overview.entity_count if overview else 0,
        has_briefing=bool(overview and overview.briefing_written_at),
    )


def briefing_out(text: str | None, overview: EventOverview | None) -> BriefingOut | None:
    """The stored briefing (05d) split into its text and its numbered sources."""
    if not text:
        return None
    body = text.split(SOURCES_HEADER, 1)[0].strip()
    sources = []
    if overview:
        urls = list(overview.briefing_urls) + [""] * len(overview.briefing_sources)
        sources = [
            BriefingSource(n=n, title=title, url=urls[n - 1] or None)
            for n, title in enumerate(overview.briefing_sources, start=1)
        ]
    return BriefingOut(
        text=body,
        sources=sources,
        verified=overview.briefing_verified if overview else None,
        model=overview.briefing_model if overview else None,
        written_at=overview.briefing_written_at if overview else None,
    )


class EventReader:
    """Lists and details of one grouping run's events, for the API and the watcher."""

    def __init__(
        self, rows: EventRows, graph: OverviewGraph, explorer: Explorer, run_id: str
    ) -> None:
        self.rows = rows
        self.graph = graph
        self.explorer = explorer
        self.run_id = run_id
        self._overviews: dict[str, EventOverview] = {}  # the last ones read, for outages

    def overviews(self, keys: list[str]) -> dict[str, EventOverview]:
        """Graph data for these events; while Neo4j is unreachable, the last known."""
        try:
            fresh = self.graph.event_overviews(keys)
        except UNREACHABLE as error:
            logger.warning("Neo4j is not reachable, showing the last known graph data: %s", error)
            return {key: self._overviews[key] for key in keys if key in self._overviews}
        if len(self._overviews) > MAX_CACHED_OVERVIEWS:
            self._overviews.clear()
        for key in keys:
            self._overviews.pop(key, None)
        self._overviews.update(fresh)
        return fresh

    def cards(
        self,
        limit: int = 50,
        statuses: Sequence[str] = LISTED,
        min_pages: int = 1,
    ) -> list[EventCard]:
        rows = self.rows.recent(self.run_id, min(limit, MAX_LIST), statuses, min_pages)
        keys = [event_key(self.run_id, row.number) for row in rows]
        found = self.overviews(keys)
        pairs = zip(rows, keys, strict=True)
        return [make_card(self.run_id, row, found.get(key)) for row, key in pairs]

    def listing(self, limit: int = 50, status: str = "all", min_pages: int = 1) -> EventList:
        statuses = LISTED if status == "all" else (status,)
        return EventList(
            run_id=self.run_id,
            as_of=self.rows.as_of(self.run_id),
            events=self.cards(limit, statuses, min_pages),
        )

    def detail(self, event_id: str) -> EventDetailOut | None:
        """One event in full, or None if the run has no such event. Raises ValueError for
        an id that isn't one."""
        run_id, number = parse_event_id(event_id, self.run_id)
        rows = self.rows.recent(run_id, 1, ANY_STATUS, 0, number)
        if not rows:
            return None
        row, key = rows[0], event_key(run_id, number)
        overview = self.overviews([key]).get(key)
        graph, note = self._graph_detail(key, overview)
        revisions = {page.title: page for page in graph.pages} if graph else {}
        pages = []
        for group in group_pages(row.pages)[:MAX_EVENT_PAGES]:
            source = revisions.get(group.title)
            pages.append(
                EventPage(
                    title=group.title,
                    spikes=group.spikes,
                    first_started_at=group.first_started_at,
                    last_ended_at=group.last_ended_at,
                    spike_score=round(group.score, 2),
                    url=source.url if source else page_url(group.title),
                    revision=source.revision if source else None,
                )
            )
        return EventDetailOut(
            event=make_card(run_id, row, overview),
            pages=pages,
            entities=graph.entities if graph else [],
            more_entities=graph.more_entities if graph else False,
            relations=graph.relations if graph else [],
            more_relations=graph.more_relations if graph else False,
            briefing=briefing_out(graph.briefing if graph else None, overview),
            note=note,
        )

    def _graph_detail(
        self, key: str, overview: EventOverview | None
    ) -> tuple[EventDetail | None, str | None]:
        if overview is None:
            return None, (
                "Not in the knowledge graph yet: entities, relations and a briefing appear "
                "once the event has been extracted."
            )
        try:
            return self.explorer.event(key), None
        except ToolInputError:
            return None, "Not in the knowledge graph yet."
        except UNREACHABLE as error:
            logger.warning("Neo4j is not reachable: %s", error)
            return None, "The knowledge graph (Neo4j) is not reachable right now."

    def health(self) -> tuple[bool, bool]:
        """(Postgres reachable, Neo4j reachable)."""
        return _works(self.rows.ping), _works(self.graph.clock)


def _works(check) -> bool:
    try:
        check()
    except Exception as error:  # noqa: BLE001 - health reports any failure as "down"
        logger.warning("Health check failed: %s", error)
        return False
    return True
