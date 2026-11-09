"""The WebSocket /ws/events: a snapshot on connect, pushed changes, cleanup on disconnect.

The watcher runs for real inside the test client (polling the fake rows every 50 ms), so
these tests see exactly what a browser would. Nothing waits without a deadline: before
reading a pushed message, the test waits until the watcher has seen the change, then
sends "ping" and reads up to the "pong".
"""

import time
from collections.abc import Callable, Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.api.helpers import Parts, fresh_row, make_app, make_parts, page

DEADLINE_S = 5
WS_URL = "ws://127.0.0.1/ws/events"  # the test client's default host is not a loopback name


def wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + DEADLINE_S
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def until_pong(ws) -> list[dict]:
    """Every message up to the answer to a "ping" (which always comes)."""
    ws.send_text("ping")
    messages = []
    while (message := ws.receive_json())["type"] != "pong":
        messages.append(message)
    return messages


@pytest.fixture
def parts() -> Parts:
    return make_parts()


@pytest.fixture
def app(parts: Parts):
    return make_app(parts)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        wait_for(lambda: app.state.watcher.cards is not None, "the first poll")
        yield test_client


def seen(app, event_id: str, **fields) -> Callable[[], bool]:
    def check() -> bool:
        card = (app.state.watcher.cards or {}).get(event_id)
        return card is not None and all(getattr(card, k) == v for k, v in fields.items())

    return check


def test_a_client_gets_a_snapshot_on_connect(client) -> None:
    with client.websocket_connect(WS_URL) as ws:
        snapshot = ws.receive_json()
    assert snapshot["type"] == "snapshot" and snapshot["ready"] is True
    assert [e["event_id"] for e in snapshot["events"]] == ["run:341", "run:407"]
    assert snapshot["events"][0]["title"].startswith("Paraguayan president")
    assert snapshot["as_of"].startswith("2026-11-05T")


def test_a_new_event_is_pushed(client, app, parts) -> None:
    """Acceptance criterion: a WebSocket test receiving a pushed event."""
    with client.websocket_connect(WS_URL) as ws:
        assert ws.receive_json()["type"] == "snapshot"
        parts.rows.events.append(fresh_row())  # the grouper stores a new event
        wait_for(seen(app, "run:500"), "the watcher to see event 500")
        pushed = until_pong(ws)
    assert len(pushed) == 1
    message = pushed[0]
    assert message["type"] == "event" and message["change"] == "new"
    assert message["event"]["event_id"] == "run:500"
    assert message["event"]["title"] == "Storm Amy" and message["event"]["status"] == "active"
    assert message["as_of"].startswith("2026-11-05T22:40")


def test_changes_to_an_event_are_pushed_as_updates(client, app, parts) -> None:
    parts.rows.events.append(fresh_row())
    wait_for(seen(app, "run:500"), "event 500")
    with client.websocket_connect(WS_URL) as ws:
        ws.receive_json()
        # A second page joins (grouping), then extraction gives it a headline (graph).
        grown = fresh_row()
        grown = replace(grown, pages=grown.pages + (page("Storm Amy (2026)", 410, 8.0),))
        parts.rows.replace(grown)
        wait_for(seen(app, "run:500", page_count=2), "the new page")
        joined = until_pong(ws)
        parts.graph.overviews["run:500"] = replace(
            parts.graph.overviews["run:407"], key="run:500", title="Storm Amy hits Ireland"
        )
        wait_for(seen(app, "run:500", extracted=True), "the extraction")
        extracted = until_pong(ws)
    assert [(m["change"], m["event"]["page_count"]) for m in joined] == [("updated", 2)]
    assert [(m["change"], m["event"]["title"]) for m in extracted] == [
        ("updated", "Storm Amy hits Ireland")
    ]


def test_nothing_is_pushed_when_nothing_changed(client, app) -> None:
    with client.websocket_connect(WS_URL) as ws:
        ws.receive_json()
        polls = app.state.watcher.polls
        wait_for(lambda: app.state.watcher.polls >= polls + 3, "three more polls")
        assert until_pong(ws) == []


def test_a_merged_event_is_pushed_and_left_out_of_snapshots(client, app, parts) -> None:
    with client.websocket_connect(WS_URL) as ws:
        ws.receive_json()
        parts.rows.replace(replace(parts.rows.events[1], status="merged", merged_into=341))
        wait_for(seen(app, "run:407", status="merged"), "the merge")
        pushed = until_pong(ws)
    assert pushed[0]["event"]["status"] == "merged"
    assert pushed[0]["event"]["merged_into"] == "run:341"
    with client.websocket_connect(WS_URL) as ws:
        assert [e["event_id"] for e in ws.receive_json()["events"]] == ["run:341"]


def test_disconnected_clients_are_cleaned_up(client, app, parts) -> None:
    """Requirement 3."""
    with client.websocket_connect(WS_URL) as one, client.websocket_connect(WS_URL):
        one.receive_json()
        wait_for(lambda: app.state.broadcaster.count == 2, "two clients")
    wait_for(lambda: app.state.broadcaster.count == 0, "both clients to be removed")
    # Publishing afterwards reaches nobody and breaks nothing.
    parts.rows.events.append(fresh_row())
    wait_for(seen(app, "run:500"), "event 500")
    assert client.get("/health").json()["websocket_clients"] == 0


def test_a_client_that_falls_behind_is_disconnected(client, app) -> None:
    with client.websocket_connect(WS_URL) as ws:
        ws.receive_json()
        wait_for(lambda: app.state.broadcaster.count == 1, "the client")
        (subscriber,) = app.state.broadcaster._clients
        app.state.broadcaster._drop(subscriber)  # as if its queue had filled up
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 1013  # "try again later": the page reconnects
    assert app.state.broadcaster.count == 0


def test_pages_from_other_sites_may_not_connect(client, app) -> None:
    # Closed before the handshake is accepted (a real server answers HTTP 403).
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(WS_URL, headers={"origin": "https://attacker.example"}):
            pass
    assert refused.value.code == 1008
    with client.websocket_connect(WS_URL, headers={"origin": "http://127.0.0.1:8000"}) as ws:
        assert ws.receive_json()["type"] == "snapshot"
    wait_for(lambda: app.state.broadcaster.count == 0, "cleanup")
