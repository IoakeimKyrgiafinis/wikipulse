"""Integration tests: the API on the real Postgres and Neo4j.

Run with `uv run pytest -m integration`. The Postgres tests group spikes of their own
`test-<uuid>` run with the real grouper and delete only those rows. Neo4j is only read:
the main graph (which has no events of a test run) and the frozen snapshot
(`neo4j-eval`, skipped when it isn't running).
"""

import time
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import AuthError, ServiceUnavailable

from tests.events.test_events_db import KeywordEncoder, spike, store_page
from wikipulse.api.app import create_app
from wikipulse.api.events import EventReader, PostgresEvents
from wikipulse.config import EventsConfig, get_settings
from wikipulse.db.events import delete_grouping_run
from wikipulse.db.migrate import migrate
from wikipulse.db.spikes import delete_run, upsert_spikes
from wikipulse.events.embed import Embedder
from wikipulse.events.run import GroupingRunner
from wikipulse.mcp_server.explore import Explorer
from wikipulse.qa.graph import QAGraph

pytestmark = pytest.mark.integration

DEADLINE_S = 15


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as connection:
        migrate(connection)
        yield connection


@pytest.fixture
def tag(conn: psycopg.Connection) -> Iterator[str]:
    marker = f"test-{uuid.uuid4().hex}"
    yield marker
    delete_grouping_run(conn, marker)
    conn.execute("DELETE FROM pages WHERE title LIKE %s", (f"{marker}%",))
    delete_run(conn, marker)


@pytest.fixture
def graph() -> Iterator[QAGraph]:
    with QAGraph.from_settings(get_settings()) as main_graph:
        yield main_graph


def wait_for(condition, what: str) -> None:
    deadline = time.monotonic() + DEADLINE_S
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def grouper(conn, tag: str) -> GroupingRunner:
    return GroupingRunner(conn, Embedder("fake", KeywordEncoder()), EventsConfig(), tag, tag)


def test_postgres_rows_for_the_api(conn, tag) -> None:
    election, candidate = f"{tag} Election", f"{tag} Candidate"
    store_page(conn, election, "An election was held.", links=(candidate,))
    store_page(conn, candidate, "A candidate in the election.")
    store_page(conn, f"{tag} Club", "A football club.")
    upsert_spikes(
        conn,
        tag,
        [
            spike(election, 0, 40, score=8.0),
            spike(candidate, 10, None, score=6.0),
            spike(f"{tag} Club", 15, 20),
        ],
    )
    grouper(conn, tag).step()
    rows = PostgresEvents(get_settings().postgres_dsn)

    events = rows.recent(tag, 10)
    # Both active (no later spike has closed them): the latest spike start first.
    assert [(e.number, e.status) for e in events] == [(2, "active"), (1, "active")]
    assert [p.title for p in events[1].pages] == [election, candidate]
    assert [e.number for e in rows.recent(tag, 10, min_pages=2)] == [1]
    assert [e.number for e in rows.recent(tag, 10, number=2)] == [2]
    assert rows.recent(tag, 10, statuses=("ended",)) == []
    assert rows.as_of(tag) == max(e.last_spike_at for e in events)
    assert rows.as_of(f"{tag}-nothing") is None
    rows.ping()


def test_an_event_the_grouper_stores_is_pushed_to_a_websocket_client(conn, tag, graph) -> None:
    """The live path end to end: grouper -> Postgres -> watcher -> WebSocket."""
    try:
        graph.clock()
    except (ServiceUnavailable, AuthError, OSError) as error:
        pytest.skip(f"Neo4j is not reachable: {error}")
    settings = get_settings()
    reader = EventReader(PostgresEvents(settings.postgres_dsn), graph, Explorer(graph), tag)
    app = create_app(reader, reader.explorer, poll_interval_s=0.1, close_on_shutdown=False)
    title = f"{tag} Storm"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wait_for(lambda: app.state.watcher.cards is not None, "the first poll")
        with client.websocket_connect("ws://127.0.0.1/ws/events") as ws:
            assert ws.receive_json()["events"] == []
            store_page(conn, title, "A storm hit the coast.")
            upsert_spikes(conn, tag, [spike(title, 0, None, score=7.5)])
            grouper(conn, tag).step()
            wait_for(lambda: f"{tag}:1" in app.state.watcher.cards, "the watcher to see it")
            ws.send_text("ping")
            pushed = ws.receive_json()
        detail = client.get(f"/events/{tag}:1").json()
    assert pushed["type"] == "event" and pushed["change"] == "new"
    assert pushed["event"]["title"] == title and pushed["event"]["status"] == "active"
    assert pushed["event"]["extracted"] is False
    assert detail["pages"][0]["title"] == title and detail["note"].startswith("Not in the")


def test_event_overviews_on_the_snapshot() -> None:
    settings = get_settings()
    snapshot = settings.model_copy(update={"neo4j_uri": settings.eval_qa_neo4j_uri})
    with QAGraph.from_settings(snapshot) as graph:
        try:
            graph.clock()
        except (ServiceUnavailable, AuthError, OSError) as error:
            pytest.skip(f"The snapshot (neo4j-eval) is not reachable: {error}")
        key = "03b-default-fix:341"
        found = graph.event_overviews([key, "no-such-run:1"])
    assert set(found) == {key}
    crisis = found[key]
    assert "Ueno Bank" in crisis.title and crisis.entity_count > 5
    assert crisis.extracted_at is not None
    assert crisis.briefing_written_at is None and crisis.briefing_sources == ()  # no briefings
