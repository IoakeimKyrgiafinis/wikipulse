"""Integration test: reading an extraction back from the real Neo4j (docker compose).

Run with `uv run pytest -m integration`. The test writes events under its own run id with
made-up Wikidata IDs (Q99...) and removes only what it wrote.
"""

import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from tests.graph.helpers import event, saff_page
from wikipulse.config import get_settings
from wikipulse.eval.extraction_data import EventSelection, load_from_graph, parse_events
from wikipulse.graph.extract import ExtractedEntity, ExtractedRelation, Extraction
from wikipulse.graph.link import UNLINKED, Link
from wikipulse.graph.store import GraphStore, build_graph_event

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
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


def write(store, run_id, number, names, links, relations=()) -> None:
    extraction = Extraction(
        f"Event {number}",
        "Description.",
        entities=[ExtractedEntity(name, type_, PAGE.title, 111, 0.9) for name, type_ in names],
        relations=[ExtractedRelation(s, r, o, PAGE.title, 111, 0.6) for s, r, o in relations],
        model="fake:m",
        prompt_version="extract_test",
    )
    source = event((PAGE,), run_id=run_id, number=number)
    store.write_event(build_graph_event(source, [PAGE], extraction, links, NOW))


def test_the_graph_reads_back_as_entities_and_relations(store, run_id):
    qid = f"Q99{random.randrange(10**9, 10**10)}"
    person = f"Person {uuid.uuid4().hex[:8]}"
    club = f"Club {uuid.uuid4().hex[:8]}"
    links = {person: Link(qid, "search", person), club: UNLINKED}
    write(
        store,
        run_id,
        1,
        [(person, "person"), (club, "organization")],
        links,
        [(person, "plays for", club)],
    )
    write(store, run_id, 2, [(person, "person")], links)

    data = load_from_graph(store.driver, run_id, EventSelection())
    assert [e.number for e in data.events] == [1, 2]
    assert data.entities[qid].aliases == (person,)
    assert data.entities[qid].link_method == "search"
    assert data.mentions[f"{run_id}:2"] == [qid]
    (relation,) = data.relations
    assert (relation.subject, relation.relation, relation.object) == (
        qid,
        "plays for",
        f"name:{club.lower()}",
    )
    assert (relation.source_title, relation.source_revision) == (PAGE.title, 111)
    assert data.models == ["fake:m"] and data.prompt_versions == ["extract_test"]

    only_two = load_from_graph(store.driver, run_id, parse_events("2"))
    assert [e.number for e in only_two.events] == [2]
    assert only_two.relations == []
