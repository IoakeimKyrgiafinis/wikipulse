"""What the web API sends and accepts. FastAPI turns these models into the JSON Schema of
the auto-generated docs at /docs, and validates every request against them.

Event fields combine two stores: grouping (stage 3, Postgres) knows an event as soon as
it is detected; extraction (stage 4) and the analyst agent (05d) add a title, entities
and a briefing in Neo4j later. `extracted` and `briefing` say how far an event has got.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from wikipulse.mcp_server.explore import MAX_QUESTION_CHARS
from wikipulse.mcp_server.models import AskResult, Involved, RelationOut

__all__ = [
    "AskRequest",
    "AskResult",
    "BriefingOut",
    "BriefingSource",
    "EventCard",
    "EventDetailOut",
    "EventList",
    "EventPage",
    "Health",
]

EventStatus = Literal["active", "ended", "merged"]


class EventCard(BaseModel):
    """One event as the live list shows it."""

    event_id: str = Field(description="'<grouping run>:<number>', e.g. 'live:12'.")
    number: int
    status: EventStatus
    title: str = Field(
        description="The extraction model's headline once extracted, else the title of "
        "the page with the highest spike score."
    )
    description: str | None = Field(None, description="One sentence, once extracted.")
    started_at: datetime
    last_spike_at: datetime = Field(description="When its latest spike started.")
    ended_at: datetime | None = None
    page_count: int = Field(description="Distinct Wikipedia pages that spiked together.")
    spike_count: int
    spike_score: float = Field(description="Best spike score of its pages (bigger = stronger).")
    news_score: float | None = Field(
        None,
        description="The news classifier's highest score of its spikes (0 to 1, how likely "
        "it is real news); None until a spike is scored, ten minutes after it starts.",
    )
    is_news: bool | None = Field(
        None,
        description="True when the classifier flags at least one of its spikes as news (score "
        "at or above the model's threshold); None until a spike is scored.",
    )
    top_pages: list[str] = Field(description="Up to five page titles, strongest first.")
    merged_into: str | None = Field(
        None, description="For a merged event: the event_id that absorbed it."
    )
    extracted: bool = Field(description="True once its entities are in the knowledge graph.")
    entity_count: int = 0
    has_briefing: bool = False


class EventList(BaseModel):
    run_id: str
    as_of: datetime | None = Field(
        description="The run's newest event time: event time, not the wall clock."
    )
    events: list[EventCard]


class EventPage(BaseModel):
    """A Wikipedia page of the event, with its spikes in it."""

    title: str
    spikes: int
    first_started_at: datetime
    last_ended_at: datetime | None = Field(None, description="None while a spike is active.")
    spike_score: float
    news_score: float | None = Field(
        None, description="The news classifier's highest score of its spikes on this page."
    )
    url: str = Field(
        description="The revision the knowledge graph read, once extracted; else the page."
    )
    revision: int | None = None


class BriefingSource(BaseModel):
    n: int = Field(description="The [n] marker in the briefing text.")
    title: str
    url: str | None = None


class BriefingOut(BaseModel):
    text: str = Field(description="The briefing, citing sources as [n].")
    sources: list[BriefingSource]
    verified: bool | None = Field(
        None, description="True when the grounding check passed it; False if it could not run."
    )
    model: str | None = None
    written_at: datetime | None = None


class EventDetailOut(BaseModel):
    event: EventCard
    pages: list[EventPage]
    entities: list[Involved] = Field(default_factory=list)
    more_entities: bool = False
    relations: list[RelationOut] = Field(default_factory=list)
    more_relations: bool = False
    briefing: BriefingOut | None = None
    note: str | None = Field(None, description="Why something is missing, if it is.")


class AskRequest(BaseModel):
    """One question for the knowledge graph."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        min_length=1,
        max_length=MAX_QUESTION_CHARS,
        description="One question in plain English, e.g. 'Who is involved in the Paraguayan "
        "banking crisis?'",
        examples=["How are Ueno Bank and Santiago Peña connected?"],
    )


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    postgres: bool
    neo4j: bool
    run_id: str
    websocket_clients: int
    last_poll_ok: bool | None = Field(
        None, description="Whether the watcher's last look for changes worked (None: not yet)."
    )
