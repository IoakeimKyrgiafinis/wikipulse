"""Integration tests: the graph runner over real Postgres and Neo4j, with a mocked LLM
and mocked Wikimedia APIs.

Run with `uv run pytest -m integration`. Every test uses its own run id and page titles
(a unique tag) and removes only those rows and graph nodes afterwards.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest

from tests.enrich.helpers import make_client
from tests.graph.helpers import API_URL, WIKIDATA_URL, answer, entity, relation
from tests.llm.helpers import ScriptedClient
from wikipulse.config import get_settings
from wikipulse.db.graph import load_event, pending_events
from wikipulse.db.migrate import migrate
from wikipulse.db.pages import upsert_page
from wikipulse.db.spikes import delete_run, upsert_spikes
from wikipulse.detect.detector import SpikeRecord
from wikipulse.enrich.pages import PageInfo, PageStatus
from wikipulse.graph.extract import Extractor
from wikipulse.graph.link import Linker
from wikipulse.graph.run import GraphRunner
from wikipulse.graph.store import GraphStore

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as connection:
        migrate(connection)
        yield connection


@pytest.fixture
def store() -> Iterator[GraphStore]:
    with GraphStore.from_settings(get_settings()) as graph:
        graph.ensure_schema()
        yield graph


@pytest.fixture
def tag(conn: psycopg.Connection, store: GraphStore) -> Iterator[str]:
    marker = f"test-{uuid.uuid4().hex}"
    yield marker
    for key in store.event_keys(marker):
        store.delete_event(key)
    conn.execute("DELETE FROM events WHERE run_id = %s", (marker,))  # cascades
    conn.execute("DELETE FROM pages WHERE title LIKE %s", (f"{marker}%",))
    delete_run(conn, marker)


def add_spike(conn, run_id: str, title: str, minute: int) -> int:
    started = T0 + timedelta(minutes=minute)
    spike = SpikeRecord(
        title=title,
        status="ended",
        started_at=started,
        first_edit_at=started,
        last_edit_at=started + timedelta(minutes=10),
        ended_at=started + timedelta(minutes=10),
        total_edits=6,
        distinct_editors=3,
        revert_edits=0,
        peak_edits_10m=6,
        score=5.0 + minute,
    )
    upsert_spikes(conn, run_id, [spike])
    upsert_page(
        conn,
        PageInfo(
            requested_title=title,
            title=title,
            status=PageStatus.OK,
            wikidata_id=None,
            summary=f"{title} is a page about a story.",
        ),
    )
    row = conn.execute(
        "SELECT id FROM spikes WHERE run_id = %s AND title = %s", (run_id, title)
    ).fetchone()
    return row[0]


def add_event(conn, run_id: str, number: int, spike_ids: list[int], title: str) -> None:
    conn.execute(
        "INSERT INTO events (run_id, spike_run_id, number, status, title, started_at, "
        "last_spike_at, page_count, score) VALUES (%s, %s, %s, 'ended', %s, %s, %s, %s, 5)",
        (run_id, run_id, number, title, T0, T0, len(spike_ids)),
    )
    join_event(conn, run_id, number, spike_ids)


def join_event(conn, run_id: str, number: int, spike_ids: list[int]) -> None:
    for spike_id in spike_ids:
        conn.execute(
            "INSERT INTO event_pages (run_id, spike_id, event_number, title, joined_score) "
            "VALUES (%s, %s, %s, 'x', 1)",
            (run_id, spike_id, number),
        )


def quiet_wikimedia(request: httpx.Request) -> httpx.Response:
    """No revisions in the burst, nothing on Wikidata."""
    if request.url.params.get("action") == "wbsearchentities":
        return httpx.Response(200, json={"search": []})
    return httpx.Response(200, json={"query": {"pages": []}})


def runner(conn, store, client, llm, run_id) -> GraphRunner:
    return GraphRunner(
        conn,
        store,
        Extractor(llm),
        Linker(client, API_URL, WIKIDATA_URL),
        client,
        API_URL,
        run_id,
    )


def graph_titles(store: GraphStore, run_id: str) -> list[str]:
    with store.driver.session() as session:
        result = session.run(
            "MATCH (ev:Event {run_id: $run}) RETURN ev.title AS title ORDER BY ev.number",
            run=run_id,
        )
        return [record["title"] for record in result]


async def test_events_are_extracted_once_and_again_when_their_spikes_change(
    conn, store, tag
) -> None:
    first = add_spike(conn, tag, f"{tag} Alpha", 0)
    add_event(conn, tag, 1, [first], f"{tag} Alpha")
    name = f"Alpha Person {tag}"
    llm = ScriptedClient(
        [
            answer([entity(name, "person")], title="First title"),
            answer(
                [entity(name, "person")], [relation(name, "visited", f"Beta {tag}")], title="Second"
            ),
        ]
    )
    async with make_client(httpx.MockTransport(quiet_wikimedia)) as client:
        graph = runner(conn, store, client, llm, tag)
        assert await graph.step() == 1
        assert graph_titles(store, tag) == ["First title"]
        assert await graph.step() == 0  # nothing changed: no LLM call
        second = add_spike(conn, tag, f"{tag} Beta", 5)
        join_event(conn, tag, 1, [second])
        assert await graph.step() == 1  # a spike joined: extracted again
    assert graph_titles(store, tag) == ["Second"]
    assert len(llm.requests) == 2
    counts = store.run_counts(tag)
    assert (counts.events, counts.entities, counts.relations) == (1, 2, 1)
    row = conn.execute(
        "SELECT status, entities, relations, model FROM graph_extractions WHERE run_id = %s",
        (tag,),
    ).fetchone()
    assert row == ("ok", 2, 1, "fake:m")


async def test_a_failed_event_is_recorded_and_retried_later(conn, store, tag) -> None:
    for number, word in ((1, "Broken"), (2, "Fine")):
        add_event(conn, tag, number, [add_spike(conn, tag, f"{tag} {word}", number)], word)
    llm = ScriptedClient(
        [
            "not JSON",  # event 1: the first answer ...
            "still not JSON",  # ... and the retry fail: StructuredOutputError
            answer([entity(f"Fine Person {tag}")], title="Fine"),  # event 2 goes on
            answer([entity(f"Broken Person {tag}")], title="Fixed"),  # event 1, later
        ]
    )
    async with make_client(httpx.MockTransport(quiet_wikimedia)) as client:
        graph = runner(conn, store, client, llm, tag)
        await graph.step()
        assert graph.stats.failed == 1 and graph.stats.events == 1
        assert graph_titles(store, tag) == ["Fine"]
        status, failures, error = conn.execute(
            "SELECT status, failures, error FROM graph_extractions "
            "WHERE run_id = %s AND event_number = 1",
            (tag,),
        ).fetchone()
        assert (status, failures) == ("error", 1)
        assert "StructuredOutputError" in error
        # Not before the retry delay ...
        assert pending_events(conn, tag) == []
        # ... but once it has passed (here: no delay).
        assert pending_events(conn, tag, retry=timedelta(0)) == [1]
        conn.execute(
            "UPDATE graph_extractions SET attempted_at = now() - interval '11 minutes' "
            "WHERE run_id = %s",
            (tag,),
        )
        assert await graph.step() == 1
    assert graph_titles(store, tag) == ["Fixed", "Fine"]


async def test_merged_events_leave_the_graph(conn, store, tag) -> None:
    spike = add_spike(conn, tag, f"{tag} Gamma", 0)
    add_event(conn, tag, 1, [spike], "Gamma")
    llm = ScriptedClient([answer([entity(f"Gamma Person {tag}")], title="Gamma")])
    async with make_client(httpx.MockTransport(quiet_wikimedia)) as client:
        graph = runner(conn, store, client, llm, tag)
        await graph.step()
        assert graph_titles(store, tag) == ["Gamma"]
        conn.execute("UPDATE events SET status = 'merged' WHERE run_id = %s", (tag,))
        assert load_event(conn, tag, 1) is None
        await graph.step()
    assert graph_titles(store, tag) == []
    assert store.run_counts(tag).entities == 0
