"""The extraction under evaluation: events, entities and relations, from Neo4j or a file.

Two sources give the same `ExtractionData`:

- **the graph** (`load_from_graph`): what `wikipulse.graph.run` stored in Neo4j for a
  grouping run;
- **a saved re-extraction** (`load_from_file`): `wikipulse.eval.extraction extract`
  runs the same pipeline (changes, LLM, linking) with another prompt or model and saves
  the result as JSON lines under `data/extractions/`, without touching the graph. That
  is how two prompts are compared on the same events.

`--events` picks the events: numbers (`341,356`), a UTC day (`2026-11-05`) or a range of
start times (`2026-11-05T16:00:00+00:00..2026-11-05T19:00:00+00:00`).
"""

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from neo4j import Driver

from wikipulse.enrich.run import parse_day_or_time
from wikipulse.graph.store import GraphEvent

DEFAULT_EXTRACTIONS_DIR = Path("data") / "extractions"


# --- Which events -------------------------------------------------------------------


@dataclass(frozen=True)
class EventSelection:
    """Event numbers, or a range of event start times (UTC), or both open (all)."""

    numbers: tuple[int, ...] | None = None
    since: datetime | None = None
    until: datetime | None = None

    def describe(self) -> str:
        if self.numbers is not None:
            return "events " + ",".join(str(n) for n in self.numbers)
        since = self.since.isoformat() if self.since else "the start"
        until = self.until.isoformat() if self.until else "the end"
        return f"events started from {since} until {until}"

    def matches(self, number: int, started_at: datetime) -> bool:
        if self.numbers is not None and number not in self.numbers:
            return False
        if self.since is not None and started_at < self.since:
            return False
        return self.until is None or started_at < self.until


def parse_events(value: str) -> EventSelection:
    """`341,356` | `2026-11-05` (that UTC day) | `<start>..<end>` (start times)."""
    value = value.strip()
    if ".." in value:
        start, _, end = value.partition("..")
        return EventSelection(since=parse_day_or_time(start), until=parse_day_or_time(end))
    if len(value) == 10 and value[4] == "-":
        day = parse_day_or_time(value)
        return EventSelection(since=day, until=day + timedelta(days=1))
    numbers = tuple(sorted({int(part) for part in value.split(",") if part.strip()}))
    if not numbers:
        raise ValueError(f"no events in {value!r}")
    return EventSelection(numbers=numbers)


# --- What is evaluated --------------------------------------------------------------


@dataclass(frozen=True)
class EntityView:
    """One distinct entity (a graph node)."""

    key: str  # Wikidata ID, or name:<normalized name> when unlinked
    name: str
    wikidata_id: str | None
    type: str
    link_method: str  # page, link, search or none
    aliases: tuple[str, ...]  # the names the model used


@dataclass(frozen=True)
class RelationView:
    """One extracted relationship, read in one event from one page revision."""

    event_key: str
    subject: str  # entity key
    relation: str
    object: str  # entity key
    source_title: str
    source_revision: int | None
    confidence: float

    @property
    def key(self) -> tuple[str, str, str, str, int | None]:
        return (self.event_key, self.subject, self.relation, self.object, self.source_revision)


@dataclass(frozen=True)
class EventView:
    key: str
    number: int
    title: str
    description: str
    started_at: datetime


@dataclass
class ExtractionData:
    """Everything one extraction produced for the selected events."""

    source: str  # "graph" or the file it came from
    run_id: str  # the grouping run
    events: list[EventView] = field(default_factory=list)
    entities: dict[str, EntityView] = field(default_factory=dict)
    mentions: dict[str, list[str]] = field(default_factory=dict)  # event key -> entity keys
    relations: list[RelationView] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    prompt_versions: list[str] = field(default_factory=list)

    def entity(self, key: str) -> EntityView:
        return self.entities[key]


def vote_type(types: Iterable[str]) -> str:
    """The type most mentions gave (ties: alphabetical), like the graph's RETYPE."""
    counts = Counter(types)
    return min(counts, key=lambda t: (-counts[t], t)) if counts else "other"


# --- From Neo4j ---------------------------------------------------------------------

GRAPH_EVENTS = """
MATCH (ev:Event {run_id: $run_id})
RETURN ev.key AS key, ev.number AS number, ev.title AS title,
       ev.description AS description, ev.started_at AS started_at,
       ev.model AS model, ev.prompt_version AS prompt_version
ORDER BY ev.started_at, ev.number
"""
GRAPH_ENTITIES = """
MATCH (n:Entity)-[:INVOLVED_IN]->(ev:Event)
WHERE ev.key IN $keys
RETURN ev.key AS event_key, n.key AS key, n.name AS name, n.wikidata_id AS wikidata_id,
       n.type AS type, n.link_method AS link_method, n.aliases AS aliases
"""
GRAPH_RELATIONS = """
MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity)
WHERE r.event_key IN $keys
RETURN r.event_key AS event_key, a.key AS subject, r.relation AS relation,
       b.key AS object, r.source_title AS source_title,
       r.source_revision AS source_revision, r.confidence AS confidence
ORDER BY event_key, subject, relation, object
"""


def native(value: Any) -> Any:
    """Neo4j's temporal values as Python ones."""
    return value.to_native() if hasattr(value, "to_native") else value


def load_from_graph(
    driver: Driver, run_id: str, selection: EventSelection, database: str | None = None
) -> ExtractionData:
    data = ExtractionData(source="graph", run_id=run_id)
    with driver.session(database=database) as session:
        rows = session.run(GRAPH_EVENTS, run_id=run_id).data()
        rows = [r for r in rows if selection.matches(r["number"], native(r["started_at"]))]
        keys = [r["key"] for r in rows]
        entity_rows = session.run(GRAPH_ENTITIES, keys=keys).data()
        relation_rows = session.run(GRAPH_RELATIONS, keys=keys).data()
    for row in rows:
        data.events.append(
            EventView(
                row["key"],
                row["number"],
                row["title"] or "",
                row["description"] or "",
                native(row["started_at"]),
            )
        )
        data.mentions[row["key"]] = []
    data.models = sorted({r["model"] for r in rows if r["model"]})
    data.prompt_versions = sorted({r["prompt_version"] for r in rows if r["prompt_version"]})
    for row in entity_rows:
        data.mentions[row["event_key"]].append(row["key"])
        data.entities[row["key"]] = EntityView(
            row["key"],
            row["name"],
            row["wikidata_id"],
            row["type"] or "other",
            row["link_method"] or "none",
            tuple(row["aliases"] or ()),
        )
    data.relations = [RelationView(**row) for row in relation_rows]
    return data


# --- To and from a file -------------------------------------------------------------


def event_record(event: GraphEvent) -> dict[str, Any]:
    """One line of a saved extraction: an event as `build_graph_event` made it."""
    props = event.props
    return {
        "key": event.key,
        "number": props["number"],
        "title": props["title"],
        "description": props["description"],
        "started_at": props["started_at"].isoformat(),
        "model": props["model"],
        "prompt_version": props["prompt_version"],
        "entities": [asdict(entity) for entity in event.entities],
        "relations": [asdict(relation) for relation in event.relations],
    }


def append_event(path: Path, event: GraphEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(event_record(event), ensure_ascii=False) + "\n")


def read_records(path: Path) -> list[dict[str, Any]]:
    """The saved events; a later line for the same event replaces an earlier one."""
    if not path.exists():
        return []
    records: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records[record["key"]] = record
    return list(records.values())


def data_from_records(
    records: Sequence[Mapping[str, Any]], run_id: str, source: str, selection: EventSelection
) -> ExtractionData:
    """ExtractionData from saved events; entities merged across events by key."""
    data = ExtractionData(source=source, run_id=run_id)
    types: dict[str, list[str]] = {}
    aliases: dict[str, list[str]] = {}
    latest: dict[str, Mapping[str, Any]] = {}
    chosen = []
    for record in records:
        started_at = datetime.fromisoformat(record["started_at"])
        if record["key"].rpartition(":")[0] != run_id:
            continue
        if not selection.matches(record["number"], started_at):
            continue
        chosen.append(record)
        data.events.append(
            EventView(
                record["key"],
                record["number"],
                record["title"],
                record["description"],
                started_at,
            )
        )
        data.mentions[record["key"]] = [e["key"] for e in record["entities"]]
        for entity in record["entities"]:
            types.setdefault(entity["key"], []).append(entity["type"])
            names = aliases.setdefault(entity["key"], [])
            names.extend(a for a in entity["aliases"] if a not in names)
            latest[entity["key"]] = entity
        for relation in record["relations"]:
            data.relations.append(
                RelationView(
                    event_key=record["key"],
                    subject=relation["subject"],
                    relation=relation["relation"],
                    object=relation["object"],
                    source_title=relation["source_title"],
                    source_revision=relation["source_revision"],
                    confidence=relation["confidence"],
                )
            )
    for key, entity in latest.items():
        data.entities[key] = EntityView(
            key,
            entity["name"],
            entity["wikidata_id"],
            vote_type(types[key]),
            entity["link_method"],
            tuple(aliases[key]),
        )
    data.events.sort(key=lambda e: (e.started_at, e.number))
    data.relations.sort(key=lambda r: (r.event_key, r.subject, r.relation, r.object))
    data.models = sorted({r["model"] for r in chosen if r["model"]})
    data.prompt_versions = sorted({r["prompt_version"] for r in chosen if r["prompt_version"]})
    return data


def load_from_file(path: Path, run_id: str, selection: EventSelection) -> ExtractionData:
    return data_from_records(read_records(path), run_id, str(path.as_posix()), selection)
