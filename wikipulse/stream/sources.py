"""Where raw messages come from: the live SSE stream, or a recording file.

Every source yields `(sse_id, raw_json)` pairs: the SSE `id` (None if unknown) and the
message's `data` payload as an unparsed JSON string. The processing core in
`consumer.py` only sees these pairs, so it can't tell (and doesn't care) whether they
came from the network or from a file. That is what makes replays and tests possible.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from httpx_sse import aconnect_sse

logger = logging.getLogger(__name__)

Message = tuple[str | None, str]

# Generous read timeout: the stream sends many messages per second, so 60 s of
# silence means the connection is dead and we should reconnect.
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)


class Source(Protocol):
    """Anything that yields `(sse_id, raw_json)` pairs, optionally resuming after an id."""

    def messages(self, last_event_id: str | None = None) -> AsyncIterator[Message]:
        """Yield messages, starting after `last_event_id` when it is given."""
        ...


class LiveSource:
    """Wikimedia EventStreams over Server-Sent Events (one HTTP connection per call)."""

    def __init__(
        self,
        url: str,
        user_agent: str,
        since: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.user_agent = user_agent
        self.since = since  # only used when there is no Last-Event-ID to resume from
        self.transport = transport  # tests pass a fake transport; None means the real network

    def request_args(self, last_event_id: str | None) -> tuple[dict[str, str], dict[str, str]]:
        """Headers and query parameters for one connection attempt."""
        headers = {"User-Agent": self.user_agent}
        params: dict[str, str] = {}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        elif self.since:
            params["since"] = self.since
        return headers, params

    async def messages(self, last_event_id: str | None = None) -> AsyncIterator[Message]:
        """Open the stream and yield messages until the server closes it or an error occurs."""
        headers, params = self.request_args(last_event_id)
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, transport=self.transport) as client:
            async with aconnect_sse(
                client, "GET", self.url, headers=headers, params=params
            ) as event_source:
                event_source.response.raise_for_status()
                logger.info("Connected to %s (resume=%s)", self.url, bool(last_event_id))
                async for sse in event_source.aiter_sse():
                    if sse.event == "message" and sse.data:
                        yield (sse.id or None), sse.data


def format_recording_line(sse_id: str | None, data: str, received_at: datetime) -> str:
    """One line of a recording file: the raw SSE id and data, plus when we received it."""
    record = {"id": sse_id, "data": data, "received_at": received_at.isoformat()}
    return json.dumps(record, ensure_ascii=False)


def parse_recording_line(line: str) -> Message:
    """Inverse of `format_recording_line`. A broken line becomes `(None, line)`.

    Passing a broken line on (instead of dropping it) lets the consumer count it as
    malformed, exactly as it would a broken message from the network.
    """
    try:
        record = json.loads(line)
        return record.get("id"), record["data"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, line


def event_timestamp(raw_json: str) -> float | None:
    """The event time (meta.dt) of a raw message as a Unix timestamp, or None if unknown."""
    try:
        raw: Any = json.loads(raw_json)
        return datetime.fromisoformat(raw["meta"]["dt"]).timestamp()
    except (ValueError, KeyError, TypeError):
        return None


class FileSource:
    """Replays a recording file written by `wikipulse.stream.record`.

    `speed=None` replays as fast as possible. `speed=N` replays N times faster than
    real time, pacing messages by their event timestamps (meta.dt).
    """

    def __init__(
        self,
        path: Path,
        speed: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if speed is not None and speed <= 0:
            raise ValueError("speed must be positive (or None for 'max')")
        self.path = path
        self.speed = speed
        self._sleep = sleep  # sleep and clock are replaceable so tests run instantly
        self._clock = clock

    async def messages(self, last_event_id: str | None = None) -> AsyncIterator[Message]:
        """Yield the recorded messages, skipping up to and including `last_event_id`."""
        skipping = last_event_id is not None
        pacer = _Pacer(self.speed, self._sleep, self._clock)
        with self.path.open(encoding="utf-8") as lines:
            for line in lines:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                sse_id, data = parse_recording_line(line)
                if skipping:
                    skipping = sse_id != last_event_id
                    continue
                await pacer.wait_for(data)
                yield sse_id, data


class _Pacer:
    """Sleeps so that event-time gaps are replayed `speed` times faster than real time."""

    def __init__(
        self,
        speed: float | None,
        sleep: Callable[[float], Awaitable[None]],
        clock: Callable[[], float],
    ) -> None:
        self.speed = speed
        self._sleep = sleep
        self._clock = clock
        self._first_event: float | None = None
        self._started: float = 0.0

    async def wait_for(self, raw_json: str) -> None:
        if self.speed is None:
            return
        event_ts = event_timestamp(raw_json)
        if event_ts is None:
            return
        if self._first_event is None:
            self._first_event, self._started = event_ts, self._clock()
            return
        # Compare against the start, not the previous message, so small sleep
        # inaccuracies don't add up. Out-of-order events (in the past) don't wait.
        due = (event_ts - self._first_event) / self.speed
        delay = due - (self._clock() - self._started)
        if delay > 0:
            await self._sleep(delay)
