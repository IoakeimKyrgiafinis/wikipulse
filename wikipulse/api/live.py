"""Pushing events to browsers as they change: a watcher that polls, a broadcaster that fans
out, one bounded queue per WebSocket client.

`EventWatcher.poll_once()` reads the most recent events (Postgres plus the graph, through
`EventReader`), compares them with what it read last time and publishes each new or
changed event to every connected client. Polling keeps the pipeline untouched: the
grouper, the graph runner and the agent don't know the API exists.

Messages (JSON):
- `{"type": "snapshot", "as_of": ..., "events": [...]}`: sent once when a client
  connects (and to everyone after the watcher's first poll);
- `{"type": "event", "change": "new" | "updated", "as_of": ..., "event": {...}}`;
  an event whose `status` is "merged" was absorbed by `merged_into`;
- `{"type": "pong"}`: the answer to a client's "ping".
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import anyio

from wikipulse.api.models import EventCard

logger = logging.getLogger(__name__)

CLIENT_QUEUE_SIZE = 500  # messages a slow client may fall behind before it is dropped
CLOSE = None  # put in a client's queue: "close this connection"

Message = dict[str, Any]


class Client:
    """One WebSocket connection's outbox."""

    def __init__(self, queue_size: int = CLIENT_QUEUE_SIZE) -> None:
        self.queue: asyncio.Queue[Message | None] = asyncio.Queue(queue_size)
        self.dropped = False  # True when it fell too far behind


class Broadcaster:
    """The set of connected clients. Everything runs on the event loop, so no locks."""

    def __init__(self, queue_size: int = CLIENT_QUEUE_SIZE) -> None:
        self.queue_size = queue_size
        self._clients: set[Client] = set()

    @property
    def count(self) -> int:
        return len(self._clients)

    def subscribe(self) -> Client:
        client = Client(self.queue_size)
        self._clients.add(client)
        return client

    def unsubscribe(self, client: Client) -> None:
        self._clients.discard(client)

    def publish(self, message: Message) -> None:
        for client in list(self._clients):
            try:
                client.queue.put_nowait(message)
            except asyncio.QueueFull:
                self._drop(client)

    def _drop(self, client: Client) -> None:
        """A client that can't keep up is disconnected; it reconnects and gets a snapshot."""
        logger.warning("A WebSocket client fell %d messages behind; closing it", self.queue_size)
        self.unsubscribe(client)
        client.dropped = True
        while not client.queue.empty():
            client.queue.get_nowait()
        client.queue.put_nowait(CLOSE)


def as_of(cards: list[EventCard]) -> datetime | None:
    """The newest event time among the cards (event time, not the wall clock)."""
    times = [t for card in cards for t in (card.last_spike_at, card.ended_at) if t is not None]
    return max(times) if times else None


def event_json(card: EventCard) -> Message:
    """A card as a JSON-ready message."""
    return card.model_dump(mode="json")


def diff(previous: dict[str, EventCard], cards: list[EventCard]) -> list[tuple[str, EventCard]]:
    """("new" | "updated", card) for every card that is new or differs from before."""
    changes = []
    for card in cards:
        before = previous.get(card.event_id)
        if before is None:
            changes.append(("new", card))
        elif before != card:
            changes.append(("updated", card))
    return changes


class EventWatcher:
    """Polls `fetch` every `interval_s` seconds and publishes what changed."""

    def __init__(
        self,
        fetch: Callable[[], list[EventCard]],
        broadcaster: Broadcaster,
        interval_s: float,
    ) -> None:
        self.fetch = fetch
        self.broadcaster = broadcaster
        self.interval_s = interval_s
        self.cards: dict[str, EventCard] | None = None  # None until the first poll worked
        self.last_poll_ok: bool | None = None
        self.polls = 0  # polls done, successful or not

    def snapshot(self) -> Message:
        cards = list(self.cards.values()) if self.cards else []
        listed = [card for card in cards if card.status != "merged"]
        return {
            "type": "snapshot",
            "ready": self.cards is not None,
            "as_of": _iso(as_of(cards)),
            "events": [event_json(card) for card in listed],
        }

    def subscribe(self) -> tuple[Client, Message]:
        """A new client and the snapshot it starts from. Synchronous on purpose: nothing
        can be published between taking the snapshot and adding the client."""
        return self.broadcaster.subscribe(), self.snapshot()

    async def poll_once(self) -> int:
        """Read the events once and publish the changes. Returns how many changed."""
        try:
            cards = await anyio.to_thread.run_sync(self.fetch)
        except Exception as error:  # noqa: BLE001 - a failed poll must not end the watcher
            self.polls += 1
            if self.last_poll_ok is not False:
                logger.warning("Could not read the events (will keep trying): %s", error)
            self.last_poll_ok = False
            return 0
        self.polls += 1
        if self.last_poll_ok is False:
            logger.info("Reading the events works again")
        self.last_poll_ok = True
        previous = self.cards
        self.cards = {card.event_id: card for card in cards}
        if previous is None:
            self.broadcaster.publish(self.snapshot())
            return len(cards)
        changes = diff(previous, cards)
        newest = _iso(as_of(cards))
        for change, card in reversed(changes):  # oldest activity first
            self.broadcaster.publish(
                {"type": "event", "change": change, "as_of": newest, "event": event_json(card)}
            )
        if changes:
            logger.info("Pushed %d new or changed event(s)", len(changes))
        return len(changes)

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await anyio.sleep(self.interval_s)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
