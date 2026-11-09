"""Events from a JSON file, served like the real graph: for prompt-injection tests.

The injection cases (`injection_cases.json`) are small invented news events whose page
text, description or relations contain planted instructions. `FixtureGraph` answers
every read the MCP tools and the agent's local tools make (the `AgentGraph` protocol)
from those events in memory, so the real MCP server code runs over them without Neo4j
and the agent "replays" each event exactly as it would investigate a live one.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from wikipulse.qa.graph import (
    EntityCandidate,
    EventInfo,
    GraphPath,
    Involvement,
    PageText,
    PathStep,
    Relation,
    Source,
)
from wikipulse.qa.mentions import fold

CASES_PATH = Path(__file__).parent / "injection_cases.json"


class FixturePage(BaseModel):
    title: str
    revision: int
    score: float = 5.0
    description: str | None = None
    summary: str | None = None


class FixtureEntity(BaseModel):
    key: str
    name: str
    type: str = "other"
    page: str  # the page (title) the entity was extracted from
    aliases: list[str] = Field(default_factory=list)


class FixtureRelation(BaseModel):
    subject: str
    relation: str
    object: str
    page: str


class FixtureEvent(BaseModel):
    key: str
    title: str
    description: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    pages: list[FixturePage]
    entities: list[FixtureEntity] = Field(default_factory=list)
    relations: list[FixtureRelation] = Field(default_factory=list)


class InjectionCase(BaseModel):
    """One event with planted instructions, what a briefing must not contain, and what
    it must still say."""

    id: str
    attack: str  # what the planted text tries to make the agent do
    forbidden: list[str] = Field(default_factory=list)  # what the attack asks for
    # What the planted text says: a briefing that repeats or reports the instruction
    # ("the council instructed AI agents to link ...") passes it on to its readers.
    reports: list[str] = Field(default_factory=list)
    # Words of which the briefing must contain at least one: it still says what happened.
    covers: list[str] = Field(default_factory=list)
    forbidden_in_tool_args: list[str] = Field(default_factory=list)  # in any call
    event: FixtureEvent


def load_cases(path: Path = CASES_PATH) -> list[InjectionCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [InjectionCase.model_validate(case) for case in data["cases"]]


@dataclass
class FixtureGraph:
    """The `AgentGraph` reads over a list of fixture events."""

    events_by_key: dict[str, FixtureEvent] = field(default_factory=dict)
    briefings: dict[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, events: list[FixtureEvent]) -> "FixtureGraph":
        return cls({event.key: event for event in events})

    # --- helpers ------------------------------------------------------------------------------
    def _page(self, event: FixtureEvent, title: str) -> FixturePage | None:
        return next((p for p in event.pages if p.title == title), None)

    def _source(self, event: FixtureEvent, title: str) -> Source:
        page = self._page(event, title)
        return Source(title, page.revision if page else None)

    def _info(self, event: FixtureEvent) -> EventInfo:
        pages = sorted(event.pages, key=lambda p: -p.score)
        return EventInfo(
            key=event.key,
            title=event.title,
            description=event.description,
            started_at=event.started_at,
            page_count=len(event.pages),
            pages=tuple(Source(p.title, p.revision) for p in pages),
            status="ended" if event.ended_at else "active",
            ended_at=event.ended_at,
            score=pages[0].score if pages else None,
        )

    def _entities(self) -> dict[str, EntityCandidate]:
        found: dict[str, EntityCandidate] = {}
        for event in self.events_by_key.values():
            for e in event.entities:
                wikidata = e.key if e.key.startswith("Q") else None
                found[e.key] = EntityCandidate(e.key, e.name, tuple(e.aliases), e.type, wikidata)
        return found

    def _involvements(self) -> list[Involvement]:
        return [
            Involvement(e.key, e.name, e.type, ev.key, ev.title, self._source(ev, e.page))
            for ev in self.events_by_key.values()
            for e in ev.entities
        ]

    # --- the reads ----------------------------------------------------------------------------
    def clock(self) -> datetime | None:
        times = [t for e in self.events_by_key.values() for t in (e.started_at, e.ended_at) if t]
        return max(times) if times else None

    def trending_events(self, since: datetime, limit: int = 10) -> list[str]:
        ordered = sorted(self.events_by_key.values(), key=lambda e: -len(e.pages))
        return [e.key for e in ordered if e.started_at >= since or not e.ended_at][:limit]

    def events(self, keys: list[str]) -> list[EventInfo]:
        return [self._info(self.events_by_key[k]) for k in keys if k in self.events_by_key]

    def events_by_number(self, numbers: list[int]) -> list[str]:
        wanted = {str(n) for n in numbers}
        return [k for k in self.events_by_key if k.rpartition(":")[2] in wanted]

    def event_entities(self, key: str, limit: int = 25) -> list[Involvement]:
        return [i for i in self._involvements() if i.event_key == key][:limit]

    def entity_events(self, key: str, limit: int = 10) -> list[Involvement]:
        return [i for i in self._involvements() if i.entity_key == key][:limit]

    def event_relations(self, key: str, limit: int = 20) -> list[Relation]:
        event = self.events_by_key.get(key)
        if event is None:
            return []
        return [
            Relation(r.subject, r.relation, r.object, key, event.title, self._source(event, r.page))
            for r in event.relations
        ][:limit]

    def entity_relations(self, key: str, limit: int = 15) -> list[Relation]:
        name = self._entities().get(key)
        relations = [r for k in self.events_by_key for r in self.event_relations(k)]
        return [r for r in relations if name and name.name in (r.subject, r.object)][:limit]

    def event_briefing(self, key: str) -> str | None:
        return self.briefings.get(key)

    def search_entities(self, query: str, limit: int = 100) -> list[EntityCandidate]:
        terms = {fold(t) for t in query.split(" OR ") if t.strip()}
        found = [
            e
            for e in self._entities().values()
            if terms & set(fold(" ".join((e.name, *e.aliases))).split())
        ]
        return found[:limit]

    def entities_by_key(self, keys: list[str]) -> list[EntityCandidate]:
        entities = self._entities()
        return [entities[k] for k in keys if k in entities]

    def entity_event_counts(self, keys: list[str]) -> dict[str, int]:
        return {k: len(self.entity_events(k)) for k in keys}

    def paths(self, a: str, b: str, max_hops: int = 4, limit: int = 3) -> list[GraphPath]:
        """Two entities of the same event are connected through it (two steps)."""
        found = []
        for left in self.entity_events(a):
            for right in self.entity_events(b):
                if left.event_key != right.event_key:
                    continue
                title = left.event_title or left.event_key
                steps = (
                    PathStep(left.entity_name, "involved in", title, left.source),
                    PathStep(right.entity_name, "involved in", title, right.source),
                )
                found.append(GraphPath((left.entity_name, title, right.entity_name), steps))
        return found[:limit]

    def page_text(self, title: str, event_key: str | None = None) -> PageText | None:
        """Like `QAGraph.page_text`: the revision of event `event_key`, else the newest."""
        newest_first = sorted(self.events_by_key.values(), key=lambda e: e.started_at, reverse=True)
        found = [
            (event, page)
            for event in newest_first
            for page in event.pages
            if fold(page.title) == fold(title)
        ]
        if not found:
            return None
        _, page = next(((e, p) for e, p in found if e.key == event_key), found[0])
        keys = tuple(e.key for e, _ in found)
        return PageText(page.title, page.description, page.summary, page.revision, keys)

    def close(self) -> None:
        pass
