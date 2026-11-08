"""A small in-memory graph for the retrieval tests: no Neo4j, no network."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from wikipulse.qa.graph import (
    EntityCandidate,
    EventHit,
    EventInfo,
    GraphPath,
    Involvement,
    PageHit,
    PathStep,
    Relation,
    Source,
)

NOW = datetime(2026, 11, 5, 22, 54, tzinfo=UTC)

UENO = EntityCandidate("Q1", "Ueno Bank", ("ueno bank",), "organization", "Q1")
PENA = EntityCandidate("Q2", "Santiago Peña", ("Peña",), "person", "Q2")
US = EntityCandidate("Q30", "United States", ("US", "USA"), "place", "Q30")
PARTY = EntityCandidate("name:party", "Party", (), "other", None)

CRISIS = EventInfo(
    "run:341",
    "Paraguayan president resigns after Ueno Bank collapse",
    "Santiago Peña resigned after Ueno Bank collapsed.",
    datetime(2026, 11, 5, 16, 49, tzinfo=UTC),
    5,
    (Source("Ueno Bank", 101), Source("Santiago Peña", 102), Source("Pedro Alliana", 103)),
)
DARTS = EventInfo(
    "run:407",
    "Darts tournaments scheduled",
    None,
    datetime(2026, 11, 5, 18, 0, tzinfo=UTC),
    2,
    (Source("PDC", 201),),
)


@dataclass
class FakeGraph:
    """Answers the `GraphReader` calls from the small story above."""

    entities: list[EntityCandidate] = field(default_factory=lambda: [UENO, PENA, US, PARTY])
    events_by_key: dict[str, EventInfo] = field(
        default_factory=lambda: {CRISIS.key: CRISIS, DARTS.key: DARTS}
    )
    connected: bool = True
    rows: list[dict[str, Any]] = field(default_factory=list)
    page_hits: list[PageHit] = field(default_factory=list)
    event_hits: list[EventHit] = field(default_factory=list)
    queries: list[tuple[str, dict[str, Any], float, int]] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=list)
    windows: list[tuple[datetime, datetime]] = field(default_factory=list)

    def clock(self) -> datetime | None:
        return NOW

    def search_entities(self, query: str, limit: int = 100) -> list[EntityCandidate]:
        terms = set(query.split(" OR "))
        return [
            e
            for e in self.entities
            if any(word.lower().strip(".") in terms for word in e.name.split())
        ]

    def search_page_titles(self, query: str, limit: int = 50) -> list[str]:
        return []

    def page_events(self, titles: list[str]) -> dict[str, list[str]]:
        return {}

    def events_between(self, start: datetime, end: datetime, limit: int = 15) -> list[str]:
        self.windows.append((start, end))
        return list(self.events_by_key)[:limit]

    def events_by_number(self, numbers: list[int]) -> list[str]:
        return [k for k in self.events_by_key if int(k.split(":")[1]) in numbers]

    def events(self, keys: list[str]) -> list[EventInfo]:
        return [self.events_by_key[k] for k in keys if k in self.events_by_key]

    def entity_events(self, key: str, limit: int = 10) -> list[Involvement]:
        name = next(e.name for e in self.entities if e.key == key)
        return [Involvement(key, name, "x", CRISIS.key, CRISIS.title, Source(name, 900))]

    def event_entities(self, key: str, limit: int = 25) -> list[Involvement]:
        event = self.events_by_key[key]
        return [Involvement("Q3", "Pedro Alliana", "person", key, event.title, Source("Pedro", 7))]

    def entity_relations(self, key: str, limit: int = 15) -> list[Relation]:
        if key != UENO.key:
            return []
        return [
            Relation(
                "Ueno Bank", "absorbed", "Visión Banco", CRISIS.key, CRISIS.title, Source("Ueno", 1)
            )
        ]

    def event_relations(self, key: str, limit: int = 20) -> list[Relation]:
        return []

    def paths(self, a: str, b: str, max_hops: int = 4, limit: int = 3) -> list[GraphPath]:
        if not self.connected:
            return []
        names = {e.key: e.name for e in self.entities}
        steps = (
            PathStep(names[a], "involved in", CRISIS.title, Source("Ueno Bank", 101)),
            PathStep(names[b], "involved in", CRISIS.title, Source("Santiago Peña", 102)),
        )
        return [GraphPath((names[a], CRISIS.title, names[b]), steps)]

    def similar_pages(self, vector: list[float], k: int) -> list[PageHit]:
        self.vectors.append(vector)
        return self.page_hits[:k]

    def similar_events(self, vector: list[float], k: int) -> list[EventHit]:
        return self.event_hits[:k]

    def run_query(
        self, cypher: str, params: dict[str, Any], timeout_s: float, max_rows: int
    ) -> list[dict[str, Any]]:
        self.queries.append((cypher, params, timeout_s, max_rows))
        return self.rows[:max_rows]


class FixedWriter:
    """A CypherWriter stand-in that returns a prepared query."""

    def __init__(self, query: str = "", error: Exception | None = None) -> None:
        self.query = query
        self.error = error
        self.questions: list[str] = []

    def write(self, question: str, times: Any, max_rows: int = 25) -> str:
        self.questions.append(question)
        if self.error:
            raise self.error
        return self.query


class FakeEmbedder:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, text: str) -> np.ndarray:
        self.texts.append(text)
        return np.ones(4, dtype=np.float32) / 2
