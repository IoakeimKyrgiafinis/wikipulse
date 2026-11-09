"""The watcher and the broadcaster on their own, without FastAPI."""

from dataclasses import replace

import psycopg

from tests.api.helpers import fresh_row, make_parts
from wikipulse.api.events import ANY_STATUS
from wikipulse.api.live import CLOSE, Broadcaster, EventWatcher, as_of, diff


def watcher_for(parts, broadcaster: Broadcaster) -> EventWatcher:
    return EventWatcher(lambda: parts.reader.cards(200, ANY_STATUS), broadcaster, 1)


def drain(client) -> list:
    messages = []
    while not client.queue.empty():
        messages.append(client.queue.get_nowait())
    return messages


def test_diff_finds_new_and_changed_cards() -> None:
    cards = make_parts().reader.cards()
    before = {card.event_id: card for card in cards}
    changed = cards[0].model_copy(update={"page_count": 9})
    new = cards[1].model_copy(update={"event_id": "run:999"})
    assert diff(before, [changed, cards[1], new]) == [("updated", changed), ("new", new)]
    assert diff(before, cards) == []


def test_as_of_is_the_newest_event_time() -> None:
    cards = make_parts().reader.cards()
    assert as_of(cards) == max(t for c in cards for t in (c.last_spike_at, c.ended_at) if t)
    assert as_of([]) is None


async def test_the_first_poll_sends_a_snapshot_then_only_changes() -> None:
    parts, broadcaster = make_parts(), Broadcaster()
    watcher = watcher_for(parts, broadcaster)
    client, snapshot = watcher.subscribe()
    assert snapshot == {"type": "snapshot", "ready": False, "as_of": None, "events": []}

    assert await watcher.poll_once() == 2
    (first,) = drain(client)
    assert first["type"] == "snapshot" and first["ready"] and len(first["events"]) == 2

    assert await watcher.poll_once() == 0
    assert drain(client) == []

    parts.rows.events.append(fresh_row())
    parts.rows.replace(replace(parts.rows.events[0], status="active"))
    assert await watcher.poll_once() == 2
    changes = [(m["change"], m["event"]["event_id"]) for m in drain(client)]
    assert sorted(changes) == [("new", "run:500"), ("updated", "run:341")]


async def test_a_failing_poll_is_reported_and_the_watcher_carries_on() -> None:
    parts, broadcaster = make_parts(), Broadcaster()
    watcher = watcher_for(parts, broadcaster)
    await watcher.poll_once()
    parts.rows.down = True
    assert await watcher.poll_once() == 0
    assert watcher.last_poll_ok is False and watcher.cards  # the last good state stays
    parts.rows.down = False
    parts.rows.events.append(fresh_row())
    assert await watcher.poll_once() == 1
    assert watcher.last_poll_ok is True and watcher.polls == 3


async def test_the_fetch_error_types_do_not_matter() -> None:
    def fail():
        raise psycopg.OperationalError("gone")

    watcher = EventWatcher(fail, Broadcaster(), 1)
    assert await watcher.poll_once() == 0
    assert watcher.cards is None and watcher.last_poll_ok is False


async def test_a_slow_client_is_dropped_and_told_to_close() -> None:
    broadcaster = Broadcaster(queue_size=2)
    slow, fast = broadcaster.subscribe(), broadcaster.subscribe()
    broadcaster.publish({"n": 1})
    broadcaster.publish({"n": 2})
    drain(fast)
    broadcaster.publish({"n": 3})  # slow's queue is full
    assert slow.dropped and drain(slow) == [CLOSE]
    assert drain(fast) == [{"n": 3}]
    assert broadcaster.count == 1


def test_unsubscribe_twice_is_harmless() -> None:
    broadcaster = Broadcaster()
    client = broadcaster.subscribe()
    broadcaster.unsubscribe(client)
    broadcaster.unsubscribe(client)
    broadcaster.publish({"n": 1})
    assert broadcaster.count == 0 and client.queue.empty()
