"""Integration tests: question answering against the real Neo4j (docker compose).

Run with `uv run pytest -m integration`. Every test writes its own events (a unique run
id, made-up names and Wikidata IDs, pages that don't exist on Wikipedia, dates in 2031)
and deletes only them. The LLM is mocked.
"""

import json
import random
import string
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pytest
from neo4j.exceptions import Neo4jError

from tests.graph.helpers import event, saff_page
from tests.llm.helpers import ScriptedClient
from wikipulse.config import get_settings
from wikipulse.graph.extract import ExtractedEntity, ExtractedRelation, Extraction
from wikipulse.graph.link import Link
from wikipulse.graph.store import GraphStore, build_graph_event
from wikipulse.qa.answer import Answerer
from wikipulse.qa.cypher import validate_cypher
from wikipulse.qa.graph import QAGraph
from wikipulse.qa.index import Document, ensure_indexes, write_embeddings
from wikipulse.qa.retrieve import Retriever
from wikipulse.qa.types import Citation

pytestmark = pytest.mark.integration

DAY = datetime(2031, 1, 1, 12, 0, tzinfo=UTC)
DIMENSIONS = 384  # the embedding model's size; the vector indexes are built for it


@dataclass
class World:
    """The made-up story the tests ask about."""

    run_id: str
    person: str
    company: str
    town: str
    page_a: str
    page_b: str


def tag() -> str:
    return "".join(random.choices(string.ascii_lowercase, k=8))


def fake_qid() -> str:
    return f"Q99{random.randrange(10**9, 10**10)}"


@pytest.fixture
def store() -> Iterator[GraphStore]:
    with GraphStore.from_settings(get_settings()) as graph:
        graph.ensure_schema()
        ensure_indexes(graph.driver, DIMENSIONS)
        yield graph


@pytest.fixture
def qa_graph() -> Iterator[QAGraph]:
    with QAGraph.from_settings(get_settings()) as graph:
        yield graph


@pytest.fixture
def world(store: GraphStore) -> Iterator[World]:
    word = tag()
    w = World(
        run_id=f"test-{uuid.uuid4().hex}",
        person=f"Zorblax Quentaro {word}",
        company=f"Vimtrel Holdings {word}",
        town=f"Plonkton {word}",
        page_a=f"Zorblax test page {word}",
        page_b=f"Vimtrel test page {word}",
    )
    links = {w.person: Link(fake_qid(), "search", w.person)}
    write(
        store,
        w,
        1,
        w.page_a,
        111,
        [(w.person, "person"), (w.company, "organization")],
        [(w.person, "chairs", w.company)],
        links,
    )
    write(
        store,
        w,
        2,
        w.page_b,
        222,
        [(w.company, "organization"), (w.town, "place")],
        [(w.company, "is based in", w.town)],
        {},
    )
    yield w
    for key in store.event_keys(w.run_id):
        store.delete_event(key)


def write(store, w: World, number, title, revision, mentions, relations, links) -> None:
    page = saff_page(
        title=title,
        spike_titles=(title,),
        wikidata_id=None,
        summary=f"Summary of {title}.",
        revision=revision,
        links=(),
    )
    extraction = Extraction(
        f"Headline {number} of {w.run_id}",
        f"Description {number}.",
        entities=[ExtractedEntity(n, t, title, revision, 0.9) for n, t in mentions],
        relations=[ExtractedRelation(s, r, o, title, revision, 0.9) for s, r, o in relations],
        model="fake:m",
    )
    source = event((page,), run_id=w.run_id, number=number, started_at=DAY)
    store.write_event(build_graph_event(source, [page], extraction, links, DAY))


def test_a_connection_is_found_through_the_shared_entity_with_its_sources(qa_graph, world):
    question = f"How are {world.person} and {world.town} connected?"
    context = Retriever(qa_graph, channels=["graph"]).retrieve(question, as_of=DAY)
    assert [e.name for e in context.entities] == [world.person, world.town]
    paths = [fact for fact in context.facts if fact.kind == "path"]
    assert paths, context.notes
    assert f"{world.person} —chairs→ {world.company} —is based in→ {world.town}" in (paths[0].text)
    assert paths[0].sources == [
        Citation(title=world.page_a, revision=111),
        Citation(title=world.page_b, revision=222),
    ]


def test_today_means_the_day_of_as_of_in_event_time(qa_graph, world):
    context = Retriever(qa_graph, channels=["graph"]).retrieve("What happened today?", as_of=DAY)
    titles = [fact.text for fact in context.facts if fact.kind == "event"]
    assert len(titles) == 2 and all(world.run_id in text for text in titles)


def test_neo4j_itself_refuses_writes_from_a_generated_query(qa_graph, store, world):
    write_query = "CREATE (:QaWriteProbe {run: $run}) RETURN 1 AS one"
    with pytest.raises(Neo4jError, match="read access mode"):
        qa_graph.run_query(write_query, {"run": world.run_id}, timeout_s=5, max_rows=5)
    with store.driver.session() as session:
        found = session.run(
            "MATCH (n:QaWriteProbe {run: $run}) RETURN count(n) AS n", run=world.run_id
        ).single()["n"]
    assert found == 0


def test_a_slow_query_is_stopped_by_the_timeout(qa_graph):
    slow = "UNWIND range(1, 1000000000) AS x WITH x WHERE x % 7 = 99 RETURN count(x) AS n"
    with pytest.raises(Neo4jError, match="(?i)terminated|timed out"):
        qa_graph.run_query(validate_cypher(slow), {}, timeout_s=0.5, max_rows=5)


def test_a_validated_query_returns_at_most_the_limit(qa_graph, world):
    query = validate_cypher(
        "MATCH (ev:Event {run_id: $run}) RETURN ev.key AS event_key", max_rows=1
    )
    rows = qa_graph.run_query(query, {"run": world.run_id}, timeout_s=5, max_rows=1)
    assert len(rows) == 1 and rows[0]["event_key"].startswith(world.run_id)


def test_the_added_limits_hold_in_neo4j_for_comments_and_every_union_branch(qa_graph):
    commented = validate_cypher("UNWIND range(1, 100) AS x RETURN x // LIMIT 1000", max_rows=3)
    assert len(qa_graph.run_query(commented, {}, timeout_s=5, max_rows=100)) == 3
    union = validate_cypher(
        "UNWIND range(1, 100) AS x RETURN x UNION ALL UNWIND range(1, 100) AS x RETURN x",
        max_rows=3,
    )
    assert len(qa_graph.run_query(union, {}, timeout_s=5, max_rows=100)) == 6


def test_a_bounded_quantified_path_is_valid_cypher(qa_graph):
    query = validate_cypher(
        "MATCH (a:QaNoSuchLabel)-[:RELATED_TO]-{1,3}(b) RETURN b.name AS name", max_rows=3
    )
    assert qa_graph.run_query(query, {}, timeout_s=5, max_rows=3) == []


def one_hot(index: int) -> list[float]:
    vector = np.zeros(DIMENSIONS, dtype=np.float32)
    vector[index] = 1.0
    return vector.tolist()


class Fixed:
    def __init__(self, vector: list[float]) -> None:
        self.vector = np.asarray(vector)

    def embed(self, text: str) -> np.ndarray:
        return self.vector


def test_vector_search_finds_the_page_and_the_answer_cites_its_revision(qa_graph, store, world):
    vector = one_hot(random.randrange(DIMENSIONS))
    docs = [
        Document("Page", world.page_a, "text", "test"),
        Document("Event", f"{world.run_id}:1", "t", "test"),
    ]
    write_embeddings(store.driver, docs, [vector, vector], "test-model")
    retriever = Retriever(qa_graph, embedder=Fixed(vector), channels=["vector"])
    context = retriever.retrieve("anything", as_of=DAY)
    page = next(fact for fact in context.facts if fact.kind == "page")
    assert page.text.startswith(f'Wikipedia page "{world.page_a}"')
    client = ScriptedClient(
        [json.dumps({"answerable": True, "answer": f"It happened [{page.id}]."})]
    )
    result = Answerer(client).answer(context)
    assert result.citations == [Citation(title=world.page_a, revision=111)]
    assert result.text == "It happened [1]."
