"""Fakes for the MCP server tests: the small story graph of tests/qa plus the reads the
tools add, and a scripted question answerer. No Neo4j, no LLM, no network."""

from dataclasses import dataclass, field
from datetime import datetime

from neo4j.exceptions import ServiceUnavailable

from tests.qa.helpers import CRISIS, DARTS, NOW, PENA, UENO, FakeGraph
from wikipulse.qa.graph import EntityCandidate, EventInfo, Relation, Source
from wikipulse.qa.types import Answer, Citation, Context

__all__ = ["CRISIS", "DARTS", "NOW", "PENA", "UENO"]

# A second "Ueno" to make a name ambiguous.
UENO_PARK = EntityCandidate("Q9", "Ueno Park", ("Ueno",), "place", "Q9")
UENO_HOLDING = EntityCandidate("name:ueno", "Ueno", (), "organization", None)


@dataclass
class ToolGraph(FakeGraph):
    """`FakeGraph` with the extra reads of the MCP tools."""

    briefings: dict[str, str] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    trending_calls: list[tuple[datetime, int]] = field(default_factory=list)
    clock_value: datetime | None = NOW
    closed: bool = False

    def clock(self) -> datetime | None:
        return self.clock_value

    def trending_events(self, since: datetime, limit: int = 10) -> list[str]:
        self.trending_calls.append((since, limit))
        ordered = sorted(self.events_by_key.values(), key=lambda e: -(e.page_count or 0))
        return [e.key for e in ordered if e.started_at and e.started_at >= since][:limit]

    def event_relations(self, key: str, limit: int = 20) -> list[Relation]:
        return [r for r in self.relations if r.event_key == key][:limit]

    def event_briefing(self, key: str) -> str | None:
        return self.briefings.get(key)

    def entities_by_key(self, keys: list[str]) -> list[EntityCandidate]:
        return [e for e in self.entities if e.key in keys]

    def entity_event_counts(self, keys: list[str]) -> dict[str, int]:
        return {key: 2 if key == UENO.key else 1 for key in keys}

    def close(self) -> None:
        self.closed = True


class DownGraph:
    """A graph whose database is not running."""

    def __getattr__(self, name: str):
        def fail(*args, **kwargs):
            raise ServiceUnavailable("Couldn't connect to localhost:7687")

        return fail


def many_relations(key: str, count: int) -> list[Relation]:
    return [
        Relation(f"A{i}", "met", f"B{i}", key, "title", Source(f"Page {i}", 1000 + i))
        for i in range(count)
    ]


def crisis_with(**changes) -> EventInfo:
    fields = {**CRISIS.__dict__, **changes}
    return EventInfo(**fields)


class ScriptedAsker:
    """Stands in for `LazyAsker`: returns a prepared answer and records the questions."""

    def __init__(self, answer: Answer | None = None, error: Exception | None = None) -> None:
        self.answer = answer or Answer(
            question="q",
            text="Peña resigned after Ueno Bank collapsed [1][2].",
            answerable=True,
            citations=[Citation(title="Ueno Bank", revision=101), Citation(title="Santiago Peña")],
            limits=["The query results stop at the limit of 25 rows; there may be more."],
            removed_sentences=["An uncited claim."],
            context=Context(question="q", as_of=NOW),
            seconds={"total": 3.2},
        )
        self.error = error
        self.questions: list[str] = []
        self.closed = False

    def __call__(self, question: str) -> Answer:
        self.questions.append(question)
        if self.error:
            raise self.error
        return self.answer

    def close(self) -> None:
        self.closed = True
