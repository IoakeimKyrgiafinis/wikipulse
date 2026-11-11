"""What the MCP tools do, without MCP: read the graph and answer questions.

Everything here reuses `wikipulse.qa` (requirement 1): `QAGraph` for every read (always
a READ session), `fold()` and `lucene_terms()` for names, `path_text()` for paths, and
`QASession` for answers. `server.py` only wraps these methods as tools.

Output limits (requirement 2) are constants below. A request that can't be served
raises `ToolInputError`, whose message tells the calling model what to do instead.
"""

import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from neo4j.exceptions import AuthError, ServiceUnavailable, SessionExpired

from wikipulse.config import Settings
from wikipulse.mcp_server.models import (
    AskResult,
    Connection,
    EntityMatch,
    EntityOut,
    EntitySearch,
    EventDetail,
    EventRef,
    EventSummary,
    Involved,
    PathOut,
    PathStepOut,
    RelationOut,
    SourceOut,
    TrendingEvents,
)
from wikipulse.qa.answer import QASession
from wikipulse.qa.graph import EntityCandidate, EventInfo, QAGraph, lucene_terms
from wikipulse.qa.mentions import STOPWORDS, fold
from wikipulse.qa.retrieve import RetrievalConfig, path_text
from wikipulse.qa.types import Answer

logger = logging.getLogger(__name__)

MAX_LIMIT = 25  # most events or entities one call returns
MAX_HOURS = 24 * 14
MAX_DESCRIPTION_CHARS = 400
MAX_TOP_PAGES = 5  # page titles shown per event in lists
MAX_EVENT_PAGES = 10
MAX_EVENT_ENTITIES = 25
MAX_EVENT_RELATIONS = 20
MAX_PATHS = 3
MAX_HOPS = RetrievalConfig().max_hops  # the same 4 steps as question answering
MAX_QUESTION_CHARS = 500
MAX_NAME_CHARS = 200
MAX_SUGGESTIONS = 5
MAX_ENTITY_EVENTS = 3  # events listed per entity found by search
SEARCH_CANDIDATES = 100

_EVENT_KEY = re.compile(r"^[\w.-]+:\d+$")
_EVENT_NUMBER = re.compile(r"^(?:event\s*)?(?:#|no\.?\s*|number\s*)?(\d+)$", re.IGNORECASE)
_ENTITY_KEY = re.compile(r"^(Q\d+|name:.+)$")
# Errors meaning "the database is not there", as opposed to a bad query.
UNREACHABLE = (ServiceUnavailable, SessionExpired, AuthError, OSError)


class ToolInputError(Exception):
    """A request the tools can't serve. The message says what to do instead."""


class ExplorerGraph(Protocol):
    """The `QAGraph` reads the tools use (tests pass a fake)."""

    def clock(self) -> datetime | None: ...
    def trending_events(self, since: datetime, limit: int = 10) -> list[str]: ...
    def events(self, keys: list[str]) -> list[EventInfo]: ...
    def events_by_number(self, numbers: list[int]) -> list[str]: ...
    def event_entities(self, key: str, limit: int = 25) -> list: ...
    def entity_events(self, key: str, limit: int = 10) -> list: ...
    def event_relations(self, key: str, limit: int = 20) -> list: ...
    def event_briefing(self, key: str) -> str | None: ...
    def search_entities(self, query: str, limit: int = 100) -> list[EntityCandidate]: ...
    def entities_by_key(self, keys: list[str]) -> list[EntityCandidate]: ...
    def entity_event_counts(self, keys: list[str]) -> dict[str, int]: ...
    def paths(self, a: str, b: str, max_hops: int = 4, limit: int = 3) -> list: ...
    def close(self) -> None: ...


def shorten(text: str | None, limit: int = MAX_DESCRIPTION_CHARS) -> str | None:
    """`text` cut to `limit` characters (None stays None)."""
    if text is None or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def summarize_event(info: EventInfo) -> EventSummary:
    """The short form of an event that list tools return."""
    return EventSummary(
        event_id=info.key,
        title=info.title,
        description=shorten(info.description),
        status=info.status,
        started_at=info.started_at,
        ended_at=info.ended_at,
        page_count=info.page_count,
        spike_score=round(info.score, 2) if info.score is not None else None,
        top_pages=[page.title for page in info.pages[:MAX_TOP_PAGES]],
    )


def entity_out(entity: EntityCandidate) -> EntityOut:
    """An entity as the tools return it."""
    return EntityOut(
        entity_id=entity.key, name=entity.name, type=entity.type, wikidata_id=entity.wikidata_id
    )


def _names(entity: EntityCandidate) -> set[str]:
    return {fold(name) for name in (entity.name, *entity.aliases) if name}


def _describe(entities: list[EntityCandidate]) -> str:
    return "; ".join(f"{e.name} ({e.type or 'unknown type'}, id {e.key})" for e in entities)


class LazyAsker:
    """Answers questions with one `QASession`, created at the first question (loading the
    embedding model takes seconds). One question at a time: they share the GPU."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: QASession | None = None
        self._lock = threading.Lock()

    def __call__(self, question: str) -> Answer:
        with self._lock:
            if self._session is None:
                logger.info("Starting the question-answering session (%s)", self.settings.qa_model)
                self._session = QASession.from_settings(self.settings)
            return self._session.ask(question)

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None


class Explorer:
    """The five read-only tools, as plain methods."""

    def __init__(
        self,
        graph: ExplorerGraph,
        asker: Callable[[str], Answer] | None = None,
        trending_hours: float = 24,
    ) -> None:
        self.graph = graph
        self.asker = asker
        self.trending_hours = trending_hours

    @classmethod
    def from_settings(cls, settings: Settings) -> "Explorer":
        return cls(
            QAGraph.from_settings(settings), LazyAsker(settings), settings.mcp_trending_hours
        )

    def close(self) -> None:
        close = getattr(self.asker, "close", None)
        if close is not None:
            close()
        self.graph.close()

    # --- Events ------------------------------------------------------------------------------
    def trending(self, limit: int = 10, hours: float | None = None) -> TrendingEvents:
        """The strongest active or recent events, relative to the graph's own clock."""
        limit = max(1, min(limit, MAX_LIMIT))
        hours = min(hours or self.trending_hours, MAX_HOURS)
        now = self.graph.clock()
        if now is None:
            return TrendingEvents(
                as_of=None, window_hours=hours, events=[], note="The graph has no events yet."
            )
        keys = self.graph.trending_events(now - timedelta(hours=hours), limit)
        by_key = {info.key: info for info in self.graph.events(keys)}
        events = [summarize_event(by_key[key]) for key in keys if key in by_key]
        note = None
        if not events:
            note = f"No event is active or started in the {hours:g} hours before {now}."
            note += " Call again with a larger `hours`."
        return TrendingEvents(as_of=now, window_hours=hours, events=events, note=note)

    def find_event(self, event_id: str) -> EventInfo:
        """The event an id names: its key ("run:341"), its number ("341", "event 341") or
        `event://...`. Raises ToolInputError with what to try instead."""
        text = event_id.strip().removeprefix("event://").strip()
        if _EVENT_KEY.match(text):
            keys = [text]
        elif match := _EVENT_NUMBER.match(text):
            keys = self.graph.events_by_number([int(match.group(1))])
        else:
            raise ToolInputError(
                f"{event_id!r} is not an event id. Event ids look like '03b-default-fix:341' "
                "(or just the number, 341); get them from get_trending_events. To find events "
                "about a topic, use ask or search_entities."
            )
        if len(keys) > 1:
            raise ToolInputError(
                f"Event number {text} exists in several grouping runs: {', '.join(keys)}. "
                "Call again with one of these ids."
            )
        found = self.graph.events(keys) if keys else []
        if not found:
            raise ToolInputError(
                f"There is no event {event_id!r} in the graph. Get current event ids from "
                "get_trending_events."
            )
        return found[0]

    def event(self, event_id: str) -> EventDetail:
        """One event with its pages, entities, relations and briefing."""
        info = self.find_event(event_id)
        entities = self.graph.event_entities(info.key, limit=MAX_EVENT_ENTITIES + 1)
        relations = self.graph.event_relations(info.key, limit=MAX_EVENT_RELATIONS + 1)
        return EventDetail(
            event=summarize_event(info),
            pages=[SourceOut.of(page) for page in info.pages[:MAX_EVENT_PAGES]],
            entities=[
                Involved(
                    entity_id=item.entity_key,
                    name=item.entity_name,
                    type=item.type,
                    wikidata_id=None if item.entity_key.startswith("name:") else item.entity_key,
                    source=SourceOut.of(item.source),
                )
                for item in entities[:MAX_EVENT_ENTITIES]
            ],
            more_entities=len(entities) > MAX_EVENT_ENTITIES,
            relations=[
                RelationOut(
                    subject=item.subject,
                    relation=item.relation,
                    object=item.object,
                    source=SourceOut.of(item.source),
                )
                for item in relations[:MAX_EVENT_RELATIONS]
            ],
            more_relations=len(relations) > MAX_EVENT_RELATIONS,
            briefing=self.graph.event_briefing(info.key),
        )

    # --- Questions ---------------------------------------------------------------------------
    def ask(self, question: str) -> AskResult:
        """A cited GraphRAG answer (`QASession.ask`)."""
        question = " ".join(question.split())
        if not question:
            raise ToolInputError("The question is empty. Ask one question in plain English.")
        if len(question) > MAX_QUESTION_CHARS:
            raise ToolInputError(
                f"The question has {len(question)} characters; the limit is "
                f"{MAX_QUESTION_CHARS}. Ask one short question at a time."
            )
        if self.asker is None:
            raise ToolInputError("Question answering is not available on this server.")
        answer = self.asker(question)
        notes = list(answer.limits)
        if answer.removed_sentences:
            notes.append(
                f"{len(answer.removed_sentences)} sentence(s) without a citation were removed "
                "from the answer."
            )
        return AskResult(
            question=question,
            answer=answer.text,
            answerable=answer.answerable,
            sources=[SourceOut.of(citation) for citation in answer.citations],
            notes=notes,
            as_of=answer.context.as_of if answer.context else None,
            seconds=answer.seconds.get("total"),
        )

    # --- Entities ----------------------------------------------------------------------------
    def _candidates(self, name: str) -> list[EntityCandidate]:
        """Entities by key (a Wikidata ID or 'name:...'), else by the full-text index."""
        name = name.strip()
        if not name:
            raise ToolInputError("The name is empty. Give an entity name, e.g. 'Ueno Bank'.")
        if len(name) > MAX_NAME_CHARS:
            raise ToolInputError(
                f"The name has {len(name)} characters; the limit is {MAX_NAME_CHARS}. "
                "Give just the entity's name."
            )
        if _ENTITY_KEY.match(name):
            found = self.graph.entities_by_key([name])
            if found:
                return found
        query = lucene_terms(name, STOPWORDS)
        return self.graph.search_entities(query, limit=SEARCH_CANDIDATES) if query else []

    def resolve_entity(self, name: str) -> EntityCandidate:
        """The one entity `name` means: its id, or its exact name or alias (case and
        accents ignored). Raises ToolInputError listing the candidates otherwise."""
        candidates = self._candidates(name)
        if _ENTITY_KEY.match(name.strip()) and len(candidates) == 1:
            return candidates[0]
        exact = [c for c in candidates if fold(name) in _names(c)]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ToolInputError(
                f"Several entities are named {name!r}: {_describe(exact[:MAX_SUGGESTIONS])}. "
                "Call again with the id of the one you mean."
            )
        if candidates:
            close = rank(name, candidates)[:MAX_SUGGESTIONS]
            raise ToolInputError(
                f"No entity is named exactly {name!r}. Close matches: {_describe(close)}. "
                "Call again with one of these ids or names."
            )
        raise ToolInputError(
            f"No entity matches {name!r}. Try search_entities with one distinctive word of "
            "the name, or ask a question instead."
        )

    def connection(self, entity_a: str, entity_b: str) -> Connection:
        """The shortest paths (at most MAX_HOPS steps) between two entities."""
        a, b = self.resolve_entity(entity_a), self.resolve_entity(entity_b)
        if a.key == b.key:
            raise ToolInputError(
                f"{entity_a!r} and {entity_b!r} are the same entity ({a.name}, id {a.key}). "
                "Give two different entities."
            )
        paths = self.graph.paths(a.key, b.key, max_hops=MAX_HOPS, limit=MAX_PATHS)
        note = None
        if not paths:
            note = (
                f"No connection within {MAX_HOPS} steps. Use get_event on their events, or "
                "ask, to look for an indirect link."
            )
        return Connection(
            entity_a=entity_out(a),
            entity_b=entity_out(b),
            max_steps=MAX_HOPS,
            paths=[
                PathOut(
                    summary=path_text(path),
                    steps=[
                        PathStepOut(
                            start=step.start,
                            label=step.label,
                            end=step.end,
                            source=SourceOut.of(step.source) if step.source else None,
                        )
                        for step in path.steps
                    ],
                )
                for path in paths
            ],
            note=note,
        )

    def search(self, name: str, limit: int = 10) -> EntitySearch:
        """Entities whose name or alias shares words with `name`, best match first."""
        limit = max(1, min(limit, MAX_LIMIT))
        ranked = rank(name, self._candidates(name))[:limit]
        counts = self.graph.entity_event_counts([e.key for e in ranked]) if ranked else {}
        matches = [
            EntityMatch(
                **entity_out(entity).model_dump(),
                aliases=[a for a in entity.aliases if a != entity.name][:MAX_SUGGESTIONS],
                events=counts.get(entity.key),
                top_events=[
                    EventRef(event_id=item.event_key, title=item.event_title)
                    for item in self.graph.entity_events(entity.key, limit=MAX_ENTITY_EVENTS)
                ],
                exact=fold(name) in _names(entity),
            )
            for entity in ranked
        ]
        note = None
        if not matches:
            note = (
                f"No entity in the knowledge graph matches {name!r}: WikiPulse has not seen it "
                "in the news events it detected. Do not answer from other knowledge as if it "
                "came from WikiPulse. Try another spelling, or call ask with the question."
            )
        elif all(overlap(name, entity) < 1 for entity in ranked):
            note = (
                f"No entity has every word of {name!r}; these share only some words and are "
                f"probably something else, so WikiPulse has likely not seen {name!r} itself."
            )
        return EntitySearch(query=name, entities=matches, note=note)


def overlap(name: str, entity: EntityCandidate) -> float:
    """The share of the words of `name` that the entity's name or aliases contain."""
    wanted = set(fold(name).split()) - STOPWORDS
    words = set(" ".join(_names(entity)).split())
    return len(wanted & words) / len(wanted) if wanted else 0.0


def rank(name: str, candidates: list[EntityCandidate]) -> list[EntityCandidate]:
    """Exact name or alias first, then the share of the query's words in the name, then
    shorter names (closer to the query)."""

    def score(entity: EntityCandidate) -> tuple[bool, float, int, str]:
        exact = fold(name) in _names(entity)
        return (not exact, -overlap(name, entity), len(entity.name), entity.name)

    return sorted(candidates, key=score)
