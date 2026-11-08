"""Retrieval for GraphRAG: `retrieve(question) -> Context`.

Three channels each add facts (`wikipulse.qa.types.Fact`) with their sources:

- **graph** (traversal): the entities the question names (`mentions.py`), their events
  and relations, the shortest paths between each pair of them, and the events the
  question points at (by number, by a page title, or found by vector search), with
  their entities and relations;
- **cypher**: one read-only query written by the LLM (`cypher.py`) for questions about
  structure: lists, counts, time ranges ("which organizations appear in more than one
  event this week?");
- **vector**: the page summaries and events whose embeddings are closest to the
  question's (Neo4j vector indexes, see `index.py`).

Each channel can be switched off (`channels=`), which is how spec 05b measures what
each one adds. A failing channel (bad query, missing index, LLM down) leaves a note and
the others go on.

"Now" is event time: the newest event in the graph (`QAGraph.clock()`), or `as_of`.
"Today" is that day (UTC), "this week" the seven days before it. A replayed day thus
answers "what happened today?" about the replayed day, not about the wall-clock date.
"""

import logging
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import numpy as np
from neo4j.exceptions import Neo4jError

from wikipulse.config import Settings, get_settings
from wikipulse.llm import LLMError
from wikipulse.qa.cypher import CypherWriter, TimeParameters, UnsafeCypherError, validate_cypher
from wikipulse.qa.graph import (
    EntityCandidate,
    EventHit,
    EventInfo,
    GraphPath,
    Involvement,
    PageHit,
    Relation,
    Source,
    lucene_terms,
)
from wikipulse.qa.index import QUERY_INSTRUCTION
from wikipulse.qa.mentions import STOPWORDS, event_references, find_mentions, find_titles
from wikipulse.qa.types import Channel, Citation, Context, EntityRef, Fact

logger = logging.getLogger(__name__)

ALL_CHANNELS: frozenset[Channel] = frozenset({"graph", "cypher", "vector"})
MAX_ROW_SOURCES = 3  # citations kept per row of a generated query
_EVENT_KEY = re.compile(r"^[\w.-]+:\d+$")


class GraphReader(Protocol):
    """What retrieval reads from the graph (`QAGraph`; tests pass a fake)."""

    def clock(self) -> datetime | None: ...
    def search_entities(self, query: str, limit: int = 100) -> list[EntityCandidate]: ...
    def search_page_titles(self, query: str, limit: int = 50) -> list[str]: ...
    def page_events(self, titles: list[str]) -> dict[str, list[str]]: ...
    def events_by_number(self, numbers: list[int]) -> list[str]: ...
    def events_between(self, start: datetime, end: datetime, limit: int = 15) -> list[str]: ...
    def events(self, keys: list[str]) -> list[EventInfo]: ...
    def entity_events(self, key: str, limit: int = 10) -> list[Involvement]: ...
    def event_entities(self, key: str, limit: int = 25) -> list[Involvement]: ...
    def entity_relations(self, key: str, limit: int = 15) -> list[Relation]: ...
    def event_relations(self, key: str, limit: int = 20) -> list[Relation]: ...
    def paths(self, a: str, b: str, max_hops: int = 4, limit: int = 3) -> list[GraphPath]: ...
    def similar_pages(self, vector: list[float], k: int) -> list[PageHit]: ...
    def similar_events(self, vector: list[float], k: int) -> list[EventHit]: ...
    def run_query(
        self, cypher: str, params: dict[str, Any], timeout_s: float, max_rows: int
    ) -> list[dict[str, Any]]: ...


class QuestionEmbedder(Protocol):
    def embed(self, text: str) -> np.ndarray: ...


@dataclass(frozen=True)
class RetrievalConfig:
    max_rows: int = 25  # rows of a generated query
    timeout_s: float = 5.0  # of a generated query
    top_k: int = 5  # vector hits per index
    min_cosine: float = 0.63  # vector hits below this are ignored
    max_facts: int = 60
    max_entities: int = 5  # entities of the question that are expanded
    max_seed_events: int = 3  # events that are expanded with their entities and relations
    max_hops: int = 4  # longest connection looked for between two entities
    entity_events: int = 8
    entity_relations: int = 10
    event_entities: int = 20
    event_relations: int = 15
    period_events: int = 15  # events listed for "today", "this week", ...

    @classmethod
    def from_settings(cls, settings: Settings) -> "RetrievalConfig":
        return cls(
            max_rows=settings.qa_cypher_max_rows,
            timeout_s=settings.qa_cypher_timeout_s,
            top_k=settings.qa_vector_top_k,
            min_cosine=settings.qa_vector_min_cosine,
            max_facts=settings.qa_max_facts,
        )


_TODAY = re.compile(r"\b(today|tonight|this (morning|afternoon|evening))\b", re.I)
_YESTERDAY = re.compile(r"\byesterday\b", re.I)
_RECENT = re.compile(
    r"\b(this|past|last) (week|few days)\b|\b(recent|recently|latest|lately|currently)\b", re.I
)


_GENERAL = re.compile(r"\b(happen\w*|news|going on|new|latest|stories|story)\b", re.I)


def time_window(question: str, times: TimeParameters) -> tuple[datetime, datetime, str] | None:
    """The period of a general question ("what happened today?", "any news this week?"),
    if it is one. Questions about something specific ("which organizations ... this week")
    are left to the other channels."""
    if not _GENERAL.search(question):
        return None
    day = timedelta(days=1)
    if _TODAY.search(question):
        return times.today_start, times.today_start + day, "today"
    if _YESTERDAY.search(question):
        return times.today_start - day, times.today_start, "yesterday"
    if _RECENT.search(question):
        return times.week_start, times.today_start + day, "the past week"
    return None


def time_parameters(now: datetime) -> TimeParameters:
    now = now.astimezone(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return TimeParameters(now=now, today_start=today, week_start=now - timedelta(days=7))


# --- Turning graph records into facts --------------------------------------------------------


def _citation(source: Source | None) -> list[Citation]:
    if source is None or not source.title:
        return []
    return [Citation(title=source.title, revision=source.revision)]


def _when(moment: datetime | None) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if moment else "unknown time"


def event_fact(event: EventInfo, channel: Channel, max_sources: int = 2) -> Fact:
    pages = f", {event.page_count} pages" if event.page_count else ""
    description = f": {event.description}" if event.description else ""
    text = (
        f'Event "{event.title}" ({event.key}, started {_when(event.started_at)}{pages})'
        f"{description}"
    )
    sources = [c for page in event.pages[:max_sources] for c in _citation(page)]
    return Fact(kind="event", channel=channel, text=text, sources=sources, key=f"event:{event.key}")


def involvement_fact(item: Involvement, channel: Channel) -> Fact:
    type_ = f" ({item.type})" if item.type else ""
    text = (
        f'{item.entity_name}{type_} is involved in the event "{item.event_title}" '
        f"({item.event_key})"
    )
    return Fact(
        kind="involvement",
        channel=channel,
        text=text,
        sources=_citation(item.source),
        key=f"involved:{item.entity_key}:{item.event_key}",
    )


def relation_fact(item: Relation, channel: Channel) -> Fact:
    where = f' (in the event "{item.event_title}", {item.event_key})' if item.event_key else ""
    text = f"{item.subject} — {item.relation} — {item.object}{where}"
    key = f"relation:{item.subject}:{item.relation}:{item.object}:{item.event_key}"
    return Fact(
        kind="relation", channel=channel, text=text, sources=_citation(item.source), key=key
    )


def path_text(path: GraphPath) -> str:
    """ "A —leader of→ B ←involved in— C": every step with its direction."""
    parts = [path.names[0]]
    for index, step in enumerate(path.steps):
        here, there = path.names[index], path.names[index + 1]
        if step.start == here and step.end == there:
            parts.append(f" —{step.label}→ {there}")
        else:
            parts.append(f" ←{step.label}— {there}")
    return "".join(parts)


def path_fact(a: str, b: str, path: GraphPath, channel: Channel) -> Fact:
    sources: list[Citation] = []
    for step in path.steps:
        for citation in _citation(step.source):
            if citation not in sources:
                sources.append(citation)
    text = f"Connection between {a} and {b}: {path_text(path)}"
    return Fact(kind="path", channel=channel, text=text, sources=sources, key=f"path:{text}")


def page_fact(hits: Sequence[PageHit], channel: Channel, max_summary: int = 400) -> Fact:
    """One fact per page, naming every event the page is part of (one hit per event)."""
    hit = hits[0]
    description = f" ({hit.description})" if hit.description else ""
    summary = (hit.summary or "").strip()
    if len(summary) > max_summary:
        summary = summary[: max_summary - 1] + "…"
    events = " and ".join(f'"{h.event_title}" ({h.event_key})' for h in hits)
    noun = "events" if len(hits) > 1 else "event"
    text = f'Wikipedia page "{hit.title}"{description}, part of the {noun} {events}: {summary}'
    sources: list[Citation] = []
    for each in hits:
        for citation in _citation(Source(each.title, each.revision)):
            if citation not in sources:
                sources.append(citation)
    return Fact(kind="page", channel=channel, text=text, sources=sources, key=f"page:{hit.title}")


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, list):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)


def row_event_keys(row: dict[str, Any]) -> list[str]:
    """Event keys anywhere in a result row ("03b-default-fix:341", or an Event node)."""
    keys = []
    for value in _walk(row):
        if isinstance(value, str) and _EVENT_KEY.match(value):
            keys.append(value)
        elif isinstance(value, dict) and "Event" in value.get("labels", []) and "key" in value:
            keys.append(str(value["key"]))
    return list(dict.fromkeys(keys))


def row_sources(row: dict[str, Any]) -> list[Citation]:
    """Provenance a row carries itself (relationships with source_title/revision)."""
    found = []
    for value in _walk(row):
        if isinstance(value, dict) and value.get("source_title"):
            found.append(
                Citation(title=value["source_title"], revision=value.get("source_revision"))
            )
    if row.get("source_title"):
        found.append(Citation(title=row["source_title"], revision=row.get("source_revision")))
    return list(dict.fromkeys(found))


def _show(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_show(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{k}: {_show(v)}" for k, v in value.items() if k != "labels")
        return "{" + inner + "}"
    return str(value)


def _only_event_keys(value: Any) -> bool:
    values = value if isinstance(value, list) else [value]
    return bool(values) and all(isinstance(v, str) and _EVENT_KEY.match(v) for v in values)


def query_fact(row: dict[str, Any], sources: list[Citation], channel: Channel) -> Fact:
    """One row of a generated query. Columns holding only event keys are left out of the
    text: they serve the citations, and a list of keys is noise to the answering model."""
    shown = {k: v for k, v in row.items() if not _only_event_keys(v)} or row
    text = "Query result: " + "; ".join(f"{k} = {_show(v)}" for k, v in shown.items())
    key = "query:" + "; ".join(f"{k}={_show(v)}" for k, v in row.items())
    return Fact(kind="query", channel=channel, text=text, sources=sources, key=key)


# --- The retriever ----------------------------------------------------------------------------


@dataclass
class _Found:
    """What the channels found, before it becomes facts in priority order."""

    paths: list[Fact]
    query: list[Fact]
    entity_facts: list[Fact]
    vector_pages: list[Fact]
    seed_events: list[str]  # named by the question (number, key, page title)
    vector_events: list[str]  # found by vector search (events, and pages' events)
    period_events: list[str]  # the biggest events of "today", "this week", ...
    query_events: list[str]  # events the generated query's rows name


def _listed_by_query(key: str, found: _Found) -> bool:
    """An event that only the "today"/"this week" listing found, and that a row of the
    generated query already shows: listing it twice would only crowd the context."""
    only_period = key in found.period_events and key not in found.seed_events
    return only_period and key not in found.vector_events and key in found.query_events


class Retriever:
    """Collects facts for a question from the graph, a generated query and vector search."""

    def __init__(
        self,
        graph: GraphReader,
        cypher_writer: CypherWriter | None = None,
        embedder: QuestionEmbedder | None = None,
        config: RetrievalConfig | None = None,
        channels: Iterable[Channel] = ALL_CHANNELS,
    ) -> None:
        self.graph = graph
        self.cypher_writer = cypher_writer
        self.embedder = embedder
        self.config = config or RetrievalConfig()
        self.channels = frozenset(channels)

    def retrieve(self, question: str, as_of: datetime | None = None) -> Context:
        context = Context(question=question, max_facts=self.config.max_facts)
        now = as_of or self._timed(context, "clock", self.graph.clock, None)
        context.as_of = now
        times = time_parameters(now or datetime.now(UTC))  # wall clock only for an empty graph
        found = _Found([], [], [], [], [], [], [], [])
        if "graph" in self.channels:
            self._timed(
                context, "graph", lambda: self._traverse(question, times, context, found), None
            )
        if "cypher" in self.channels and self.cypher_writer is not None:
            self._timed(context, "cypher", lambda: self._query(question, times, context, found))
        if "vector" in self.channels and self.embedder is not None:
            self._timed(context, "vector", lambda: self._vector(question, context, found))
        seeds: list[EventInfo] = []
        if found.seed_events or found.vector_events or found.period_events:
            seeds = self._timed(context, "events", lambda: self._seeds(found, context), [])
        self._assemble(context, found, seeds)
        return context

    # Each step is timed; a Neo4j error becomes a note instead of an exception.
    def _timed(self, context: Context, name: str, step: Callable[[], Any], fallback=None):
        started = time.monotonic()
        try:
            return step()
        except Neo4jError as error:
            hint = ""
            if "index" in str(error).lower():
                hint = " (run `uv run python -m wikipulse.qa.index`)"
            context.notes.append(f"{name}: Neo4j error {error.code}{hint}")
            logger.warning("%s step failed: %s", name, error)
            return fallback
        finally:
            context.seconds[name] = round(time.monotonic() - started, 3)

    def _traverse(
        self, question: str, times: TimeParameters, context: Context, found: _Found
    ) -> None:
        """The graph channel: what the question names, how it connects, what is around it."""
        terms = lucene_terms(question, STOPWORDS)
        mentions = find_mentions(question, self.graph.search_entities(terms))
        found.seed_events += self._named_events(question, terms)
        if not mentions and not found.seed_events:
            context.notes.append("No entity, page or event of the graph is named in the question.")
            self._period(question, times, context, found)
        chosen = [m.candidate for m in mentions[: self.config.max_entities]]
        context.entities = [
            EntityRef(
                key=m.candidate.key,
                name=m.candidate.name,
                type=m.candidate.type,
                wikidata_id=m.candidate.wikidata_id,
                matched=m.matched,
            )
            for m in mentions[: self.config.max_entities]
        ]
        self._connect(chosen, context, found)
        for candidate in chosen:
            found.entity_facts += self._neighbourhood(candidate.key)

    def _named_events(self, question: str, terms: str) -> list[str]:
        """Events the question names: by key, by number, or through a page title."""
        numbers, keys = event_references(question)
        events = list(keys)
        if numbers:
            events += self.graph.events_by_number(numbers)
        titles = find_titles(question, self.graph.search_page_titles(terms))
        for event_keys in self.graph.page_events(titles).values() if titles else []:
            events += event_keys
        return events

    def _period(
        self, question: str, times: TimeParameters, context: Context, found: _Found
    ) -> None:
        """For "what happened today?": the biggest events of the period."""
        window = time_window(question, times)
        if window is None:
            return
        start, end, label = window
        found.period_events += self.graph.events_between(start, end, self.config.period_events)
        context.notes.append(f"The biggest events of {label} (by pages touched) were looked up.")

    def _connect(
        self, entities: Sequence[EntityCandidate], context: Context, found: _Found
    ) -> None:
        """The shortest paths between every pair of named entities."""
        max_hops = self.config.max_hops
        for index, a in enumerate(entities):
            for b in entities[index + 1 :]:
                paths = self.graph.paths(a.key, b.key, max_hops, limit=3)
                if not paths:
                    context.notes.append(
                        f"No connection between {a.name} and {b.name} within "
                        f"{max_hops} steps in the graph."
                    )
                found.paths += [path_fact(a.name, b.name, p, "graph") for p in paths]

    def _neighbourhood(self, key: str) -> list[Fact]:
        """An entity's relations and the events it is involved in."""
        config = self.config
        facts = [
            relation_fact(r, "graph")
            for r in self.graph.entity_relations(key, config.entity_relations)
        ]
        facts += [
            involvement_fact(i, "graph")
            for i in self.graph.entity_events(key, config.entity_events)
        ]
        return facts

    def _query(self, question: str, times: TimeParameters, context: Context, found: _Found) -> None:
        assert self.cypher_writer is not None
        try:
            raw = self.cypher_writer.write(question, times, self.config.max_rows)
        except LLMError as error:
            context.notes.append(f"cypher: the model gave no query ({error})")
            return
        if not raw:
            context.notes.append("cypher: the model wrote no query for this question.")
            return
        try:
            cypher = validate_cypher(raw, self.config.max_rows)
        except UnsafeCypherError as error:
            context.cypher = raw
            context.notes.append(f"cypher: query rejected, {error}")
            return
        context.cypher = cypher
        try:
            rows = self.graph.run_query(
                cypher, times.as_params(), self.config.timeout_s, self.config.max_rows
            )
        except Neo4jError as error:
            context.notes.append(f"cypher: the query failed ({error.code})")
            logger.info("Generated query failed: %s\n%s", error, cypher)
            return
        context.cypher_rows = len(rows)
        if not rows:
            context.notes.append("cypher: the query returned no rows.")
        keys = list(dict.fromkeys(k for row in rows for k in row_event_keys(row)))
        found.query_events += keys
        pages = {event.key: event.pages for event in self.graph.events(keys)} if keys else {}
        for row in rows:
            # A row's own provenance (a relation's source revision) is the best
            # citation; otherwise the pages of the events it names.
            sources = row_sources(row)
            for key in row_event_keys(row) if not sources else []:
                for page in pages.get(key, ())[:1]:
                    citation = Citation(title=page.title, revision=page.revision)
                    if citation not in sources:
                        sources.append(citation)
            found.query.append(query_fact(row, sources[:MAX_ROW_SOURCES], "cypher"))
        if len(rows) >= self.config.max_rows:
            context.notes.append(
                f"cypher: the query returned the maximum of {len(rows)} rows; there may be more."
            )

    def _vector(self, question: str, context: Context, found: _Found) -> None:
        assert self.embedder is not None
        vector = [float(x) for x in self.embedder.embed(QUERY_INSTRUCTION + question)]
        minimum = self.config.min_cosine
        events = self.graph.similar_events(vector, self.config.top_k)
        pages = self.graph.similar_pages(vector, self.config.top_k)
        best = max([h.cosine for h in events] + [h.cosine for h in pages], default=None)
        events = [h for h in events if h.cosine >= minimum]
        pages = [h for h in pages if h.cosine >= minimum]
        if not events and not pages:
            shown = f"{best:.2f}" if best is not None else "none"
            context.notes.append(
                f"vector: nothing similar enough (best cosine {shown}, minimum {minimum})."
            )
        by_title: dict[str, list[PageHit]] = {}
        for hit in pages:
            by_title.setdefault(hit.title, []).append(hit)
        found.vector_pages += [page_fact(hits, "vector") for hits in by_title.values()]
        found.vector_events += [hit.key for hit in events]
        found.vector_events += [hit.event_key for hit in pages]

    def _seeds(self, found: _Found, context: Context) -> list[EventInfo]:
        keys = list(dict.fromkeys(found.seed_events + found.vector_events + found.period_events))
        by_key = {event.key: event for event in self.graph.events(keys)}
        ordered = [by_key[k] for k in keys if k in by_key]
        context.events = [event.key for event in ordered]
        return ordered

    def _assemble(self, context: Context, found: _Found, seeds: Sequence[EventInfo]) -> None:
        """Facts in order of priority: when the context is full, the last ones drop."""
        config = self.config
        expanded: list[Fact] = []
        named = [e for e in seeds if e.key not in found.period_events or e.key in found.seed_events]
        to_expand = named[: config.max_seed_events] if "graph" in self.channels else []
        for event in to_expand:
            expanded += [
                involvement_fact(i, "graph")
                for i in self.graph.event_entities(event.key, config.event_entities)
            ]
            expanded += [
                relation_fact(r, "graph")
                for r in self.graph.event_relations(event.key, config.event_relations)
            ]
        groups = [
            found.paths,
            found.query,
            [
                event_fact(event, "vector" if event.key in found.vector_events else "graph")
                for event in seeds
                if not _listed_by_query(event.key, found)
            ],
            found.entity_facts,
            expanded,
            found.vector_pages,
        ]
        for group in groups:
            for fact in group:
                context.add(fact)


def retrieve(
    question: str,
    as_of: datetime | None = None,
    channels: Iterable[Channel] = ALL_CHANNELS,
    settings: Settings | None = None,
) -> Context:
    """Retrieve facts for one question with everything built from settings.

    For many questions, build one `Retriever` (or use `wikipulse.qa.answer.QASession`)
    so the Neo4j driver, the LLM client and the embedding model are created once.
    """
    from wikipulse.qa.answer import QASession  # avoid a circular import

    with QASession.from_settings(settings or get_settings(), channels=channels) as session:
        return session.retriever.retrieve(question, as_of=as_of)
