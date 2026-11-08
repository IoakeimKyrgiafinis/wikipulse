"""Reading the knowledge graph for question answering, always in READ transactions.

Every query here runs in a session opened with `READ_ACCESS`: Neo4j refuses to write in
it ("Writing in read access mode not allowed"), whatever the query says. The only
writes of the QA package (indexes and embeddings) live in `wikipulse.qa.index`.

Indexes this module reads (created by `uv run python -m wikipulse.qa.index`):
- `qa_entity_names`: full-text index over entity names and aliases, with Lucene's
  `standard-folding` analyzer, so "pena" finds "Santiago Peña";
- `qa_page_titles`: full-text index over page titles;
- `qa_page_embedding`, `qa_event_embedding`: vector indexes (cosine) over the
  embeddings of page summaries and of event titles and descriptions.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import neo4j
from neo4j import Driver, GraphDatabase, Query
from neo4j.graph import Node, Path, Relationship
from neo4j.time import Date, DateTime, Duration, Time

from wikipulse.config import Settings

logger = logging.getLogger(__name__)

ENTITY_NAMES_INDEX = "qa_entity_names"
PAGE_TITLES_INDEX = "qa_page_titles"
PAGE_VECTOR_INDEX = "qa_page_embedding"
EVENT_VECTOR_INDEX = "qa_event_embedding"
MAX_VALUE_CHARS = 300  # longer strings from a generated query are cut
HIDDEN_PROPERTIES = {"embedding", "embedding_hash", "embedding_model"}

_TERM = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class EntityCandidate:
    key: str
    name: str
    aliases: tuple[str, ...]
    type: str | None
    wikidata_id: str | None


@dataclass(frozen=True)
class Source:
    title: str
    revision: int | None


@dataclass(frozen=True)
class EventInfo:
    key: str
    title: str | None
    description: str | None
    started_at: datetime | None
    page_count: int | None
    pages: tuple[Source, ...] = ()  # highest spike score first


@dataclass(frozen=True)
class Involvement:
    entity_key: str
    entity_name: str
    type: str | None
    event_key: str
    event_title: str | None
    source: Source


@dataclass(frozen=True)
class Relation:
    subject: str
    relation: str
    object: str
    event_key: str | None
    event_title: str | None
    source: Source


@dataclass(frozen=True)
class PathStep:
    """One hop of a path: `start` -[label]-> `end`, as read from the graph."""

    start: str
    label: str  # "involved in" or the relation phrase
    end: str
    source: Source | None


@dataclass(frozen=True)
class GraphPath:
    names: tuple[str, ...]  # the nodes along the path, from the first entity
    steps: tuple[PathStep, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PageHit:
    title: str
    description: str | None
    summary: str | None
    revision: int | None
    event_key: str
    event_title: str | None
    cosine: float


@dataclass(frozen=True)
class EventHit:
    key: str
    cosine: float


def lucene_terms(text: str, stopwords: frozenset[str]) -> str:
    """A full-text query that finds names sharing any word with `text`."""
    terms = [t.lower() for t in _TERM.findall(text)]
    kept = list(dict.fromkeys(t for t in terms if len(t) > 1 and t not in stopwords))
    return " OR ".join(kept)


def cosine_from_score(score: float) -> float:
    """Neo4j's vector index reports (1 + cosine) / 2 in [0, 1]; we think in cosines."""
    return 2 * score - 1


def plain_value(value: Any) -> Any:
    """A value from a query result as plain Python (no embeddings, no huge strings)."""
    if isinstance(value, Node):
        props = {k: plain_value(v) for k, v in value.items() if k not in HIDDEN_PROPERTIES}
        return {"labels": sorted(value.labels), **props}
    if isinstance(value, Relationship):
        props = {k: plain_value(v) for k, v in value.items() if k not in HIDDEN_PROPERTIES}
        return {"type": value.type, **props}
    if isinstance(value, Path):
        return [plain_value(node) for node in value.nodes]
    if isinstance(value, DateTime | Date | Time):
        return value.iso_format()
    if isinstance(value, Duration):
        return str(value)
    if isinstance(value, list):
        return [plain_value(v) for v in value]
    if isinstance(value, dict):
        return {k: plain_value(v) for k, v in value.items() if k not in HIDDEN_PROPERTIES}
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[: MAX_VALUE_CHARS - 1] + "…"
    return value


def _native(value: Any) -> Any:
    return value.to_native() if isinstance(value, DateTime) else value


def _source(title: Any, revision: Any) -> Source:
    return Source(str(title or ""), int(revision) if revision is not None else None)


# --- Queries ------------------------------------------------------------------------------
CLOCK = """
MATCH (ev:Event)
RETURN max(ev.started_at) AS started, max(ev.ended_at) AS ended
"""
SEARCH_ENTITIES = f"""
CALL db.index.fulltext.queryNodes('{ENTITY_NAMES_INDEX}', $query, {{limit: $limit}})
YIELD node
RETURN node.key AS key, node.name AS name, coalesce(node.aliases, []) AS aliases,
       node.type AS type, node.wikidata_id AS wikidata_id
"""
SEARCH_PAGES = f"""
CALL db.index.fulltext.queryNodes('{PAGE_TITLES_INDEX}', $query, {{limit: $limit}})
YIELD node
RETURN node.title AS title
"""
EVENTS = """
MATCH (ev:Event) WHERE ev.key IN $keys
OPTIONAL MATCH (p:Page)-[po:PART_OF]->(ev)
WITH ev, p, po ORDER BY po.score DESC
WITH ev, [x IN collect({title: p.title, revision: po.revision}) WHERE x.title IS NOT NULL]
     AS pages
RETURN ev.key AS key, ev.title AS title, ev.description AS description,
       ev.started_at AS started_at, ev.page_count AS page_count, pages
ORDER BY ev.started_at DESC
"""
EVENTS_BETWEEN = """
MATCH (ev:Event) WHERE ev.started_at >= $start AND ev.started_at < $end
RETURN ev.key AS key ORDER BY ev.page_count DESC, ev.started_at DESC LIMIT $limit
"""
EVENTS_BY_NUMBER = """
MATCH (ev:Event) WHERE ev.number IN $numbers
RETURN ev.key AS key ORDER BY ev.started_at DESC
"""
PAGE_EVENTS = """
MATCH (p:Page)-[:PART_OF]->(ev:Event) WHERE p.title IN $titles
RETURN p.title AS title, collect(ev.key) AS keys
"""
ENTITY_EVENTS = """
MATCH (n:Entity {key: $key})-[r:INVOLVED_IN]->(ev:Event)
RETURN n.key AS entity_key, n.name AS entity_name, r.type AS type, ev.key AS event_key,
       ev.title AS event_title, r.source_title AS source_title,
       r.source_revision AS source_revision
ORDER BY ev.page_count DESC, ev.started_at DESC LIMIT $limit
"""
EVENT_ENTITIES = """
MATCH (n:Entity)-[r:INVOLVED_IN]->(ev:Event {key: $key})
RETURN n.key AS entity_key, n.name AS entity_name, r.type AS type, ev.key AS event_key,
       ev.title AS event_title, r.source_title AS source_title,
       r.source_revision AS source_revision
ORDER BY r.confidence DESC, n.name LIMIT $limit
"""
ENTITY_RELATIONS = """
MATCH (n:Entity {key: $key})-[r:RELATED_TO]-(m:Entity)
OPTIONAL MATCH (ev:Event {key: r.event_key})
RETURN startNode(r).name AS subject, r.relation AS relation, endNode(r).name AS object,
       r.event_key AS event_key, ev.title AS event_title,
       r.source_title AS source_title, r.source_revision AS source_revision
ORDER BY r.confidence DESC, ev.page_count DESC LIMIT $limit
"""
EVENT_RELATIONS = """
MATCH (a:Entity)-[r:RELATED_TO {event_key: $key}]->(b:Entity)
OPTIONAL MATCH (ev:Event {key: $key})
RETURN a.name AS subject, r.relation AS relation, b.name AS object,
       r.event_key AS event_key, ev.title AS event_title,
       r.source_title AS source_title, r.source_revision AS source_revision
ORDER BY r.confidence DESC LIMIT $limit
"""
PATHS = """
MATCH (a:Entity {key: $a}), (b:Entity {key: $b})
MATCH p = allShortestPaths((a)-[:RELATED_TO|INVOLVED_IN*..%d]-(b))
RETURN [n IN nodes(p) | coalesce(n.name, n.title)] AS names,
       [r IN relationships(p) | {
          start: coalesce(startNode(r).name, startNode(r).title),
          end: coalesce(endNode(r).name, endNode(r).title),
          label: CASE type(r) WHEN 'INVOLVED_IN' THEN 'involved in' ELSE r.relation END,
          source_title: r.source_title, source_revision: r.source_revision}] AS steps
LIMIT $limit
"""
SIMILAR_PAGES = f"""
CALL db.index.vector.queryNodes('{PAGE_VECTOR_INDEX}', $k, $vector) YIELD node AS p, score
MATCH (p)-[po:PART_OF]->(ev:Event)
RETURN p.title AS title, p.description AS description, p.summary AS summary,
       po.revision AS revision, ev.key AS event_key, ev.title AS event_title, score
ORDER BY score DESC
"""
SIMILAR_EVENTS = f"""
CALL db.index.vector.queryNodes('{EVENT_VECTOR_INDEX}', $k, $vector) YIELD node, score
RETURN node.key AS key, score ORDER BY score DESC
"""


class QAGraph:
    """Read-only access to the knowledge graph for retrieval."""

    def __init__(self, driver: Driver, database: str | None = None) -> None:
        self.driver = driver
        self.database = database

    @classmethod
    def from_settings(cls, settings: Settings) -> "QAGraph":
        auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
        return cls(GraphDatabase.driver(settings.neo4j_uri, auth=auth))

    def close(self) -> None:
        self.driver.close()

    def __enter__(self) -> "QAGraph":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _session(self) -> neo4j.Session:
        return self.driver.session(database=self.database, default_access_mode=neo4j.READ_ACCESS)

    def _read(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        with self._session() as session:
            return [record.data() for record in session.run(cypher, params)]

    # --- Time -----------------------------------------------------------------------------
    def clock(self) -> datetime | None:
        """The graph's "now": the latest start or end of any event (event time)."""
        row = self._read(CLOCK)[0]
        times = [_native(t) for t in (row["started"], row["ended"]) if t is not None]
        return max(times) if times else None

    # --- Finding what the question names ----------------------------------------------------
    def search_entities(self, query: str, limit: int = 100) -> list[EntityCandidate]:
        if not query:
            return []
        rows = self._read(SEARCH_ENTITIES, query=query, limit=limit)
        return [
            EntityCandidate(r["key"], r["name"], tuple(r["aliases"]), r["type"], r["wikidata_id"])
            for r in rows
        ]

    def search_page_titles(self, query: str, limit: int = 50) -> list[str]:
        if not query:
            return []
        return [r["title"] for r in self._read(SEARCH_PAGES, query=query, limit=limit)]

    def page_events(self, titles: list[str]) -> dict[str, list[str]]:
        rows = self._read(PAGE_EVENTS, titles=titles)
        return {r["title"]: list(r["keys"]) for r in rows}

    def events_between(self, start: datetime, end: datetime, limit: int = 15) -> list[str]:
        """Keys of the events that started in [start, end), biggest first."""
        rows = self._read(EVENTS_BETWEEN, start=start, end=end, limit=limit)
        return [r["key"] for r in rows]

    def events_by_number(self, numbers: list[int]) -> list[str]:
        return [r["key"] for r in self._read(EVENTS_BY_NUMBER, numbers=numbers)]

    # --- Traversal ----------------------------------------------------------------------------
    def events(self, keys: list[str]) -> list[EventInfo]:
        rows = self._read(EVENTS, keys=keys)
        return [
            EventInfo(
                r["key"],
                r["title"],
                r["description"],
                _native(r["started_at"]),
                r["page_count"],
                tuple(_source(p["title"], p["revision"]) for p in r["pages"]),
            )
            for r in rows
        ]

    def _involvements(self, cypher: str, key: str, limit: int) -> list[Involvement]:
        return [
            Involvement(
                r["entity_key"],
                r["entity_name"],
                r["type"],
                r["event_key"],
                r["event_title"],
                _source(r["source_title"], r["source_revision"]),
            )
            for r in self._read(cypher, key=key, limit=limit)
        ]

    def entity_events(self, key: str, limit: int = 10) -> list[Involvement]:
        return self._involvements(ENTITY_EVENTS, key, limit)

    def event_entities(self, key: str, limit: int = 25) -> list[Involvement]:
        return self._involvements(EVENT_ENTITIES, key, limit)

    def _relations(self, cypher: str, key: str, limit: int) -> list[Relation]:
        return [
            Relation(
                r["subject"],
                r["relation"],
                r["object"],
                r["event_key"],
                r["event_title"],
                _source(r["source_title"], r["source_revision"]),
            )
            for r in self._read(cypher, key=key, limit=limit)
        ]

    def entity_relations(self, key: str, limit: int = 15) -> list[Relation]:
        return self._relations(ENTITY_RELATIONS, key, limit)

    def event_relations(self, key: str, limit: int = 20) -> list[Relation]:
        return self._relations(EVENT_RELATIONS, key, limit)

    def paths(self, a: str, b: str, max_hops: int = 4, limit: int = 3) -> list[GraphPath]:
        """The shortest paths (at most `max_hops` steps) between two entities."""
        rows = self._read(PATHS % int(max_hops), a=a, b=b, limit=limit)
        paths = []
        for row in rows:
            steps = tuple(
                PathStep(
                    s["start"],
                    s["label"],
                    s["end"],
                    _source(s["source_title"], s["source_revision"]) if s["source_title"] else None,
                )
                for s in row["steps"]
            )
            paths.append(GraphPath(tuple(row["names"]), steps))
        return paths

    # --- Vector search ------------------------------------------------------------------------
    def similar_pages(self, vector: list[float], k: int) -> list[PageHit]:
        rows = self._read(SIMILAR_PAGES, k=k, vector=vector)
        return [
            PageHit(
                r["title"],
                r["description"],
                r["summary"],
                r["revision"],
                r["event_key"],
                r["event_title"],
                cosine_from_score(r["score"]),
            )
            for r in rows
        ]

    def similar_events(self, vector: list[float], k: int) -> list[EventHit]:
        rows = self._read(SIMILAR_EVENTS, k=k, vector=vector)
        return [EventHit(r["key"], cosine_from_score(r["score"])) for r in rows]

    # --- Generated queries ------------------------------------------------------------------
    def run_query(
        self, cypher: str, params: dict[str, Any], timeout_s: float, max_rows: int
    ) -> list[dict[str, Any]]:
        """Run an already validated query: read-only session, timeout, row cap."""
        rows: list[dict[str, Any]] = []
        with self._session() as session:
            result = session.run(Query(cypher, timeout=timeout_s), params)
            for record in result:
                rows.append({key: plain_value(value) for key, value in record.items()})
                if len(rows) >= max_rows:
                    break
            result.consume()
        return rows
