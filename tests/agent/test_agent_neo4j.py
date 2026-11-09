"""Integration tests: briefings in the real Neo4j, and the agent over the real MCP server.

Run with `uv run pytest -m integration`.

- The briefing store writes to the main graph (docker compose `neo4j`), on events of its
  own made-up run id, and removes only what it wrote.
- The agent test reads the frozen snapshot (`neo4j-eval`, skipped when it isn't
  running) through the real MCP server, started as a subprocess over stdio, with a
  scripted LLM. It writes nothing.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from neo4j.exceptions import AuthError, ServiceUnavailable

from tests.agent.helpers import ScriptedLLM, call, response
from tests.graph.helpers import event, saff_page
from wikipulse.agent.briefing import Briefing
from wikipulse.agent.loop import AgentLimits, AnalystAgent
from wikipulse.agent.run import mcp_client
from wikipulse.agent.sources import ExtractionCitation
from wikipulse.agent.store import BriefingMeta, BriefingStore
from wikipulse.agent.tools import ToolBox
from wikipulse.config import get_settings
from wikipulse.graph.extract import Extraction
from wikipulse.graph.store import GraphStore, build_graph_event
from wikipulse.mcp_server.explore import Explorer
from wikipulse.qa.graph import QAGraph
from wikipulse.qa.types import Citation

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
META = BriefingMeta("fake:m", "analyst_v1", datetime(2026, 11, 9, tzinfo=UTC), "t.json")
BRIEFING = Briefing(
    text="The SAFF Championship began [1].",
    citations=[Citation(title="2026 SAFF Championship", revision=111)],
)


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


def write_event(store: GraphStore, run_id: str, number: int, score: float, at: datetime) -> str:
    page = saff_page(revision=111, score=score, title=f"Test page {run_id} {number}")
    source = event((page,), run_id=run_id, number=number)
    extraction = Extraction(f"Event {number}", "Description.", model="fake:m")
    store.write_event(build_graph_event(source, [page], extraction, {}, at))
    return source.key


def test_the_trigger_finds_strong_events_until_they_are_briefed(store, run_id) -> None:
    strong = write_event(store, run_id, 1, score=12.0, at=NOW)
    write_event(store, run_id, 2, score=3.0, at=NOW)  # weak, one page
    briefings = BriefingStore(store.driver)
    assert [c.key for c in briefings.candidates(9.0, 3, run_id=run_id)] == [strong]
    assert briefings.save(strong, BRIEFING, META)
    assert briefings.candidates(9.0, 3, run_id=run_id) == []
    assert [c.key for c in briefings.candidates(9.0, 3, run_id=run_id, force=True)] == [strong]
    assert briefings.save(f"{run_id}:404", BRIEFING, META) is False
    # The MCP tool get_event shows the briefing with its sources (spec 05c reads it).
    graph = QAGraph(store.driver)
    detail = Explorer(graph).event(strong)
    assert detail.briefing == BRIEFING.as_text()
    with store.driver.session() as session:
        props = session.run("MATCH (ev:Event {key: $k}) RETURN ev", k=strong).single()["ev"]
    assert props["briefing_urls"] == ["https://en.wikipedia.org/w/index.php?oldid=111"]
    assert props["briefing_sources"] == ["2026 SAFF Championship"]
    assert props["briefing_model"] == "fake:m" and props["briefing_transcript"] == "t.json"
    assert props.get("briefing_verified") is None  # this briefing was never checked
    # Extracting the event again (its pages changed) makes the briefing out of date.
    write_event(store, run_id, 1, score=12.0, at=NOW + timedelta(hours=1))
    assert [c.key for c in briefings.candidates(9.0, 3, run_id=run_id)] == [strong]
    with store.driver.session() as session:
        kept = session.run("MATCH (ev:Event {key: $k}) RETURN ev.briefing AS b", k=strong).single()
    assert kept["b"] == BRIEFING.as_text()  # a new extraction doesn't erase the briefing


@pytest.fixture(scope="module")
def snapshot_uri() -> str:
    uri = get_settings().eval_qa_neo4j_uri
    graph = QAGraph.from_settings(get_settings().model_copy(update={"neo4j_uri": uri}))
    try:
        graph.driver.verify_connectivity()
    except (ServiceUnavailable, AuthError, OSError):
        pytest.skip("the snapshot server (neo4j-eval) is not running")
    finally:
        graph.close()
    return uri


def test_page_text_on_the_snapshot(snapshot_uri: str) -> None:
    settings = get_settings().model_copy(update={"neo4j_uri": snapshot_uri})
    with QAGraph.from_settings(settings) as graph:
        page = graph.page_text("ueno bank")
        assert page is not None and page.title == "Ueno Bank"
        assert page.summary and page.revision and page.event_keys
        assert graph.page_text("No such page at all") is None
        # With an event: the revision the page had in that event, not in its newest one.
        rows = graph._read(
            "MATCH (:Page {title: 'Ueno Bank'})-[po:PART_OF]->(ev:Event) "
            "RETURN ev.key AS key, po.revision AS revision"
        )
        assert rows
        for row in rows:
            assert graph.page_text("Ueno Bank", row["key"]).revision == row["revision"]


async def test_the_agent_over_the_real_mcp_server(snapshot_uri: str) -> None:
    settings = get_settings().model_copy(update={"neo4j_uri": snapshot_uri})
    llm = ScriptedLLM(
        [response("", [call("read_page", title="Ueno Bank")]), "Ueno Bank collapsed [F1][F6]."]
    )
    with QAGraph.from_settings(settings) as graph:
        key = graph.events_by_number([341])[0]
        with anyio.fail_after(120):
            async with mcp_client(settings, None) as mcp:
                tools = ToolBox(mcp, graph)
                agent = AnalystAgent(llm, tools, AgentLimits(), verify=False)
                run = await agent.run(key)
    assert run.stop_reason == "answered" and run.event_key == key
    assert "Ueno Bank [F1]" in llm.requests[0].messages[2].content
    page, description = run.briefing.citations
    # The page at its revision in *this* event, with its permanent link ...
    assert page.title == "Ueno Bank"
    assert page.url == f"https://en.wikipedia.org/w/index.php?oldid={page.revision}"
    assert page.revision == graph_revision(snapshot_uri, "Ueno Bank", key)
    # ... and [F6], the event's title and description, cited as the extraction's.
    assert isinstance(description, ExtractionCitation) and description.url == ""


def graph_revision(uri: str, title: str, event_key: str) -> int:
    settings = get_settings().model_copy(update={"neo4j_uri": uri})
    with QAGraph.from_settings(settings) as graph:
        rows = graph._read(
            "MATCH (:Page {title: $title})-[po:PART_OF]->(:Event {key: $key}) "
            "RETURN po.revision AS revision",
            title=title,
            key=event_key,
        )
    return rows[0]["revision"]
