"""Retrieval: facts from the graph, a generated query and vector search (fake graph)."""

from datetime import UTC, datetime

from neo4j.exceptions import Neo4jError

from tests.qa.helpers import CRISIS, DARTS, NOW, FakeEmbedder, FakeGraph, FixedWriter
from wikipulse.llm import LLMUnavailableError
from wikipulse.qa.graph import EventHit, PageHit
from wikipulse.qa.retrieve import (
    RetrievalConfig,
    Retriever,
    path_text,
    row_event_keys,
    time_parameters,
    time_window,
)
from wikipulse.qa.types import Citation

CONNECTION = "How are Ueno Bank and Santiago Peña connected?"


def retriever(graph: FakeGraph, **kwargs) -> Retriever:
    return Retriever(graph, config=RetrievalConfig(**kwargs.pop("config", {})), **kwargs)


def test_now_is_the_newest_event_and_today_is_its_day() -> None:
    times = time_parameters(NOW)
    assert times.today_start == datetime(2026, 11, 5, tzinfo=UTC)
    assert times.week_start == datetime(2026, 10, 29, 22, 54, tzinfo=UTC)
    context = retriever(FakeGraph(), channels=["graph"]).retrieve("Anything?")
    assert context.as_of == NOW


def test_a_connection_question_finds_both_entities_and_the_path_between_them() -> None:
    context = retriever(FakeGraph(), channels=["graph"]).retrieve(CONNECTION)
    assert [e.name for e in context.entities] == ["Ueno Bank", "Santiago Peña"]
    first = context.facts[0]
    assert first.id == "F1" and first.kind == "path"
    assert "Ueno Bank —involved in→ Paraguayan president resigns" in first.text
    assert "←involved in— Santiago Peña" in first.text
    assert first.sources == [
        Citation(title="Ueno Bank", revision=101),
        Citation(title="Santiago Peña", revision=102),
    ]
    kinds = [fact.kind for fact in context.facts]
    assert "relation" in kinds and "involvement" in kinds
    assert all(fact.sources for fact in context.facts)  # every fact carries its source


def test_no_path_leaves_a_note_not_a_fact() -> None:
    context = retriever(FakeGraph(connected=False), channels=["graph"]).retrieve(CONNECTION)
    assert not any(fact.kind == "path" for fact in context.facts)
    assert any("No connection between Ueno Bank and Santiago Peña" in n for n in context.notes)


def test_an_event_named_by_number_is_expanded_with_its_entities() -> None:
    context = retriever(FakeGraph(), channels=["graph"]).retrieve("Who is involved in event 341?")
    assert context.events == [CRISIS.key]
    texts = [fact.text for fact in context.facts]
    assert texts[0].startswith('Event "Paraguayan president resigns after Ueno Bank collapse"')
    assert any(t.startswith("Pedro Alliana (person) is involved in") for t in texts)


def test_a_question_about_today_lists_the_days_biggest_events() -> None:
    graph = FakeGraph()
    context = retriever(graph, channels=["graph"]).retrieve("What happened today?")
    assert graph.windows == [(datetime(2026, 11, 5, tzinfo=UTC), datetime(2026, 11, 6, tzinfo=UTC))]
    assert [f.kind for f in context.facts] == ["event", "event"]  # listed, not expanded
    assert context.facts[0].sources == [
        Citation(title="Ueno Bank", revision=101),
        Citation(title="Santiago Peña", revision=102),
    ]  # the event's two highest-scoring pages
    window = time_window("What happened today?", time_parameters(NOW))
    assert window == (datetime(2026, 11, 5, tzinfo=UTC), datetime(2026, 11, 6, tzinfo=UTC), "today")
    assert time_window("Who won?", time_parameters(NOW)) is None


def test_a_generated_query_runs_with_time_parameters_and_its_rows_cite_event_pages() -> None:
    graph = FakeGraph(
        rows=[
            {"organization": "Ueno Bank", "event_keys": [CRISIS.key, DARTS.key]},
            {
                "subject": "A",
                "relation": "r",
                "object": "B",
                "source_title": "A page",
                "source_revision": 5,
            },
        ]
    )
    writer = FixedWriter("MATCH (ev:Event) RETURN ev.key AS event_key")
    context = retriever(graph, cypher_writer=writer, channels=["cypher"]).retrieve(
        "Which organizations appear in more than one event this week?"
    )
    cypher, params, timeout, max_rows = graph.queries[0]
    assert cypher.endswith("LIMIT 25") and timeout == 5.0 and max_rows == 25
    assert params["now"] == NOW and params["week_start"] == datetime(
        2026, 10, 29, 22, 54, tzinfo=UTC
    )
    first, second = context.facts
    assert first.kind == "query" and first.channel == "cypher"
    assert first.text == "Query result: organization = Ueno Bank"  # keys serve citations only
    assert first.sources == [
        Citation(title="Ueno Bank", revision=101),
        Citation(title="PDC", revision=201),
    ]  # one page per event
    assert second.sources == [Citation(title="A page", revision=5)]  # the row's own source


def test_a_query_that_hits_the_row_cap_marks_its_list_incomplete() -> None:
    graph = FakeGraph(rows=[{"organization": f"Org {n}"} for n in range(5)])
    writer = FixedWriter("MATCH (o:Entity) RETURN o.name AS organization")
    question = "Which organizations appear in more than one event this week?"
    context = retriever(
        graph, cypher_writer=writer, channels=["cypher"], config={"max_rows": 3}
    ).retrieve(question)
    assert context.cypher_rows == 3
    assert context.limits == ["The query results stop at the limit of 3 rows; there may be more."]
    under = FakeGraph(rows=[{"organization": "Org 1"}])
    context = retriever(under, cypher_writer=writer, channels=["cypher"]).retrieve(question)
    assert context.limits == []


def test_a_period_listing_that_hits_its_limit_marks_its_list_incomplete() -> None:
    config = {"period_events": 2}
    context = retriever(FakeGraph(), channels=["graph"], config=config).retrieve(
        "What happened today?"
    )
    assert context.limits == [
        "Only the 2 biggest events of today were looked up; there may be more."
    ]
    context = retriever(FakeGraph(), channels=["graph"]).retrieve("What happened today?")
    assert context.limits == []


def test_an_unsafe_query_never_reaches_the_graph() -> None:
    graph = FakeGraph()
    writer = FixedWriter("MATCH (n) DETACH DELETE n")
    context = retriever(graph, cypher_writer=writer, channels=["cypher"]).retrieve("Delete all")
    assert graph.queries == []
    assert context.facts == []
    assert any("query rejected" in note and "DELETE" in note for note in context.notes)


def test_a_failing_model_leaves_a_note_and_the_other_channels_go_on() -> None:
    writer = FixedWriter(error=LLMUnavailableError("Ollama is not running"))
    context = retriever(FakeGraph(), cypher_writer=writer).retrieve(CONNECTION)
    assert any("no query" in note for note in context.notes)
    assert context.facts  # the graph channel still answered


def page_hit(title: str, event_key: str, cosine: float) -> PageHit:
    return PageHit(title, "desc", "Summary.", 55, event_key, "Event title", cosine)


def test_vector_search_keeps_close_hits_and_expands_their_events() -> None:
    graph = FakeGraph(
        page_hits=[
            page_hit("Ueno Bank", CRISIS.key, 0.8),
            page_hit("Ueno Bank", DARTS.key, 0.8),
            page_hit("Darts", DARTS.key, 0.3),
        ],
        event_hits=[EventHit(CRISIS.key, 0.75)],
    )
    embedder = FakeEmbedder()
    context = retriever(graph, embedder=embedder, channels=["vector"]).retrieve("bank collapse")
    assert (
        embedder.texts[0].startswith("Represent this sentence")
        and "bank collapse" in (embedder.texts[0])
    )
    pages = [f for f in context.facts if f.kind == "page"]
    assert len(pages) == 1  # one fact per page, naming both events
    assert "part of the events" in pages[0].text
    events = [f for f in context.facts if f.kind == "event"]
    assert [f.channel for f in events] == ["vector", "vector"]
    assert not any(f.kind == "involvement" for f in context.facts)  # no traversal: graph off


def test_vector_search_below_the_minimum_says_so() -> None:
    graph = FakeGraph(page_hits=[page_hit("Darts", DARTS.key, 0.4)])
    context = retriever(graph, embedder=FakeEmbedder(), channels=["vector"]).retrieve("x")
    assert context.facts == []
    assert any("best cosine 0.40" in note for note in context.notes)


def test_the_context_keeps_at_most_max_facts_and_no_duplicates() -> None:
    context = retriever(FakeGraph(), channels=["graph"], config={"max_facts": 3}).retrieve(
        CONNECTION
    )
    assert [fact.id for fact in context.facts] == ["F1", "F2", "F3"]
    assert len({fact.key for fact in context.facts}) == 3


def test_helpers_read_paths_and_event_keys() -> None:
    graph = FakeGraph()
    (path,) = graph.paths("Q1", "Q2")
    assert path_text(path).count("involved in") == 2
    row = {"a": [{"labels": ["Event"], "key": "run:7"}], "b": "run:8", "c": "not:a key"}
    assert row_event_keys(row) == ["run:7", "run:8"]


def test_events_the_query_already_lists_are_not_listed_twice() -> None:
    graph = FakeGraph(rows=[{"event_key": CRISIS.key, "title": CRISIS.title}])
    writer = FixedWriter("MATCH (ev:Event) RETURN ev.key AS event_key, ev.title AS title")
    context = retriever(graph, cypher_writer=writer, channels=["graph", "cypher"]).retrieve(
        "What happened today?"
    )
    assert [(f.kind, f.channel) for f in context.facts] == [("query", "cypher"), ("event", "graph")]
    assert DARTS.key in context.facts[1].text  # only the event the query did not show


# --- Repairing a generated query (spec 12a) ---------------------------------------------------

GOOD = "MATCH (ev:Event) RETURN ev.key AS event_key"


class FailingOnceGraph(FakeGraph):
    """Neo4j refuses the first query (a syntax error), then answers."""

    def run_query(self, cypher, params, timeout_s, max_rows):
        if not self.queries:
            self.queries.append((cypher, params, timeout_s, max_rows))
            raise Neo4jError._hydrate_neo4j(
                code="Neo.ClientError.Statement.SyntaxError", message="Invalid input 'interval'"
            )
        return super().run_query(cypher, params, timeout_s, max_rows)


def test_a_rejected_query_gets_one_repair_with_the_reason() -> None:
    graph = FakeGraph(rows=[{"event_key": CRISIS.key}])
    writer = FixedWriter("MATCH (n) DETACH DELETE n", repairs=[GOOD])
    context = retriever(graph, cypher_writer=writer, channels=["cypher"]).retrieve("Anything?")
    assert len(writer.errors) == 1 and "DELETE" in writer.errors[0]
    assert [q[0] for q in graph.queries] == [GOOD + "\nLIMIT 25"]  # only the fixed one ran
    assert context.cypher_ran and context.cypher == GOOD + "\nLIMIT 25"
    assert context.cypher_failures == ["rejected: not a read-only query (uses DELETE, DETACH)"]
    assert context.cypher_prompt == "fixed"
    assert "cypher: the corrected query ran." in context.notes
    assert [f.kind for f in context.facts] == ["query"]


def test_a_query_that_neo4j_refuses_gets_one_repair_with_the_error() -> None:
    graph = FailingOnceGraph(rows=[{"event_key": CRISIS.key}])
    writer = FixedWriter(
        "MATCH (ev:Event) WHERE ev.started_at > $now - interval '1 hour' RETURN ev"
    )
    writer.repairs = [GOOD]
    context = retriever(graph, cypher_writer=writer, channels=["cypher"]).retrieve("Anything?")
    assert writer.errors == [
        "failed: Neo.ClientError.Statement.SyntaxError: Invalid input 'interval'"
    ]
    assert context.cypher_ran and context.cypher_rows == 1
    assert any(
        "the query failed (Neo.ClientError.Statement.SyntaxError)" in n for n in context.notes
    )


def test_the_repair_is_bounded_and_an_unsafe_query_still_never_runs() -> None:
    graph = FakeGraph()
    writer = FixedWriter("MATCH (n) DETACH DELETE n")  # the repair returns it again
    context = retriever(graph, cypher_writer=writer, channels=["cypher"]).retrieve("Delete all")
    assert len(writer.errors) == 1  # one repair, not more
    assert graph.queries == [] and context.facts == []
    assert not context.cypher_ran and len(context.cypher_failures) == 2


def test_repairs_can_be_switched_off() -> None:
    graph = FakeGraph()
    writer = FixedWriter("MATCH (n) DETACH DELETE n", repairs=[GOOD])
    context = retriever(
        graph, cypher_writer=writer, channels=["cypher"], config={"cypher_repairs": 0}
    ).retrieve("Anything?")
    assert writer.errors == [] and graph.queries == []
    assert len(context.cypher_failures) == 1 and not context.cypher_ran


def test_an_empty_repair_means_no_query() -> None:
    graph = FakeGraph()
    writer = FixedWriter("MATCH (n) DETACH DELETE n", repairs=[""])
    context = retriever(graph, cypher_writer=writer, channels=["cypher"]).retrieve("Anything?")
    assert graph.queries == [] and not context.cypher_ran
    assert "cypher: the model wrote no query for this question." in context.notes
