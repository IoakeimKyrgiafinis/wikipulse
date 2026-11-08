"""What the MCP tools return. Each model becomes the tool's output schema, and the
client gets it both as JSON text and as structured content.

Field names and descriptions are read by the client's model, so they say what a value
means and, where useful, which tool to call next.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from wikipulse.qa.graph import Source
from wikipulse.qa.types import Citation


class SourceOut(BaseModel):
    """A Wikipedia page at the revision a fact was read from."""

    title: str
    revision: int | None = None
    url: str = Field(description="Permanent link to exactly that revision.")

    @classmethod
    def of(cls, source: Source | Citation) -> "SourceOut":
        citation = Citation(title=source.title, revision=source.revision)
        return cls(title=citation.title, revision=citation.revision, url=citation.url)


class EventSummary(BaseModel):
    event_id: str = Field(description="Pass this to get_event or read event://{event_id}.")
    title: str | None
    description: str | None = None
    status: str | None = Field(None, description="'active' while its pages still spike.")
    started_at: datetime | None = None
    ended_at: datetime | None = None
    page_count: int | None = Field(None, description="Wikipedia pages that spiked together.")
    spike_score: float | None = Field(
        None, description="Best spike score of its pages (how unusual the editing was)."
    )
    top_pages: list[str] = Field(default_factory=list)


class TrendingEvents(BaseModel):
    as_of: datetime | None = Field(
        description="The graph's 'now': the newest event time, not the wall clock."
    )
    window_hours: float
    events: list[EventSummary]
    note: str | None = None


class EntityOut(BaseModel):
    entity_id: str = Field(description="The Wikidata ID when linked, else 'name:...'.")
    name: str
    type: str | None = None
    wikidata_id: str | None = None


class Involved(EntityOut):
    source: SourceOut


class RelationOut(BaseModel):
    subject: str
    relation: str
    object: str
    source: SourceOut


class EventDetail(BaseModel):
    event: EventSummary
    pages: list[SourceOut] = Field(description="Its pages, most unusual editing first.")
    entities: list[Involved]
    more_entities: bool = False
    relations: list[RelationOut]
    more_relations: bool = False
    briefing: str | None = Field(
        None, description="The analyst agent's briefing, if one has been written."
    )


class AskResult(BaseModel):
    question: str
    answer: str = Field(description="Cites sources as [n]; n is the number in `sources`.")
    answerable: bool = Field(description="False when the graph has no facts to answer it.")
    sources: list[SourceOut] = Field(description="Source [1] is the first item.")
    notes: list[str] = Field(
        default_factory=list, description="Lists that stopped at a limit, and similar caveats."
    )
    as_of: datetime | None = None
    seconds: float | None = None


class PathStepOut(BaseModel):
    start: str
    label: str = Field(description="'involved in' or the relation, read start -> end.")
    end: str
    source: SourceOut | None = None


class PathOut(BaseModel):
    summary: str
    steps: list[PathStepOut]


class Connection(BaseModel):
    entity_a: EntityOut
    entity_b: EntityOut
    max_steps: int
    paths: list[PathOut]
    note: str | None = None


class EventRef(BaseModel):
    event_id: str
    title: str | None


class EntityMatch(EntityOut):
    aliases: list[str] = Field(default_factory=list)
    events: int | None = Field(None, description="Number of events it is involved in.")
    top_events: list[EventRef] = Field(
        default_factory=list, description="Its biggest events (at most 3); see get_event."
    )
    exact: bool = Field(description="True when the name or an alias equals the query.")


class EntitySearch(BaseModel):
    query: str
    entities: list[EntityMatch]
    note: str | None = None
