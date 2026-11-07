"""Shared helpers for stream tests: the recorded fixture and a scripted fake source."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from wikipulse.stream.sources import Message, parse_recording_line

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "stream_sample.jsonl"


def fixture_messages() -> list[Message]:
    """All `(sse_id, raw_json)` pairs in the committed fixture."""
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    return [parse_recording_line(line) for line in lines if line.strip()]


def fixture_raws() -> list[dict[str, Any]]:
    """The fixture's payloads as dicts."""
    return [json.loads(data) for _, data in fixture_messages()]


def with_unique_ids(messages: list[Message]) -> list[Message]:
    """Give each message a distinct SSE id (real ids can repeat; tests want exact positions)."""
    return [(f"id-{i}", data) for i, (_, data) in enumerate(messages)]


class FakeSource:
    """A scripted source. Resumes after `last_event_id` like the real server.

    `fail_after`: on the first connection, raise after yielding that many messages.
    `block_at_end`: instead of ending, wait forever (like a quiet live stream).
    """

    def __init__(
        self,
        messages: list[Message],
        fail_after: int | None = None,
        block_at_end: bool = False,
    ) -> None:
        self.all = messages
        self.fail_after = fail_after
        self.block_at_end = block_at_end
        self.connections: list[str | None] = []  # the last_event_id of each connection

    async def messages(self, last_event_id: str | None = None) -> AsyncIterator[Message]:
        self.connections.append(last_event_id)
        start = 0
        if last_event_id is not None:
            ids = [sse_id for sse_id, _ in self.all]
            start = ids.index(last_event_id) + 1
        for n, message in enumerate(self.all[start:]):
            if len(self.connections) == 1 and n == self.fail_after:
                raise ConnectionError("connection reset by fake server")
            yield message
            await asyncio.sleep(0)
        if self.block_at_end:
            await asyncio.Event().wait()
