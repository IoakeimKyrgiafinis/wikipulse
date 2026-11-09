"""The HTTP routes, through FastAPI's test client (no server, no network)."""

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable

from tests.api.helpers import RUN, Parts, fresh_row, make_app, make_parts
from tests.mcp_server.helpers import ScriptedAsker
from wikipulse.api.app import STATIC_DIR
from wikipulse.llm.errors import LLMError


@pytest.fixture
def parts() -> Parts:
    return make_parts()


@pytest.fixture
def client(parts: Parts) -> Iterator[TestClient]:
    with TestClient(make_app(parts), base_url="http://127.0.0.1") as test_client:
        yield test_client


# --- GET /events --------------------------------------------------------------------------
def test_events_lists_active_first_then_by_latest_spike(client, parts) -> None:
    parts.rows.events.append(fresh_row())
    body = client.get("/events").json()
    assert body["run_id"] == RUN
    assert [e["event_id"] for e in body["events"]] == ["run:500", "run:341", "run:407"]
    assert body["as_of"].startswith("2026-11-05T22:40")  # newest spike start, event time


def test_an_extracted_event_shows_the_graph_headline_and_counts(client) -> None:
    crisis = client.get("/events").json()["events"][0]
    assert crisis["title"] == "Paraguayan president resigns after Ueno Bank collapse"
    assert crisis["description"].startswith("Santiago Peña resigned")
    assert crisis["extracted"] and crisis["entity_count"] == 4 and crisis["has_briefing"]
    # Three spikes, but "Santiago Pena" redirects to "Santiago Peña": two distinct pages.
    assert crisis["page_count"] == 2 and crisis["spike_count"] == 3
    assert crisis["top_pages"] == ["Ueno Bank", "Santiago Peña"]


def test_a_new_event_is_listed_before_extraction_with_its_page_title(client, parts) -> None:
    parts.rows.events.append(fresh_row())
    fresh = client.get("/events").json()["events"][0]
    assert fresh["title"] == "Storm Amy" and fresh["status"] == "active"
    assert not fresh["extracted"] and fresh["description"] is None
    assert fresh["ended_at"] is None


def test_events_filters(client, parts) -> None:
    parts.rows.events.append(fresh_row())
    ids = lambda **q: [e["event_id"] for e in client.get("/events", params=q).json()["events"]]  # noqa: E731
    assert ids(status="active") == ["run:500"]
    assert ids(status="ended") == ["run:341", "run:407"]
    assert ids(min_pages=2) == ["run:341"]
    assert ids(limit=1) == ["run:500"]


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=201", "status=merged", "min_pages=0", "limit=ten"]
)
def test_events_rejects_bad_parameters(client, query) -> None:
    response = client.get(f"/events?{query}")
    assert response.status_code == 422


def test_events_when_postgres_is_down(client, parts) -> None:
    parts.rows.down = True
    response = client.get("/events")
    assert response.status_code == 503
    assert "Postgres is not reachable" in response.json()["detail"]


def test_events_survive_a_graph_outage_with_the_last_known_graph_data(client, parts) -> None:
    client.get("/events")  # reads the overviews once
    parts.graph.down = True
    crisis = client.get("/events").json()["events"][0]
    assert crisis["extracted"] and crisis["title"].startswith("Paraguayan president")


# --- GET /events/{id} ---------------------------------------------------------------------
@pytest.mark.parametrize("event_id", ["341", "run:341", " 341 "])
def test_event_detail(client, event_id) -> None:
    body = client.get(f"/events/{event_id}").json()
    assert body["event"]["event_id"] == "run:341"
    assert [p["title"] for p in body["pages"]] == ["Ueno Bank", "Santiago Peña"]
    ueno, pena = body["pages"]
    assert ueno["url"].endswith("oldid=101") and ueno["revision"] == 101  # graph's revision
    assert pena["spikes"] == 2
    assert [e["name"] for e in body["entities"]] == ["Pedro Alliana"]
    assert body["note"] is None


def test_event_detail_splits_the_briefing_from_its_sources(client) -> None:
    briefing = client.get("/events/341").json()["briefing"]
    assert briefing["text"] == "Santiago Peña resigned after Ueno Bank collapsed [1][2]."
    assert briefing["sources"] == [
        {"n": 1, "title": "Ueno Bank", "url": "https://en.wikipedia.org/w/index.php?oldid=101"},
        {"n": 2, "title": "WikiPulse event run:341: title and description", "url": None},
    ]
    assert briefing["verified"] is True and briefing["model"] == "fake:m"


def test_an_event_not_yet_extracted_has_pages_and_a_note(client, parts) -> None:
    parts.rows.events.append(fresh_row())
    body = client.get("/events/500").json()
    assert body["entities"] == [] and body["briefing"] is None
    assert "Not in the knowledge graph yet" in body["note"]
    assert body["pages"][0]["url"] == "https://en.wikipedia.org/wiki/Storm_Amy"
    assert body["pages"][0]["last_ended_at"] is None  # still spiking


def test_event_detail_while_the_graph_is_down(client, parts) -> None:
    client.get("/events/341")
    parts.graph.down = True
    response = client.get("/events/341")
    assert response.status_code == 200
    body = response.json()
    assert body["pages"][0]["title"] == "Ueno Bank" and body["event"]["extracted"]
    assert body["note"] == "The knowledge graph (Neo4j) is not reachable right now."


def test_unknown_and_malformed_event_ids(client) -> None:
    assert client.get("/events/999").status_code == 404
    assert client.get("/events/other-run:341").status_code == 404
    bad = client.get("/events/Ueno Bank")
    assert bad.status_code == 422 and "is not an event id" in bad.json()["detail"]
    assert client.get("/events/" + "1" * 121).status_code == 422


# --- POST /ask ------------------------------------------------------------------------------
def test_ask_returns_a_cited_answer(client, parts) -> None:
    response = client.post("/ask", json={"question": "  Why did   Peña resign? "})
    body = response.json()
    assert response.status_code == 200
    assert body["answer"] == "Peña resigned after Ueno Bank collapsed [1][2]."
    assert body["sources"][0]["url"].endswith("oldid=101")
    assert parts.asker.questions == ["Why did Peña resign?"]  # whitespace collapsed


@pytest.mark.parametrize(
    "body",
    [
        {"question": ""},
        {"question": "x" * 501},
        {"question": 42},
        {"question": "ok", "extra": 1},
        {},
        ["question"],
    ],
)
def test_ask_validates_its_input(client, parts, body) -> None:
    response = client.post("/ask", json=body)
    assert response.status_code == 422
    assert "x" * 100 not in response.text  # the input is not echoed back
    assert parts.asker.questions == []


def test_ask_accepts_the_longest_question(client, parts) -> None:
    assert client.post("/ask", json={"question": "x" * 500}).status_code == 200


def test_ask_with_only_whitespace(client) -> None:
    response = client.post("/ask", json={"question": "   \n  "})
    assert response.status_code == 422
    assert response.json()["detail"] == "The question is empty. Ask one question in plain English."


def test_ask_refuses_a_large_or_unsized_body(client, parts) -> None:
    big = client.post(
        "/ask",
        content=b'{"question": "' + b"x" * 9000 + b'"}',
        headers={"content-type": "application/json"},
    )
    assert big.status_code == 413
    chunked = client.post(
        "/ask", content=iter([b'{"question": "hi"}']), headers={"content-type": "application/json"}
    )
    assert chunked.status_code == 411
    assert parts.asker.questions == []


@pytest.mark.parametrize(
    ("error", "status", "text"),
    [
        (LLMError("model not found"), 503, "The answering model failed"),
        (ServiceUnavailable("down"), 503, "knowledge graph (Neo4j) is not reachable"),
    ],
)
def test_ask_when_a_service_fails(error, status, text) -> None:
    parts = make_parts(ScriptedAsker(error=error))
    with TestClient(make_app(parts), base_url="http://127.0.0.1") as client:
        response = client.post("/ask", json={"question": "Why?"})
    assert response.status_code == status
    assert text in response.json()["detail"]


def test_ask_turns_questions_away_when_too_many_wait(parts) -> None:
    app = make_app(parts, max_waiting_questions=1)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.gate.in_flight = 1  # one question is running
        busy = client.post("/ask", json={"question": "Why?"})
        app.state.gate.in_flight = 0
        ok = client.post("/ask", json={"question": "Why?"})
    assert busy.status_code == 429 and ok.status_code == 200
    assert app.state.gate.in_flight == 0  # released after the answer


# --- GET /health ----------------------------------------------------------------------------
def test_health(client, parts) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["postgres"] and body["neo4j"]
    assert body["run_id"] == RUN and body["websocket_clients"] == 0
    parts.graph.down = True
    degraded = client.get("/health")
    assert degraded.status_code == 200
    assert degraded.json()["status"] == "degraded" and not degraded.json()["neo4j"]


# --- The page, docs and hosts ---------------------------------------------------------------
def test_the_page_is_served_with_a_strict_content_security_policy(client) -> None:
    response = client.get("/")
    assert response.status_code == 200 and "<title>WikiPulse" in response.text
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "connect-src 'self'" in policy
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_the_page_loads_nothing_from_outside() -> None:
    """Requirement 1: every script, style and connection is on this server."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for url in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert url.startswith("/"), url
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "location.host" in script  # the WebSocket goes back to where the page came from
    for call in re.findall(r"(?:fetch|WebSocket)\(([^)]*)\)", script):
        assert "http" not in call or "location" in call, call
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert "url(" not in css and "@import" not in css


def test_the_api_documents_itself(client) -> None:
    schema = client.get("/openapi.json").json()
    assert {"/events", "/events/{event_id}", "/ask", "/health"} <= set(schema["paths"])
    question = schema["components"]["schemas"]["AskRequest"]["properties"]["question"]
    assert question["maxLength"] == 500 and question["minLength"] == 1
    assert client.get("/docs").status_code == 200


def test_requests_for_other_hosts_are_refused(client) -> None:
    response = client.get("/health", headers={"host": "wikipulse.attacker.example"})
    assert response.status_code == 400
