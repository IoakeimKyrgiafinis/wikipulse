"""The knowledge graph in Neo4j: what is a node, what is a relationship, how writes work.

```
(:Entity {key, wikidata_id, name, type, link_method, aliases})
    -[:INVOLVED_IN {type, source_title, source_revision, extracted_at, confidence, ...}]->
(:Event {key, run_id, number, title, description, started_at, ended_at, ...})
    <-[:PART_OF {revision, first_edit_at, last_edit_at, score}]-
(:Page {title, wikidata_id, description, summary})

(:Entity)-[:RELATED_TO {relation, event_key, source_title, source_revision,
                        extracted_at, confidence, ...}]->(:Entity)
```

- **One entity per Wikidata ID** (requirement 2): a linked entity's `key` *is* its
  Wikidata ID, and both `key` and `wikidata_id` have uniqueness constraints, so two
  names for Q76 can only ever reach the same node (its `aliases` collect the names). An
  unlinked entity's key is `name:<normalized name>`.
- **Idempotent writes** (requirement 1): `write_event()` runs in one transaction. It
  removes the event's previous relationships, then MERGEs (match or create) every node
  and relationship again, then deletes entities and pages the event alone held and no
  longer mentions. Writing the same extraction twice leaves the graph as it was;
  writing a new extraction replaces the old one.
- **Provenance**: every INVOLVED_IN and RELATED_TO stores the page and revision it was
  extracted from, when (wall clock: metadata only), with which model and prompt, and
  the model's confidence.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any

from neo4j import Driver, GraphDatabase, ManagedTransaction

from wikipulse.config import Settings
from wikipulse.graph.extract import Extraction
from wikipulse.graph.inputs import EventInput, EventPage
from wikipulse.graph.link import UNLINKED, Link
from wikipulse.graph.names import normalize_name, without_qualifier

logger = logging.getLogger(__name__)

SCHEMA = [
    "CREATE CONSTRAINT entity_key IF NOT EXISTS FOR (n:Entity) REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT entity_wikidata_id IF NOT EXISTS "
    "FOR (n:Entity) REQUIRE n.wikidata_id IS UNIQUE",
    "CREATE CONSTRAINT event_key IF NOT EXISTS FOR (n:Event) REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT page_title IF NOT EXISTS FOR (n:Page) REQUIRE n.title IS UNIQUE",
    "CREATE INDEX event_run_id IF NOT EXISTS FOR (n:Event) ON (n.run_id)",
    "CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name)",
]

# --- Removing an event's previous facts (each returns the nodes it may have orphaned) ---
DROP_INVOLVED = """
MATCH (n:Entity)-[r:INVOLVED_IN]->(:Event {key: $key})
DELETE r
RETURN collect(n.key) AS keys
"""
DROP_RELATED = """
MATCH (a:Entity)-[r:RELATED_TO {event_key: $key}]->(b:Entity)
DELETE r
RETURN collect(a.key) + collect(b.key) AS keys
"""
DROP_PART_OF = """
MATCH (p:Page)-[r:PART_OF]->(:Event {key: $key})
DELETE r
RETURN collect(p.title) AS titles
"""
DROP_ORPHANS = """
MATCH (n:Entity) WHERE n.key IN $keys AND NOT (n)--() DELETE n
WITH count(*) AS ignored
MATCH (p:Page) WHERE p.title IN $titles AND NOT (p)--() DELETE p
"""

# --- Writing an event --------------------------------------------------------------------
MERGE_EVENT = """
MERGE (ev:Event {key: $key})
SET ev += $props
"""
MERGE_PAGES = """
MATCH (ev:Event {key: $key})
UNWIND $pages AS page
MERGE (p:Page {title: page.title})
SET p.wikidata_id = page.wikidata_id, p.description = page.description,
    p.summary = page.summary
MERGE (p)-[r:PART_OF]->(ev)
SET r.revision = page.revision, r.first_edit_at = page.first_edit_at,
    r.last_edit_at = page.last_edit_at, r.score = page.score
"""
MERGE_ENTITIES = """
MATCH (ev:Event {key: $key})
UNWIND $entities AS e
MERGE (n:Entity {key: e.key})
SET n.name = e.name, n.wikidata_id = e.wikidata_id,
    n.linked = e.wikidata_id IS NOT NULL, n.link_method = e.link_method,
    n.aliases = coalesce(n.aliases, []) + [a IN e.aliases WHERE NOT a IN coalesce(n.aliases, [])]
MERGE (n)-[r:INVOLVED_IN]->(ev)
SET r += $provenance, r.source_title = e.source_title, r.source_revision = e.source_revision,
    r.confidence = e.confidence, r.type = e.type
"""
# An entity's type is the one most of its events gave it (ties: alphabetical), so it
# doesn't depend on which event was written last.
RETYPE = """
MATCH (n:Entity) WHERE n.key IN $keys
CALL (n) {
  MATCH (n)-[r:INVOLVED_IN]->(:Event)
  WITH r.type AS type, count(*) AS mentions
  ORDER BY mentions DESC, type
  LIMIT 1
  RETURN type
}
SET n.type = type
"""
MERGE_RELATIONS = """
UNWIND $relations AS rel
MATCH (a:Entity {key: rel.subject}), (b:Entity {key: rel.object})
MERGE (a)-[r:RELATED_TO {event_key: $key, relation: rel.relation}]->(b)
SET r += $provenance, r.source_title = rel.source_title,
    r.source_revision = rel.source_revision, r.confidence = rel.confidence
"""
DELETE_EVENT = "MATCH (ev:Event {key: $key}) DETACH DELETE ev"


@dataclass(frozen=True)
class GraphEntity:
    key: str
    wikidata_id: str | None
    name: str
    type: str
    link_method: str
    aliases: list[str]
    source_title: str
    source_revision: int | None
    confidence: float


@dataclass(frozen=True)
class GraphRelation:
    subject: str  # entity key
    relation: str
    object: str  # entity key
    source_title: str
    source_revision: int | None
    confidence: float


@dataclass
class GraphEvent:
    """Everything `write_event()` stores for one event, ready for Cypher parameters."""

    key: str
    props: dict[str, Any]
    pages: list[dict[str, Any]]
    entities: list[GraphEntity] = field(default_factory=list)
    relations: list[GraphRelation] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


def entity_key(name: str, link: Link) -> str:
    """The node key: the Wikidata ID if linked, else the normalized name."""
    return link.wikidata_id or f"name:{normalize_name(name)}"


def page_props(page: EventPage) -> dict[str, Any]:
    """A page's properties on its `Page` node."""
    return {
        "title": page.title,
        "wikidata_id": page.wikidata_id,
        "description": page.description,
        "summary": page.summary,
        "revision": page.revision,
        "first_edit_at": page.first_edit_at,
        "last_edit_at": page.last_edit_at,
        "score": page.score,
    }


def build_graph_event(
    event: EventInput,
    pages: Sequence[EventPage],
    extraction: Extraction,
    links: Mapping[str, Link],
    extracted_at: datetime,
) -> GraphEvent:
    """Combine an event, its extraction and the links into nodes and relationships.

    Names that linked to the same Wikidata ID become one entity (the first, most
    important mention keeps its type; the best confidence wins; all names are aliases).
    """
    entities: dict[str, GraphEntity] = {}
    key_of: dict[str, str] = {}
    for mention in extraction.entities:
        link = links.get(mention.name, UNLINKED)
        key = entity_key(mention.name, link)
        key_of[mention.name] = key
        found = entities.get(key)
        if found is None:
            entities[key] = GraphEntity(
                key=key,
                wikidata_id=link.wikidata_id,
                name=without_qualifier(link.label or mention.name),
                type=mention.type,
                link_method=link.method,
                aliases=[mention.name],
                source_title=mention.source_title,
                source_revision=mention.source_revision,
                confidence=mention.confidence,
            )
            continue
        updates: dict[str, Any] = {}
        if mention.name not in found.aliases:
            updates["aliases"] = [*found.aliases, mention.name]
        if mention.confidence > found.confidence:
            updates["source_title"] = mention.source_title
            updates["source_revision"] = mention.source_revision
            updates["confidence"] = mention.confidence
        entities[key] = replace(found, **updates)
    relations: dict[tuple[str, str, str], GraphRelation] = {}
    for fact in extraction.relations:
        subject, object_ = key_of.get(fact.subject), key_of.get(fact.object)
        if subject is None or object_ is None or subject == object_:
            continue  # e.g. two names for the same Wikidata item
        relations.setdefault(
            (subject, fact.relation, object_),
            GraphRelation(
                subject,
                fact.relation,
                object_,
                fact.source_title,
                fact.source_revision,
                fact.confidence,
            ),
        )
    provenance = {
        "extracted_at": extracted_at,
        "model": extraction.model,
        "prompt_version": extraction.prompt_version,
    }
    props = {
        "run_id": event.run_id,
        "number": event.number,
        "status": event.status,
        "title": extraction.title,
        "description": extraction.description,
        "provisional_title": event.title,
        "started_at": event.started_at,
        "ended_at": event.ended_at,
        "page_count": len(event.pages),
        "correlation_id": event.correlation_id,
        **provenance,
    }
    return GraphEvent(
        key=event.key,
        props=props,
        pages=[page_props(page) for page in pages],
        entities=list(entities.values()),
        relations=list(relations.values()),
        provenance=provenance,
    )


def _drop_event_facts(tx: ManagedTransaction, key: str) -> tuple[list[str], list[str]]:
    """Delete the event's relationships; return the entity keys and page titles touched."""
    keys = tx.run(DROP_INVOLVED, key=key).single()["keys"]
    keys += tx.run(DROP_RELATED, key=key).single()["keys"]
    titles = tx.run(DROP_PART_OF, key=key).single()["titles"]
    return keys, titles


def _write_event(tx: ManagedTransaction, event: GraphEvent) -> None:
    old_keys, old_titles = _drop_event_facts(tx, event.key)
    tx.run(MERGE_EVENT, key=event.key, props=event.props).consume()
    tx.run(MERGE_PAGES, key=event.key, pages=event.pages).consume()
    tx.run(
        MERGE_ENTITIES,
        key=event.key,
        entities=[asdict(e) for e in event.entities],
        provenance=event.provenance,
    ).consume()
    tx.run(
        MERGE_RELATIONS,
        key=event.key,
        relations=[asdict(r) for r in event.relations],
        provenance=event.provenance,
    ).consume()
    tx.run(DROP_ORPHANS, keys=old_keys, titles=old_titles).consume()
    keys = sorted(set(old_keys) | {entity.key for entity in event.entities})
    tx.run(RETYPE, keys=keys).consume()


def _delete_event(tx: ManagedTransaction, key: str) -> None:
    old_keys, old_titles = _drop_event_facts(tx, key)
    tx.run(DELETE_EVENT, key=key).consume()
    tx.run(DROP_ORPHANS, keys=old_keys, titles=old_titles).consume()
    tx.run(RETYPE, keys=sorted(set(old_keys))).consume()


@dataclass(frozen=True)
class GraphCounts:
    """Size of the graph for one grouping run."""

    events: int
    entities: int
    linked: int
    by_method: dict[str, int]
    relations: int

    @property
    def linked_share(self) -> float:
        return self.linked / self.entities if self.entities else 0.0


RUN_COUNTS = """
MATCH (ev:Event {run_id: $run_id})
OPTIONAL MATCH (n:Entity)-[:INVOLVED_IN]->(ev)
WITH count(DISTINCT ev) AS events, collect(DISTINCT n) AS entities
RETURN events, size(entities) AS total,
       [m IN ['page', 'link', 'search', 'none'] |
        size([n IN entities WHERE n.link_method = m])] AS by_method
"""
RUN_RELATIONS = """
MATCH (ev:Event {run_id: $run_id})
MATCH (:Entity)-[r:RELATED_TO {event_key: ev.key}]->(:Entity)
RETURN count(r) AS relations
"""


class GraphStore:
    """Reads and writes the knowledge graph through one Neo4j driver."""

    def __init__(self, driver: Driver, database: str | None = None) -> None:
        self.driver = driver
        self.database = database

    @classmethod
    def from_settings(cls, settings: Settings) -> "GraphStore":
        auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
        return cls(GraphDatabase.driver(settings.neo4j_uri, auth=auth))

    def close(self) -> None:
        self.driver.close()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def ensure_schema(self) -> None:
        """Create the constraints and indexes (does nothing if they exist)."""
        with self.driver.session(database=self.database) as session:
            for statement in SCHEMA:
                session.run(statement).consume()

    def write_event(self, event: GraphEvent) -> None:
        """Replace everything the graph holds about `event`, in one transaction."""
        with self.driver.session(database=self.database) as session:
            session.execute_write(_write_event, event)

    def delete_event(self, key: str) -> None:
        """Remove an event (e.g. merged into another) and whatever only it held."""
        with self.driver.session(database=self.database) as session:
            session.execute_write(_delete_event, key)

    def event_keys(self, run_id: str) -> list[str]:
        """Keys of the run's events in the graph."""
        with self.driver.session(database=self.database) as session:
            result = session.run(
                "MATCH (ev:Event {run_id: $run_id}) RETURN ev.key AS key", run_id=run_id
            )
            return [record["key"] for record in result]

    def run_counts(self, run_id: str) -> GraphCounts:
        """Events, distinct entities (linked, by link method) and relations of a run."""
        with self.driver.session(database=self.database) as session:
            row = session.run(RUN_COUNTS, run_id=run_id).single()
            relations = session.run(RUN_RELATIONS, run_id=run_id).single()["relations"]
        methods = dict(zip(["page", "link", "search", "none"], row["by_method"], strict=True))
        linked = row["total"] - methods["none"]
        return GraphCounts(row["events"], row["total"], linked, methods, relations)
