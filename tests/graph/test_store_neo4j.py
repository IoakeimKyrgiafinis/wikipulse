"""Integration tests: writing events to the real Neo4j (docker compose).

Run with `uv run pytest -m integration`. Every test uses its own run id and made-up
Wikidata IDs (Q99...), and removes only what it wrote.
"""

import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from tests.graph.helpers import event, saff_page
from wikipulse.config import get_settings
from wikipulse.graph.extract import ExtractedEntity, ExtractedRelation, Extraction
from wikipulse.graph.link import UNLINKED, Link
from wikipulse.graph.store import GraphStore, build_graph_event

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
PAGE = saff_page(revision=111)


@pytest.fixture
def store() -> Iterator[GraphStore]:
    with GraphStore.from_settings(get_settings()) as graph:
        graph.ensure_schema()
        yield graph


@pytest.fixture
def run_id(store: GraphStore) -> Iterator[str]:
    run = f"test-{uuid.uuid4().hex}"
    yield run
    for key in store.event_keys(run):
        store.delete_event(key)


def fake_qid() -> str:
    return f"Q99{random.randrange(10**9, 10**10)}"


def mention(name: str, type_: str = "person") -> ExtractedEntity:
    return ExtractedEntity(name, type_, PAGE.title, 111, 0.9)


def write(store, run_id, number, mentions, links, relations=(), title="Title") -> None:
    extraction = Extraction(
        title,
        "Description.",
        entities=list(mentions),
        relations=[ExtractedRelation(s, r, o, PAGE.title, 111, 0.6) for s, r, o in relations],
        model="fake:m",
    )
    source = event((PAGE,), run_id=run_id, number=number)
    store.write_event(build_graph_event(source, [PAGE], extraction, links, NOW))


def query(store: GraphStore, cypher: str, **params) -> list[dict]:
    with store.driver.session() as session:
        return [record.data() for record in session.run(cypher, **params)]


def event_facts(store: GraphStore, run_id: str) -> dict:
    """Everything the run's events hold, as comparable values."""
    entities = query(
        store,
        "MATCH (n:Entity)-[r:INVOLVED_IN]->(ev:Event {run_id: $run}) "
        "RETURN ev.key AS event, n.key AS key, n.name AS name, n.aliases AS aliases "
        "ORDER BY event, key",
        run=run_id,
    )
    relations = query(
        store,
        "MATCH (ev:Event {run_id: $run}) MATCH (a)-[r:RELATED_TO {event_key: ev.key}]->(b) "
        "RETURN a.key AS a, r.relation AS rel, b.key AS b ORDER BY a, rel, b",
        run=run_id,
    )
    events = query(store, "MATCH (ev:Event {run_id: $run}) RETURN ev.title AS t", run=run_id)
    return {"entities": entities, "relations": relations, "events": events}


def test_writing_the_same_event_twice_changes_nothing(store, run_id) -> None:
    qid = fake_qid()
    name = f"Person {run_id}"
    args = ([mention(name), mention(f"Other {run_id}")], {name: Link(qid, "search", name)})
    rel = [(name, "met", f"Other {run_id}")]
    write(store, run_id, 1, *args, relations=rel)
    first = event_facts(store, run_id)
    write(store, run_id, 1, *args, relations=rel)
    assert event_facts(store, run_id) == first
    assert len(first["entities"]) == 2 and len(first["relations"]) == 1
    assert len(first["events"]) == 1


def test_reprocessing_replaces_the_events_facts_and_removes_orphans(store, run_id) -> None:
    kept, dropped, added = (f"{word} {run_id}" for word in ("Kept", "Dropped", "Added"))
    write(store, run_id, 1, [mention(kept), mention(dropped)], {}, [(kept, "met", dropped)])
    write(store, run_id, 1, [mention(kept), mention(added)], {}, title="New title")
    facts = event_facts(store, run_id)
    assert [e["name"] for e in facts["entities"]] == [added, kept]
    assert facts["relations"] == []
    assert facts["events"] == [{"t": "New title"}]
    orphan = query(store, "MATCH (n:Entity {name: $name}) RETURN n", name=dropped)
    assert orphan == []


def test_one_entity_per_wikidata_id_however_it_was_named(store, run_id) -> None:
    qid = fake_qid()
    full, short = f"Donald Trump {run_id}", f"Trump {run_id}"
    write(store, run_id, 1, [mention(full)], {full: Link(qid, "link", full)})
    write(store, run_id, 2, [mention(short)], {short: Link(qid, "search", full)})
    nodes = query(
        store,
        "MATCH (n:Entity {wikidata_id: $qid})-[:INVOLVED_IN]->(ev:Event) "
        "RETURN n.key AS key, n.aliases AS aliases, count(ev) AS events",
        qid=qid,
    )
    assert nodes == [{"key": qid, "aliases": [full, short], "events": 2}]


def test_deleting_an_event_keeps_entities_other_events_use(store, run_id) -> None:
    shared, only = f"Shared {run_id}", f"Only {run_id}"
    write(store, run_id, 1, [mention(shared), mention(only)], {})
    write(store, run_id, 2, [mention(shared)], {})
    store.delete_event(f"{run_id}:1")
    facts = event_facts(store, run_id)
    assert [(e["event"], e["name"]) for e in facts["entities"]] == [(f"{run_id}:2", shared)]
    assert query(store, "MATCH (n:Entity {name: $name}) RETURN n", name=only) == []


def test_relationships_carry_their_provenance_and_counts_add_up(store, run_id) -> None:
    qid = fake_qid()
    a, b = f"Alpha {run_id}", f"Beta {run_id}"
    write(store, run_id, 1, [mention(a), mention(b)], {a: Link(qid, "page", a)}, [(a, "leads", b)])
    (rel,) = query(
        store,
        "MATCH (:Entity {key: $qid})-[r:RELATED_TO]->(:Entity) RETURN properties(r) AS p",
        qid=qid,
    )
    assert rel["p"] | {"extracted_at": None} == {
        "event_key": f"{run_id}:1",
        "relation": "leads",
        "source_title": PAGE.title,
        "source_revision": 111,
        "confidence": 0.6,
        "model": "fake:m",
        "prompt_version": "extract_v1",
        "extracted_at": None,
    }
    counts = store.run_counts(run_id)
    assert (counts.events, counts.entities, counts.linked, counts.relations) == (1, 2, 1, 1)
    assert counts.by_method == {"page": 1, "link": 0, "search": 0, "none": 1}
    assert UNLINKED.method == "none"


def test_an_entitys_type_is_the_one_most_of_its_events_gave_it(store, run_id) -> None:
    name = f"India {run_id}"

    def entity_type() -> str:
        (row,) = query(store, "MATCH (n:Entity {name: $name}) RETURN n.type AS t", name=name)
        return row["t"]

    write(store, run_id, 1, [mention(name, "place")], {})
    write(store, run_id, 2, [mention(name, "place")], {})
    write(store, run_id, 3, [mention(name, "organization")], {})
    assert entity_type() == "place"  # 2 to 1, although event 3 was written last
    write(store, run_id, 1, [mention(name, "organization")], {})
    assert entity_type() == "organization"  # now 2 to 1 the other way
    store.delete_event(f"{run_id}:3")
    assert entity_type() == "organization"  # 1 to 1: alphabetical
