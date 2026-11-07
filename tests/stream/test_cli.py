"""The command-line tools: recorder, replay and stats."""

import argparse
import asyncio
import dataclasses
import io
from datetime import UTC, datetime
from pathlib import Path

import fakeredis
import pytest

from tests.stream.helpers import FakeSource, fixture_messages, with_unique_ids
from wikipulse.stream.consumer import Consumer
from wikipulse.stream.record import Recorder, default_output_path
from wikipulse.stream.replay import REPLAY_NAMESPACE, parse_speed, replay_keys
from wikipulse.stream.resilience import Backoff
from wikipulse.stream.sources import FileSource
from wikipulse.stream.stats import render, take_snapshot


def test_default_recording_path_has_no_colons() -> None:
    path = default_output_path(Path("data"), datetime(2026, 11, 7, 3, 15, 0, tzinfo=UTC))
    assert path == Path("data") / "recordings" / "20261107T031500Z.jsonl"


async def test_recorder_writes_raw_messages_and_survives_a_reconnect(tmp_path: Path) -> None:
    messages = with_unique_ids(fixture_messages())[:50]
    source = FakeSource(messages, fail_after=20, block_at_end=True)
    out = io.StringIO()
    recorder = Recorder(source, out)
    stop = asyncio.Event()

    async def stop_when_done() -> None:
        while recorder.count < len(messages):
            await asyncio.sleep(0.001)
        stop.set()

    watcher = asyncio.create_task(stop_when_done())
    await asyncio.wait_for(recorder.run(stop, Backoff(base_s=0.001, max_s=0.01)), timeout=5)
    await watcher
    assert source.connections == [None, messages[19][0]]
    assert recorder.reconnects == 1
    path = tmp_path / "rec.jsonl"
    path.write_text(out.getvalue(), encoding="utf-8")
    replayed = [m async for m in FileSource(path).messages()]
    assert replayed == messages  # unfiltered and unchanged


@pytest.mark.parametrize(("text", "expected"), [("max", None), ("MAX", None), ("10", 10.0)])
def test_parse_speed(text: str, expected: float | None) -> None:
    assert parse_speed(text) == expected


@pytest.mark.parametrize("text", ["0", "-2", "fast"])
def test_parse_speed_rejects_bad_values(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_speed(text)


def test_replay_keys_never_touch_the_live_checkpoint() -> None:
    keys = replay_keys("wiki:edits", "run1")
    assert keys.stream == "wiki:edits"
    assert keys.checkpoint == f"{REPLAY_NAMESPACE}:last_id"
    assert keys.seen_prefix == f"{REPLAY_NAMESPACE}:run1:seen:"


async def test_stats_snapshot_and_render(fake_server, keys) -> None:
    async_client = fakeredis.FakeAsyncRedis(server=fake_server, decode_responses=True)
    consumer = Consumer(async_client, keys, maxlen=10_000)
    messages = fixture_messages()
    for sse_id, data in messages:
        await consumer.process(sse_id, data)
    sync_client = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
    snap = take_snapshot(sync_client, keys)
    kept = snap.counters["kept"]
    assert snap.counters["received"] == len(messages)
    assert snap.stream_length == kept
    assert snap.recent_kept == kept  # all written just now
    assert 0 <= snap.recent_bots <= kept
    assert snap.newest_event_time is not None
    earlier = dataclasses.replace(snap, taken_at=snap.taken_at - 2)
    text = render(snap, keys, previous=earlier)
    assert "kept events/s" in text
    assert "lag" in text
    assert "messages/s received" in text


def test_stats_on_an_empty_stream(fake_server, keys) -> None:
    client = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
    snap = take_snapshot(client, keys)
    assert snap.stream_length == 0
    assert "n/a" in render(snap, keys)
